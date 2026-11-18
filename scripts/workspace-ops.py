#!/usr/bin/env python3
"""Workspace optimization commands for TokWiz:
1. `tokwiz test` — Graph-driven impacted test runner with 1-line pass receipts.
2. `tokwiz checkpoint` — <200-token working memory snapshot for clean context resets.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _is_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name.lower()
    parts = [p.lower() for p in Path(rel_path).parts]
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py") or name.endswith(".test.ts") or name.endswith(".spec.ts"):
        return True
    if name.endswith(".test.js") or name.endswith(".spec.js"):
        return True
    if "tests" in parts and name.endswith(".py") and name != "__init__.py":
        return True
    return False


def _git_changed_files(root_dir: Path) -> Tuple[str, List[str]]:
    branch = "unknown"
    changed: List[str] = []
    try:
        b = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if b.returncode == 0 and b.stdout.strip():
            branch = b.stdout.strip()
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=4,
        )
        if st.returncode == 0:
            for line in st.stdout.splitlines():
                if len(line) >= 4:
                    path_part = line[3:].strip()
                    if " -> " in path_part:
                        path_part = path_part.split(" -> ", 1)[1].strip()
                    changed.append(path_part.strip('"'))
    except Exception:
        pass
    return branch, changed


def _discover_impacted_tests(root_dir: Path, changed_files: List[str]) -> List[str]:
    """Find test files directly or transitively impacted by `changed_files` via graph.db."""
    all_tests = sorted(
        str(p.relative_to(root_dir))
        for p in root_dir.rglob("*")
        if p.is_file()
        and ".git" not in p.parts
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
        and _is_test_file(str(p.relative_to(root_dir)))
    )
    if not all_tests or not changed_files:
        return all_tests

    impacted: Set[str] = {f for f in changed_files if f in set(all_tests)}
    changed_stems = {Path(f).stem.lower().replace("-", "_") for f in changed_files}

    # 1. Graph-based reverse-edge traversal from changed files
    pg_mod = _load_module("project_graph", "project-graph.py")
    graph = pg_mod.ProjectGraph(root_dir=root_dir)
    graph.build(force=False)
    if graph.db_path.exists():
        with contextlib.closing(sqlite3.connect(str(graph.db_path))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT node_id, file_path FROM nodes").fetchall()
            node_to_file = {r["node_id"]: r["file_path"] for r in rows}
            edges = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
            rev_adj: Dict[str, Set[str]] = {}
            for e in edges:
                rev_adj.setdefault(e["target_id"], set()).add(e["source_id"])

            frontier = [nid for nid, fp in node_to_file.items() if fp in set(changed_files)]
            visited = set(frontier)
            for _ in range(3):
                next_frontier: List[str] = []
                for nid in frontier:
                    for caller_id in rev_adj.get(nid, ()):
                        if caller_id not in visited:
                            visited.add(caller_id)
                            next_frontier.append(caller_id)
                            caller_fp = node_to_file.get(caller_id, "")
                            if caller_fp and _is_test_file(caller_fp):
                                impacted.add(caller_fp)
                frontier = next_frontier

    # 2. Import/text fallback for tests referencing modified module stems
    for tf in all_tests:
        if tf in impacted:
            continue
        try:
            text = (root_dir / tf).read_text(encoding="utf-8", errors="replace").lower().replace("-", "_")
            if any(stem and stem in text for stem in changed_stems):
                impacted.add(tf)
        except OSError:
            continue

    return sorted(impacted) if impacted else all_tests


def run_impacted_tests(
    root_dir: Path,
    all_tests: bool = False,
    explicit_files: Optional[List[str]] = None,
    json_out: bool = False,
) -> int:
    t0 = time.perf_counter()
    _, changed_files = _git_changed_files(root_dir)
    if explicit_files:
        targets = [f for f in explicit_files if (root_dir / f).exists()]
    elif all_tests:
        targets = _discover_impacted_tests(root_dir, [])
    else:
        targets = _discover_impacted_tests(root_dir, changed_files)

    py_tests = [t for t in targets if t.endswith(".py")]
    if not py_tests:
        msg = "✓ 0/0 impacted tests (none matched)"
        print(json.dumps({"passed": 0, "total": 0, "targets": [], "status": "ok"}) if json_out else msg)
        return 0

    cmd = [sys.executable, "-m", "unittest", *py_tests, "-v"]
    proc = subprocess.run(cmd, cwd=str(root_dir), capture_output=True, text=True)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ran_m = re.search(r"Ran\s+(\d+)\s+test", combined)
    total = int(ran_m.group(1)) if ran_m else len(py_tests)
    ok = proc.returncode == 0

    status_payload = {
        "ok": ok,
        "passed": total if ok else 0,
        "total": total,
        "targets": py_tests,
        "elapsed_ms": elapsed_ms,
        "timestamp": int(time.time()),
    }
    try:
        tokwiz_dir = root_dir / ".tokwiz"
        tokwiz_dir.mkdir(parents=True, exist_ok=True)
        (tokwiz_dir / "last-test.json").write_text(json.dumps(status_payload, indent=2), encoding="utf-8")
    except Exception:
        pass

    if json_out:
        if not ok:
            status_payload["output"] = combined[-1500:]
        print(json.dumps(status_payload, indent=2))
        return 0 if ok else 1

    if ok:
        label = ", ".join(Path(t).name for t in py_tests[:4])
        if len(py_tests) > 4:
            label += f" +{len(py_tests) - 4} more"
        print(f"✓ {total}/{total} impacted tests passed ({label}) in {elapsed_ms}ms")
        return 0

    flt_mod = _load_module("output_filter", "output-filter.py")
    filtered = flt_mod.filter_output(combined, command="unittest")
    print(filtered)
    return 1


def generate_checkpoint(
    root_dir: Path,
    out_path: Optional[Path] = None,
    max_tokens: int = 200,
) -> str:
    branch, changed = _git_changed_files(root_dir)
    pg_mod = _load_module("project_graph", "project-graph.py")
    graph = pg_mod.ProjectGraph(root_dir=root_dir)

    coords: List[str] = []
    if changed and graph.db_path.exists():
        with contextlib.closing(sqlite3.connect(str(graph.db_path))) as conn:
            conn.row_factory = sqlite3.Row
            for rel in changed[:5]:
                rows = conn.execute(
                    "SELECT name, start_line, end_line FROM nodes WHERE file_path = ? ORDER BY start_line LIMIT 4",
                    (rel,),
                ).fetchall()
                if rows:
                    syms = ", ".join(f"{r['name']}:L{r['start_line']}-{r['end_line']}" for r in rows)
                    coords.append(f"- `{rel}` ({syms})")
                else:
                    coords.append(f"- `{rel}`")
    else:
        for rel in changed[:6]:
            coords.append(f"- `{rel}`")

    test_line = "not run"
    last_test_file = root_dir / ".tokwiz" / "last-test.json"
    if last_test_file.exists():
        try:
            lt = json.loads(last_test_file.read_text(encoding="utf-8"))
            state = "PASS" if lt.get("ok") else "FAIL"
            test_line = f"{state} ({lt.get('passed', 0)}/{lt.get('total', 0)} in {lt.get('elapsed_ms', 0)}ms)"
        except Exception:
            pass

    recent_prompts: List[str] = []
    tel_db = root_dir / ".tokwiz" / "telemetry.db"
    if tel_db.exists():
        try:
            with contextlib.closing(sqlite3.connect(str(tel_db))) as conn:
                rows_p = conn.execute(
                    "SELECT prompt FROM turns ORDER BY timestamp DESC LIMIT 2"
                ).fetchall()
                for rp in reversed(rows_p):
                    p_txt = re.sub(r"\s+", " ", str(rp[0] or "")).strip()
                    if p_txt:
                        recent_prompts.append(p_txt[:70])
        except Exception:
            pass

    lines = [
        f"# TokWiz Checkpoint ({branch})",
        f"- Changed ({len(changed)}): " + (", ".join(changed[:5]) if changed else "clean"),
        f"- Tests: {test_line}",
    ]
    if recent_prompts:
        lines.append("- Recent tasks: " + " | ".join(recent_prompts))
    if coords:
        lines.append("- Symbols:")
        lines.extend(coords)

    # Strictly enforce <max_tokens
    kept: List[str] = []
    used = 0
    for ln in lines:
        t = pg_mod.estimate_tokens(ln + "\n")
        if used + t > max_tokens and kept:
            break
        kept.append(ln)
        used += t
    snapshot = "\n".join(kept)

    target = out_path or (root_dir / ".tokwiz" / "checkpoint.md")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot + "\n", encoding="utf-8")
    except Exception:
        pass
    return snapshot


def resume_checkpoint(root_dir: Path, max_tokens: int = 200) -> str:
    """Return the latest working-memory checkpoint (refreshing if missing or older than 60s)."""
    cp_path = root_dir / ".tokwiz" / "checkpoint.md"
    if cp_path.exists():
        try:
            if (time.time() - cp_path.stat().st_mtime) < 60.0:
                return cp_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return generate_checkpoint(root_dir, out_path=cp_path, max_tokens=max_tokens)


def main_resume(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz resume", description="Reload <200-token working memory checkpoint after /clear.")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--max-tokens", type=int, default=200, help="Maximum tokens (default: 200)")
    args = parser.parse_args(argv)
    print(resume_checkpoint(Path(args.root).resolve(), max_tokens=args.max_tokens))
    return 0


def main_test(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz test", description="Graph-driven impacted test runner.")
    parser.add_argument("files", nargs="*", help="Optional explicit test files")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--all", action="store_true", help="Run all discovered tests")
    parser.add_argument("--json", action="store_true", help="Output JSON receipt")
    args = parser.parse_args(argv)
    return run_impacted_tests(
        Path(args.root).resolve(),
        all_tests=args.all,
        explicit_files=args.files or None,
        json_out=args.json,
    )


def optimize_prompt(root_dir: Path, prompt: str, max_tokens: int = 250) -> str:
    """Rewrite a prompt into a graph-pinned prompt with exact `file:Lstart-Lend` coordinates."""
    pg_mod = _load_module("project_graph", "project-graph.py")
    graph = pg_mod.ProjectGraph(root_dir=root_dir)
    if not (root_dir / ".tokwiz" / "graph.db").exists():
        graph.build(force=False)
    res = graph.query(prompt, max_tokens=max_tokens)
    coords: List[str] = []
    for sym in res.matched_symbols[:6]:
        fp = sym.get("file_path")
        sl = sym.get("start_line")
        el = sym.get("end_line")
        name = sym.get("name")
        kind = sym.get("kind")
        coords.append(f"- `{name}` ({kind}): `{fp}:L{sl}-L{el}`")
    tests = _discover_impacted_tests(root_dir, list(res.impacted_files)[:8])
    lines = [
        f"Task: {prompt.strip()}",
        "",
        "Graph-Pinned Context (skip repo-wide grep/glob; read only these exact ranges):",
    ]
    if coords:
        lines.extend(coords)
    else:
        lines.append("- No direct symbol match in `.tokwiz/graph.db`; use `tokwiz_graph_query` first.")
    if tests:
        lines.append(f"- Impacted tests to verify via `tokwiz_test`: {', '.join(f'`{t}`' for t in tests[:4])}")
    return "\n".join(lines)


def main_optimize(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz optimize", description="Pin prompt with exact AST symbol coordinates.")
    parser.add_argument("prompt", nargs="+", help="Prompt text to optimize")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--max-tokens", type=int, default=250, help="Max graph query budget (default: 250)")
    args = parser.parse_args(argv)
    full_prompt = " ".join(args.prompt)
    print(optimize_prompt(Path(args.root).resolve(), full_prompt, max_tokens=args.max_tokens))
    return 0


def main_checkpoint(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz checkpoint", description="Generate <200-token working memory snapshot.")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--max-tokens", type=int, default=200, help="Maximum tokens in checkpoint (default: 200)")
    parser.add_argument("--out", default="", help="Optional output path (default: .tokwiz/checkpoint.md)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    out_path = Path(args.out) if args.out else None
    print(generate_checkpoint(root, out_path=out_path, max_tokens=args.max_tokens))
    return 0
