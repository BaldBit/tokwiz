#!/usr/bin/env python3
"""Tier 2 Project Graph Engine for TokWiz.

Builds and queries a SQLite codebase graph (`.tokwiz/graph.db`) with:
- Incremental indexing keyed by (mtime_ns, size_bytes, sha1)
- Nodes: file, class, interface, function, method, type (with exact line/byte spans)
- Edges: IMPORTS, DEFINES, CALLS, INHERITS
- Personalized PageRank ranking + budget-capped subgraph slicing (`--max-tokens 400`)
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

# Dynamically load code-skeletonizer module from the same directory
_SCRIPTS_DIR = Path(__file__).resolve().parent
_SKEL_SPEC = importlib.util.spec_from_file_location(
    "code_skeletonizer", _SCRIPTS_DIR / "code-skeletonizer.py"
)
assert _SKEL_SPEC and _SKEL_SPEC.loader
_skel_mod = importlib.util.module_from_spec(_SKEL_SPEC)
sys.modules["code_skeletonizer"] = _skel_mod
_SKEL_SPEC.loader.exec_module(_skel_mod)

EXT_TO_LANG: Dict[str, str] = _skel_mod.EXT_TO_LANG
estimate_tokens = _skel_mod.estimate_tokens
parse_source_file = _skel_mod.parse_source_file

IGNORED_DIRS = {
    ".git",
    ".tokwiz",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "out",
    ".next",
    "__pycache__",
    ".pytest_cache",
    "target",
    "vendor",
}

IGNORED_FILES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
}


@dataclass
class GraphSliceResult:
    query: str
    matched_symbols: List[Dict[str, object]]
    callers: List[Dict[str, object]]
    callees: List[Dict[str, object]]
    impacted_files: List[str]
    total_lines_in_radius: int
    token_count: int
    formatted_slice: str
    elapsed_ms: float


def _safe_resolve(path_val: Path | str) -> Path:
    try:
        return Path(path_val).resolve()
    except FileNotFoundError:
        pwd_env = os.environ.get("PWD")
        if pwd_env and os.path.isdir(pwd_env):
            os.chdir(pwd_env)
            p = Path(path_val)
            return p.resolve() if p.is_absolute() else (Path(pwd_env) / p).resolve()
        repo_root = Path(__file__).resolve().parent.parent
        os.chdir(str(repo_root))
        return repo_root


class ProjectGraph:
    """SQLite-backed structural codebase graph with Personalized PageRank."""

    def __init__(self, root_dir: Path | str, db_path: Optional[Path | str] = None) -> None:
        self.root_dir = _safe_resolve(root_dir)
        if db_path is not None:
            self.db_path = _safe_resolve(db_path)
        else:
            self.db_path = self.root_dir / ".tokwiz" / "graph.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    file_path TEXT PRIMARY KEY,
                    language TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha1 TEXT NOT NULL,
                    line_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    signature TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
                CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);

                CREATE TABLE IF NOT EXISTS edges (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line_no INTEGER NOT NULL,
                    PRIMARY KEY (source_id, target_id, edge_type, line_no)
                );

                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target_name ON edges(target_name);
                """
            )

    def _iter_source_files(self) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            dirnames[:] = [
                d for d in sorted(dirnames) if d not in IGNORED_DIRS and not d.startswith(".")
            ]
            for fname in sorted(filenames):
                if fname in IGNORED_FILES or fname.endswith(".min.js"):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in EXT_TO_LANG:
                    yield Path(dirpath) / fname

    @staticmethod
    def _file_fingerprint(path: Path) -> Tuple[int, int, str]:
        st = path.stat()
        data = path.read_bytes()
        sha1 = hashlib.sha1(data).hexdigest()
        return st.st_mtime_ns, st.st_size, sha1

    def build(self, force: bool = False) -> Dict[str, object]:
        """Incrementally index repository source files into `.tokwiz/graph.db`."""
        t0 = time.perf_counter()
        with self._connect() as conn:
            existing: Dict[str, Tuple[int, int, str]] = {}
            if not force:
                for row in conn.execute("SELECT file_path, mtime_ns, size_bytes, sha1 FROM files"):
                    existing[row["file_path"]] = (
                        int(row["mtime_ns"]),
                        int(row["size_bytes"]),
                        str(row["sha1"]),
                    )
            else:
                conn.executescript("DELETE FROM files; DELETE FROM nodes; DELETE FROM edges;")

            seen_files: Set[str] = set()
            changed_files: List[Tuple[Path, str, int, int, str]] = []
            unchanged_count = 0

            for full_path in self._iter_source_files():
                rel_path = full_path.relative_to(self.root_dir).as_posix()
                seen_files.add(rel_path)
                st = full_path.stat()
                prev = existing.get(rel_path)
                if prev and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
                    unchanged_count += 1
                    continue
                mtime_ns, size_bytes, sha1 = self._file_fingerprint(full_path)
                if prev and prev[2] == sha1:
                    conn.execute(
                        "UPDATE files SET mtime_ns = ?, size_bytes = ? WHERE file_path = ?",
                        (mtime_ns, size_bytes, rel_path),
                    )
                    unchanged_count += 1
                    continue
                changed_files.append((full_path, rel_path, mtime_ns, size_bytes, sha1))

            deleted_files = set(existing.keys()) - seen_files
            for del_path in deleted_files:
                conn.execute("DELETE FROM files WHERE file_path = ?", (del_path,))
                conn.execute("DELETE FROM nodes WHERE file_path = ?", (del_path,))
                conn.execute("DELETE FROM edges WHERE file_path = ?", (del_path,))

            for full_path, rel_path, mtime_ns, size_bytes, sha1 in changed_files:
                conn.execute("DELETE FROM nodes WHERE file_path = ?", (rel_path,))
                conn.execute("DELETE FROM edges WHERE file_path = ?", (rel_path,))

                parsed = parse_source_file(full_path, root_dir=self.root_dir)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO files (file_path, language, mtime_ns, size_bytes, sha1, line_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rel_path, parsed.language, mtime_ns, size_bytes, sha1, parsed.line_count),
                )

                file_node_id = f"file:{rel_path}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO nodes
                    (node_id, file_path, name, qualified_name, kind, start_line, end_line, start_byte, end_byte, signature)
                    VALUES (?, ?, ?, ?, 'file', 1, ?, 0, ?, ?)
                    """,
                    (
                        file_node_id,
                        rel_path,
                        Path(rel_path).name,
                        rel_path,
                        max(1, parsed.line_count),
                        size_bytes,
                        f"file {rel_path}",
                    ),
                )

                for line_no, _stmt, target in parsed.imports:
                    resolved = self._resolve_import_path(rel_path, target, seen_files)
                    target_id = f"file:{resolved}" if resolved else f"module:{target}"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO edges (source_id, target_id, target_name, edge_type, file_path, line_no)
                        VALUES (?, ?, ?, 'IMPORTS', ?, ?)
                        """,
                        (file_node_id, target_id, target, rel_path, line_no),
                    )

                for sym in parsed.symbols:
                    qname = f"{sym.parent}.{sym.name}" if sym.parent else sym.name
                    sym_id = f"sym:{rel_path}:{qname}:L{sym.start_line}"
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO nodes
                        (node_id, file_path, name, qualified_name, kind, start_line, end_line, start_byte, end_byte, signature)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sym_id,
                            rel_path,
                            sym.name,
                            qname,
                            sym.kind,
                            sym.start_line,
                            sym.end_line,
                            sym.start_byte,
                            sym.end_byte,
                            sym.signature,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO edges (source_id, target_id, target_name, edge_type, file_path, line_no)
                        VALUES (?, ?, ?, 'DEFINES', ?, ?)
                        """,
                        (file_node_id, sym_id, sym.name, rel_path, sym.start_line),
                    )
                    for base in sym.bases:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO edges (source_id, target_id, target_name, edge_type, file_path, line_no)
                            VALUES (?, ?, ?, 'INHERITS', ?, ?)
                            """,
                            (sym_id, f"unresolved:{base}", base, rel_path, sym.start_line),
                        )
                    for callee in sym.calls:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO edges (source_id, target_id, target_name, edge_type, file_path, line_no)
                            VALUES (?, ?, ?, 'CALLS', ?, ?)
                            """,
                            (sym_id, f"unresolved:{callee}", callee, rel_path, sym.start_line),
                        )

            if changed_files or deleted_files or force:
                self._resolve_symbol_edges(conn)

            total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "indexed_files": len(changed_files),
            "unchanged_files": unchanged_count,
            "deleted_files": len(deleted_files),
            "total_files": len(seen_files),
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "elapsed_ms": round(elapsed_ms, 2),
        }

    @staticmethod
    def _resolve_import_path(source_rel_path: str, import_target: str, known_files: Set[str]) -> Optional[str]:
        base_dir = Path(source_rel_path).parent
        cleaned = import_target.strip()
        candidates: List[Path] = []
        if cleaned.startswith("."):
            rel_base = (base_dir / cleaned).as_posix()
            rel_norm = os.path.normpath(rel_base).replace("\\", "/")
            candidates.append(Path(rel_norm))
        else:
            mod_path = cleaned.replace(".", "/")
            candidates.append(Path(mod_path))
            candidates.append(base_dir / mod_path)

        for cand in candidates:
            cand_str = cand.as_posix().lstrip("./")
            if cand_str in known_files:
                return cand_str
            for ext in EXT_TO_LANG:
                with_ext = f"{cand_str}{ext}"
                if with_ext in known_files:
                    return with_ext
                idx_file = f"{cand_str}/index{ext}"
                if idx_file in known_files:
                    return idx_file
        return None

    @staticmethod
    def _resolve_symbol_edges(conn: sqlite3.Connection) -> None:
        """Resolve `unresolved:<name>` edge targets to concrete symbol node_ids."""
        name_to_nodes: Dict[str, List[Tuple[str, str]]] = {}
        for row in conn.execute("SELECT node_id, name, file_path FROM nodes WHERE kind != 'file'"):
            name_to_nodes.setdefault(row["name"], []).append((row["node_id"], row["file_path"]))

        unresolved = conn.execute(
            "SELECT rowid, source_id, target_name, edge_type, file_path, line_no FROM edges WHERE target_id LIKE 'unresolved:%'"
        ).fetchall()

        for row in unresolved:
            tname = row["target_name"]
            matches = name_to_nodes.get(tname)
            if not matches:
                continue
            # Prefer symbol in same file or imported file, otherwise first definition
            chosen_id = matches[0][0]
            for cand_id, cand_file in matches:
                if cand_file != row["file_path"]:
                    chosen_id = cand_id
                    break
            conn.execute(
                "UPDATE OR IGNORE edges SET target_id = ? WHERE rowid = ?",
                (chosen_id, row["rowid"]),
            )

    def _personalized_pagerank(
        self,
        seed_weights: Dict[str, float],
        adjacency: Dict[str, List[str]],
        alpha: float = 0.85,
        iterations: int = 15,
    ) -> Dict[str, float]:
        if not seed_weights:
            return {}
        total_seed = sum(seed_weights.values()) or 1.0
        pers = {k: v / total_seed for k, v in seed_weights.items()}
        all_nodes: Set[str] = set(pers.keys()) | set(adjacency.keys())
        for nbrs in adjacency.values():
            all_nodes.update(nbrs)

        scores: Dict[str, float] = {n: pers.get(n, 0.0) for n in all_nodes}
        for _ in range(iterations):
            next_scores: Dict[str, float] = {n: (1.0 - alpha) * pers.get(n, 0.0) for n in all_nodes}
            for u, u_score in scores.items():
                if u_score <= 1e-9:
                    continue
                nbrs = adjacency.get(u, [])
                if nbrs:
                    share = (alpha * u_score) / len(nbrs)
                    for v in nbrs:
                        next_scores[v] = next_scores.get(v, 0.0) + share
                else:
                    for s_node, s_prob in pers.items():
                        next_scores[s_node] = next_scores.get(s_node, 0.0) + alpha * u_score * s_prob
            scores = next_scores
        return scores

    @staticmethod
    def extract_query_terms(query: str) -> List[str]:
        stopwords = {
            "the", "and", "for", "with", "that", "this", "from", "into", "update",
            "refactor", "fix", "add", "remove", "change", "function", "method",
            "class", "file", "test", "tests", "support", "make", "when", "where",
            "how", "what", "why", "all", "use", "using", "check", "verify",
        }
        raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_./-]*", query)
        terms: List[str] = []
        seen: Set[str] = set()
        for tok in raw_tokens:
            low = tok.lower()
            if len(tok) >= 2 and low not in stopwords and low not in seen:
                seen.add(low)
                terms.append(tok)
        return terms

    def query(
        self,
        query_text: str,
        max_tokens: int = 400,
        auto_build: bool = True,
    ) -> GraphSliceResult:
        """Query the Tier 2 Project Graph using Personalized PageRank and slice to budget."""
        t0 = time.perf_counter()
        if auto_build:
            self.build(force=False)

        terms = self.extract_query_terms(query_text)
        if not terms and query_text.strip():
            terms = [query_text.strip()]

        with self._connect() as conn:
            node_rows = conn.execute(
                "SELECT node_id, file_path, name, qualified_name, kind, start_line, end_line, signature FROM nodes"
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT source_id, target_id, target_name, edge_type, file_path, line_no FROM edges"
            ).fetchall()

        nodes_by_id: Dict[str, Dict[str, object]] = {
            r["node_id"]: dict(r) for r in node_rows
        }
        adjacency: Dict[str, List[str]] = {}
        for e in edge_rows:
            src, dst = e["source_id"], e["target_id"]
            if dst in nodes_by_id:
                adjacency.setdefault(src, []).append(dst)
                adjacency.setdefault(dst, []).append(src)

        seed_weights: Dict[str, float] = {}
        for nid, nd in nodes_by_id.items():
            if nd["kind"] == "file":
                continue
            name = str(nd["name"])
            qname = str(nd["qualified_name"])
            fpath = str(nd["file_path"])
            name_low = name.lower()
            qname_low = qname.lower()
            fpath_low = fpath.lower()

            for term in terms:
                t_low = term.lower()
                if name == term or qname == term:
                    seed_weights[nid] = seed_weights.get(nid, 0.0) + 10.0
                elif name_low == t_low or qname_low == t_low:
                    seed_weights[nid] = seed_weights.get(nid, 0.0) + 8.0
                elif t_low in name_low or name_low in t_low:
                    seed_weights[nid] = seed_weights.get(nid, 0.0) + 4.0
                elif t_low in fpath_low:
                    seed_weights[nid] = seed_weights.get(nid, 0.0) + 2.0

        ppr_scores = self._personalized_pagerank(seed_weights, adjacency)

        ranked_symbols = sorted(
            (
                (score, seed_weights.get(nid, 0.0), nid, nodes_by_id[nid])
                for nid, score in ppr_scores.items()
                if nid in nodes_by_id and nodes_by_id[nid]["kind"] != "file"
            ),
            key=lambda item: (item[1] > 0, item[0], item[1], -int(item[3]["start_line"])),
            reverse=True,
        )

        primary_nodes = [item[3] for item in ranked_symbols[:5] if item[1] > 0]
        if not primary_nodes and ranked_symbols:
            primary_nodes = [ranked_symbols[0][3]]

        primary_ids = {str(n["node_id"]) for n in primary_nodes}
        primary_names = {str(n["name"]) for n in primary_nodes}

        # Collect 1-hop callers and callees for primary nodes
        callers: List[Dict[str, object]] = []
        callees: List[Dict[str, object]] = []
        seen_callers: Set[Tuple[str, int]] = set()
        seen_callees: Set[Tuple[str, int]] = set()

        for e in edge_rows:
            if e["edge_type"] not in ("CALLS", "INHERITS"):
                continue
            src_id = e["source_id"]
            dst_id = e["target_id"]
            tname = e["target_name"]

            # Outgoing call from primary symbol -> Callee
            if src_id in primary_ids and dst_id in nodes_by_id and dst_id not in primary_ids:
                callee_node = nodes_by_id[dst_id]
                key = (str(callee_node["file_path"]), int(callee_node["start_line"]))
                if key not in seen_callees:
                    seen_callees.add(key)
                    callees.append(
                        {
                            "node_id": dst_id,
                            "name": callee_node["name"],
                            "kind": callee_node["kind"],
                            "file_path": callee_node["file_path"],
                            "start_line": callee_node["start_line"],
                            "end_line": callee_node["end_line"],
                            "signature": callee_node["signature"],
                            "score": ppr_scores.get(dst_id, 0.0),
                        }
                    )

            # Incoming call to primary symbol -> Caller
            if (dst_id in primary_ids or tname in primary_names) and src_id in nodes_by_id and src_id not in primary_ids:
                caller_node = nodes_by_id[src_id]
                key = (str(caller_node["file_path"]), int(caller_node["start_line"]))
                if key not in seen_callers:
                    seen_callers.add(key)
                    callers.append(
                        {
                            "node_id": src_id,
                            "name": caller_node["name"],
                            "kind": caller_node["kind"],
                            "file_path": caller_node["file_path"],
                            "start_line": caller_node["start_line"],
                            "end_line": caller_node["end_line"],
                            "signature": caller_node["signature"],
                            "score": ppr_scores.get(src_id, 0.0),
                        }
                    )

        callers.sort(key=lambda c: (-float(c["score"]), str(c["file_path"]), int(c["start_line"])))
        callees.sort(key=lambda c: (-float(c["score"]), str(c["file_path"]), int(c["start_line"])))

        impacted_files_set: Set[str] = set()
        total_lines = 0
        for n in primary_nodes:
            impacted_files_set.add(str(n["file_path"]))
            total_lines += max(1, int(n["end_line"]) - int(n["start_line"]) + 1)
        for c in callers:
            impacted_files_set.add(str(c["file_path"]))
            total_lines += max(1, int(c["end_line"]) - int(c["start_line"]) + 1)
        for c in callees:
            impacted_files_set.add(str(c["file_path"]))
            total_lines += max(1, int(c["end_line"]) - int(c["start_line"]) + 1)

        formatted, token_count = self._format_budget_slice(
            query_text=query_text,
            primary_nodes=primary_nodes,
            callers=callers,
            callees=callees,
            max_tokens=max_tokens,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return GraphSliceResult(
            query=query_text,
            matched_symbols=primary_nodes,
            callers=callers,
            callees=callees,
            impacted_files=sorted(impacted_files_set),
            total_lines_in_radius=total_lines,
            token_count=token_count,
            formatted_slice=formatted,
            elapsed_ms=round(elapsed_ms, 2),
        )

    @staticmethod
    def _format_budget_slice(
        query_text: str,
        primary_nodes: List[Dict[str, object]],
        callers: List[Dict[str, object]],
        callees: List[Dict[str, object]],
        max_tokens: int,
    ) -> Tuple[str, int]:
        if not primary_nodes:
            msg = f"[TokWiz Graph Slice: {query_text} (0 matches)]"
            return msg, estimate_tokens(msg)

        body_lines: List[str] = []
        for n in primary_nodes:
            sig = str(n["signature"])
            kind_short = "fn" if n["kind"] in ("function", "method") else str(n["kind"])
            prefix = "" if sig.startswith((kind_short, "def ", "func ", "function ", "class ", "interface ", "type ", "export ", "pub ")) else f"{kind_short} "
            body_lines.append(
                f"DEF {n['file_path']}:L{n['start_line']}-L{n['end_line']} {prefix}{sig}"
            )

        if callers:
            body_lines.append(f"CALLERS ({len(callers)}):")
            for c in callers:
                body_lines.append(
                    f"  - {c['file_path']}:L{c['start_line']}-L{c['end_line']} {c['name']}"
                )

        if callees:
            body_lines.append(f"CALLEES ({len(callees)}):")
            for c in callees:
                kind_short = "fn" if c["kind"] in ("function", "method") else str(c["kind"])
                body_lines.append(
                    f"  - {c['file_path']}:L{c['start_line']}-L{c['end_line']} {kind_short} {c['name']}()"
                )

        # Enforce strict max_tokens budget
        header_reserve = 18
        budget = max(25, max_tokens - header_reserve)
        kept: List[str] = []
        used = 0
        for line in body_lines:
            t = estimate_tokens(line + "\n")
            if used + t > budget and kept:
                break
            kept.append(line)
            used += t

        body_str = "\n".join(kept)
        prelim = f"[TokWiz Graph Slice: {query_text} (000 tokens)]\n{body_str}"
        tok_count = min(max_tokens, estimate_tokens(prelim))
        final_str = f"[TokWiz Graph Slice: {query_text} ({tok_count} tokens)]\n{body_str}"
        return final_str, estimate_tokens(final_str)

    def _read_lines_cached(self, rel_path: str, cache: Dict[str, List[str]]) -> List[str]:
        if rel_path in cache:
            return cache[rel_path]
        target = self.root_dir / rel_path
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        cache[rel_path] = lines
        return lines

    def slice_code(
        self,
        query_text: str,
        max_tokens: int = 600,
        auto_build: bool = True,
    ) -> GraphSliceResult:
        """Bundle exact AST definition lines and caller call-site snippets in one shot."""
        t0 = time.perf_counter()
        res = self.query(query_text, max_tokens=max_tokens, auto_build=auto_build)
        if not res.matched_symbols:
            return res

        file_cache: Dict[str, List[str]] = {}
        sections: List[str] = []
        header_reserve = 22
        budget = max(60, max_tokens - header_reserve)
        used_tokens = 0

        # 1. Include exact definition bodies for top matched symbols
        for sym in res.matched_symbols[:2]:
            rel_path = str(sym["file_path"])
            lines = self._read_lines_cached(rel_path, file_cache)
            if not lines:
                continue
            start_l = max(1, int(sym["start_line"]))
            end_l = min(len(lines), int(sym["end_line"]))
            block_header = f"--- DEF {rel_path}:L{start_l}-L{end_l} {sym['name']} ---"
            block_lines = [block_header]
            for ln in range(start_l, end_l + 1):
                block_lines.append(f"{ln}: {lines[ln - 1]}")

            added_lines: List[str] = []
            for bl in block_lines:
                t = estimate_tokens(bl + "\n")
                if used_tokens + t > budget and added_lines:
                    added_lines.append("... (truncated to max_tokens)")
                    break
                added_lines.append(bl)
                used_tokens += t
            sections.append("\n".join(added_lines))
            if used_tokens >= budget:
                break

        # 2. Include concise caller call-site snippets if budget remains
        primary_names = [str(s["name"]) for s in res.matched_symbols if s.get("name")]
        if used_tokens < budget and res.callers and primary_names:
            for caller in res.callers[:4]:
                if used_tokens >= budget:
                    break
                rel_path = str(caller["file_path"])
                lines = self._read_lines_cached(rel_path, file_cache)
                if not lines:
                    continue
                c_start = max(1, int(caller["start_line"]))
                c_end = min(len(lines), int(caller["end_line"]))
                hit_lines: List[int] = []
                for ln in range(c_start, c_end + 1):
                    text_l = lines[ln - 1]
                    if any(name in text_l for name in primary_names):
                        hit_lines.append(ln)
                if not hit_lines:
                    hit_lines = [c_start]

                snippet_lines = [f"--- CALLER {rel_path}:L{c_start}-L{c_end} {caller['name']} ---"]
                seen_ln = set()
                for h in hit_lines[:2]:
                    for ln in range(max(c_start, h - 1), min(c_end, h + 1) + 1):
                        if ln not in seen_ln:
                            seen_ln.add(ln)
                            snippet_lines.append(f"{ln}: {lines[ln - 1]}")
                block_text = "\n".join(snippet_lines)
                t = estimate_tokens(block_text + "\n")
                if used_tokens + t <= budget:
                    sections.append(block_text)
                    used_tokens += t

        # 3. Append compact callee index if budget still has room
        if used_tokens < budget and res.callees:
            callee_hdr = "CALLEES: " + ", ".join(
                f"{c['name']} ({c['file_path']}:L{c['start_line']})" for c in res.callees[:5]
            )
            if used_tokens + estimate_tokens(callee_hdr + "\n") <= budget:
                sections.append(callee_hdr)

        body_str = "\n".join(sections) if sections else res.formatted_slice
        prelim = f"[TokWiz Code Slice: {query_text} (000 tokens)]\n{body_str}"
        tok_count = min(max_tokens, estimate_tokens(prelim))
        final_str = f"[TokWiz Code Slice: {query_text} ({tok_count} tokens)]\n{body_str}"
        elapsed_ms = round(res.elapsed_ms + (time.perf_counter() - t0) * 1000.0, 2)
        return GraphSliceResult(
            query=res.query,
            matched_symbols=res.matched_symbols,
            impacted_files=res.impacted_files,
            callers=res.callers,
            callees=res.callees,
            total_lines_in_radius=res.total_lines_in_radius,
            formatted_slice=final_str,
            token_count=estimate_tokens(final_str),
            elapsed_ms=elapsed_ms,
        )

    def trace_path(
        self,
        source_query: str,
        target_query: str,
        max_hops: int = 8,
        max_tokens: int = 450,
    ) -> str:
        """Find shortest call-chain path between source and target symbols with file:line coords."""
        t0 = time.perf_counter()
        self.build(force=False)
        src_low = source_query.strip().lower()
        tgt_low = target_query.strip().lower()
        if not src_low or not tgt_low:
            return "[TokWiz Trace] Both source and target symbols are required."

        with self._connect() as conn:
            node_rows = conn.execute(
                "SELECT node_id, file_path, name, qualified_name, kind, start_line, end_line, signature FROM nodes"
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT source_id, target_id, edge_type FROM edges WHERE edge_type IN ('CALLS', 'IMPORTS')"
            ).fetchall()

        nodes_by_id: Dict[str, Dict[str, object]] = {str(r["node_id"]): dict(r) for r in node_rows}

        def _match_ids(q: str) -> Set[str]:
            exact = {
                nid for nid, n in nodes_by_id.items()
                if str(n["name"]).lower() == q or str(n["qualified_name"]).lower() == q
            }
            if exact:
                return exact
            return {
                nid for nid, n in nodes_by_id.items()
                if n["kind"] != "file" and (q in str(n["name"]).lower() or q in str(n["qualified_name"]).lower())
            }

        src_ids = _match_ids(src_low)
        tgt_ids = _match_ids(tgt_low)
        if not src_ids or not tgt_ids:
            return f"[TokWiz Trace] Symbol not found (source={source_query!r}, target={target_query!r})."

        adj: Dict[str, List[str]] = {}
        for er in edge_rows:
            s_id, d_id = str(er["source_id"]), str(er["target_id"])
            adj.setdefault(s_id, []).append(d_id)

        q_bfs: deque = deque()
        parent: Dict[str, Optional[str]] = {}
        for sid in sorted(src_ids):
            q_bfs.append((sid, 0))
            parent[sid] = None

        found_end: Optional[str] = None
        while q_bfs:
            curr, depth = q_bfs.popleft()
            if curr in tgt_ids and depth > 0:
                found_end = curr
                break
            if depth >= max_hops:
                continue
            for nxt in adj.get(curr, []):
                if nxt not in parent:
                    parent[nxt] = curr
                    q_bfs.append((nxt, depth + 1))

        if found_end is None:
            return (
                f"[TokWiz Trace] No direct call path within {max_hops} hops between "
                f"`{source_query}` and `{target_query}`. Try `tokwiz_slice` on each symbol."
            )

        chain: List[str] = []
        cur_id: Optional[str] = found_end
        while cur_id is not None:
            chain.append(cur_id)
            cur_id = parent.get(cur_id)
        chain.reverse()

        lines: List[str] = []
        for idx, nid in enumerate(chain):
            n = nodes_by_id[nid]
            prefix = "→ " if idx > 0 else "  "
            lines.append(
                f"{prefix}{idx + 1}. {n['name']} ({n['file_path']}:L{n['start_line']}-L{n['end_line']}) :: {n['signature']}"
            )
            if estimate_tokens("\n".join(lines)) > max_tokens - 30:
                break
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return f"[TokWiz Trace: {source_query} -> {target_query} ({len(chain) - 1} hops, {elapsed_ms}ms)]\n" + "\n".join(lines)

    def export_graph_data(
        self,
        max_nodes: int = 120,
        auto_build: bool = True,
        expand_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """Export hierarchical nodes, edges, PageRank scores, and optional expanded neighborhood."""
        if auto_build:
            self.build(force=False)

        with self._connect() as conn:
            file_rows = conn.execute(
                "SELECT file_path, language, line_count, size_bytes FROM files ORDER BY file_path"
            ).fetchall()
            node_rows = conn.execute(
                "SELECT node_id, file_path, name, qualified_name, kind, start_line, end_line, signature FROM nodes"
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT source_id, target_id, target_name, edge_type, file_path, line_no FROM edges"
            ).fetchall()

        nodes_by_id: Dict[str, Dict[str, object]] = {r["node_id"]: dict(r) for r in node_rows}
        adjacency: Dict[str, List[str]] = {}
        in_degree: Dict[str, int] = {}
        out_degree: Dict[str, int] = {}

        resolved_edges: List[Dict[str, object]] = []
        for e in edge_rows:
            src, dst = str(e["source_id"]), str(e["target_id"])
            if src in nodes_by_id and dst in nodes_by_id and src != dst:
                adjacency.setdefault(src, []).append(dst)
                adjacency.setdefault(dst, []).append(src)
                out_degree[src] = out_degree.get(src, 0) + 1
                in_degree[dst] = in_degree.get(dst, 0) + 1
                resolved_edges.append(
                    {
                        "source": src,
                        "target": dst,
                        "type": str(e["edge_type"]),
                        "line": int(e["line_no"]),
                    }
                )

        # Compute global PageRank across all nodes
        uniform_seeds = {nid: 1.0 for nid in nodes_by_id}
        pr_scores = self._personalized_pagerank(uniform_seeds, adjacency) if uniform_seeds else {}

        kind_counts: Dict[str, int] = {}
        for nd in nodes_by_id.values():
            k = str(nd["kind"])
            kind_counts[k] = kind_counts.get(k, 0) + 1

        lang_counts: Dict[str, int] = {}
        for fr in file_rows:
            lg = str(fr["language"])
            lang_counts[lg] = lang_counts.get(lg, 0) + 1

        # Select top-hierarchy nodes up to max_nodes (files -> classes -> top PageRank functions/methods)
        def _hier_rank(nid: str) -> Tuple[int, float, int]:
            kind = str(nodes_by_id[nid]["kind"])
            tier = 2 if kind == "file" else (1 if kind in ("class", "interface", "module") else 0)
            return (
                tier,
                pr_scores.get(nid, 0.0),
                in_degree.get(nid, 0) + out_degree.get(nid, 0),
            )

        ranked_ids = sorted(nodes_by_id.keys(), key=_hier_rank, reverse=True)[:max_nodes]
        kept_set = set(ranked_ids)
        expanded_added_count = 0

        if expand_id and expand_id in nodes_by_id:
            extra_candidates: List[str] = []
            if expand_id not in kept_set:
                extra_candidates.append(expand_id)
            target_nd = nodes_by_id[expand_id]
            for nb in adjacency.get(expand_id, []):
                if nb not in kept_set and nb not in extra_candidates:
                    extra_candidates.append(nb)
            if str(target_nd["kind"]) == "file":
                fp = str(target_nd["file_path"])
                for nid, nd in nodes_by_id.items():
                    if str(nd["file_path"]) == fp and nid not in kept_set and nid not in extra_candidates:
                        extra_candidates.append(nid)
            extra_candidates.sort(
                key=lambda nid: (pr_scores.get(nid, 0.0), in_degree.get(nid, 0) + out_degree.get(nid, 0)),
                reverse=True,
            )
            for eid in extra_candidates[:80]:
                if eid not in kept_set:
                    kept_set.add(eid)
                    ranked_ids.append(eid)
                    expanded_added_count += 1

        def _node_dict(nid: str) -> Dict[str, object]:
            nd = nodes_by_id[nid]
            return {
                "id": nid,
                "name": nd["name"],
                "qualified_name": nd["qualified_name"],
                "kind": nd["kind"],
                "file_path": nd["file_path"],
                "start_line": nd["start_line"],
                "end_line": nd["end_line"],
                "signature": nd["signature"],
                "pagerank": round(pr_scores.get(nid, 0.0) * 100.0, 3),
                "in_degree": in_degree.get(nid, 0),
                "out_degree": out_degree.get(nid, 0),
            }

        export_nodes: List[Dict[str, object]] = [_node_dict(nid) for nid in ranked_ids]

        export_edges = [
            e for e in resolved_edges if e["source"] in kept_set and e["target"] in kept_set
        ]

        non_file_ranked = sorted(
            [nid for nid, nd in nodes_by_id.items() if nd["kind"] != "file"],
            key=lambda nid: (pr_scores.get(nid, 0.0), in_degree.get(nid, 0) + out_degree.get(nid, 0)),
            reverse=True,
        )[:10]
        top_hubs = [_node_dict(nid) for nid in non_file_ranked]

        return {
            "total_files": len(file_rows),
            "total_nodes": len(nodes_by_id),
            "total_edges": len(resolved_edges),
            "kind_counts": kind_counts,
            "language_counts": lang_counts,
            "top_hubs": top_hubs,
            "nodes": export_nodes,
            "edges": export_edges,
            "expanded_node_id": expand_id if (expand_id and expand_id in nodes_by_id) else None,
            "expanded_added_count": expanded_added_count,
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz graph",
        description="Tier 2 Tree-Sitter + Personalized PageRank codebase graph.",
    )
    parser.add_argument("--root", default=".", help="Repository root directory")
    sub = parser.add_subparsers(dest="subcommand")

    p_build = sub.add_parser("build", help="Build or incrementally update the project graph")
    p_build.add_argument("--force", action="store_true", help="Force full re-index")
    p_build.add_argument("--json", action="store_true", help="Output JSON summary")

    p_query = sub.add_parser("query", help="Query symbol neighborhood slice using PageRank")
    p_query.add_argument("query_text", help="Symbol name or prompt keywords")
    p_query.add_argument("--max-tokens", type=int, default=400, help="Maximum tokens in slice (default: 400)")
    p_query.add_argument("--with-code", action="store_true", help="Bundle matched AST body + caller snippets")
    p_query.add_argument("--json", action="store_true", help="Output JSON slice")

    p_slice = sub.add_parser("slice", help="One-shot AST definition + caller snippet bundler")
    p_slice.add_argument("query_text", help="Symbol name or prompt keywords")
    p_slice.add_argument("--root", dest="slice_root", default=None, help="Repository root directory")
    p_slice.add_argument("--max-tokens", type=int, default=600, help="Maximum tokens in code slice (default: 600)")
    p_slice.add_argument("--json", action="store_true", help="Output JSON slice")

    args = parser.parse_args(argv)
    effective_root = getattr(args, "slice_root", None) or args.root
    graph = ProjectGraph(root_dir=Path(effective_root))

    if args.subcommand == "build":
        stats = graph.build(force=args.force)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(
                f"[TokWiz Graph] Indexed {stats['indexed_files']} changed, "
                f"{stats['unchanged_files']} cached ({stats['total_nodes']} nodes, "
                f"{stats['total_edges']} edges) in {stats['elapsed_ms']}ms"
            )
        return 0

    if args.subcommand in ("query", "slice"):
        if args.subcommand == "slice" or getattr(args, "with_code", False):
            res = graph.slice_code(args.query_text, max_tokens=args.max_tokens)
        else:
            res = graph.query(args.query_text, max_tokens=args.max_tokens)
        if args.json:
            print(
                json.dumps(
                    {
                        "query": res.query,
                        "matched_symbols": res.matched_symbols,
                        "callers": res.callers,
                        "callees": res.callees,
                        "impacted_files": res.impacted_files,
                        "total_lines_in_radius": res.total_lines_in_radius,
                        "token_count": res.token_count,
                        "formatted_slice": res.formatted_slice,
                        "elapsed_ms": res.elapsed_ms,
                    },
                    indent=2,
                )
            )
        else:
            print(res.formatted_slice)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
