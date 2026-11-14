#!/usr/bin/env python3
"""AST signature extractor (Code Skeletonizer) for TokWiz.

Strips function/method bodies while preserving imports, class/interface/type
definitions, function/method signatures, and exact start/end line numbers.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from tree_sitter_language_pack import get_parser as _ts_get_parser  # type: ignore

    HAS_TREESITTER = True
except Exception:
    _ts_get_parser = None
    HAS_TREESITTER = False


EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sql": "sql",
}

STRUCTURED_DATA_EXTS = {".json", ".yaml", ".yml", ".toml", ".sql"}


@dataclass
class SymbolSpan:
    name: str
    kind: str  # "class" | "interface" | "type" | "function" | "method"
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str
    parent: Optional[str] = None
    bases: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)


@dataclass
class FileParseResult:
    file_path: str
    language: str
    line_count: int
    imports: List[Tuple[int, str, str]]  # (line_no, raw_stmt, imported_target)
    symbols: List[SymbolSpan]
    parser_backend: str  # "treesitter" | "python_ast" | "structural"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4.0))


def _line_byte_offsets(source: str) -> List[int]:
    offsets = [0]
    encoded = source.encode("utf-8", errors="replace")
    pos = 0
    for line in encoded.splitlines(keepends=True):
        pos += len(line)
        offsets.append(pos)
    return offsets


def _compact_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _extract_calls_from_py_node(node: ast.AST) -> List[str]:
    calls: List[str] = []
    seen = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name not in seen:
                seen.add(name)
                calls.append(name)
    return calls


def _format_py_signature(node: ast.AST, lines: List[str], kind: str) -> str:
    start_idx = getattr(node, "lineno", 1) - 1
    header_lines: List[str] = []
    for idx in range(start_idx, min(len(lines), start_idx + 15)):
        line = lines[idx].strip()
        header_lines.append(line)
        if line.endswith(":"):
            break
    raw = _compact_ws(" ".join(header_lines))
    if raw.endswith(":"):
        raw = raw[:-1].rstrip()
    return raw


def _parse_python_ast(source: str, file_path: str) -> Optional[FileParseResult]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines = source.splitlines()
    offsets = _line_byte_offsets(source)
    imports: List[Tuple[int, str, str]] = []
    symbols: List[SymbolSpan] = []

    def byte_range(start_line: int, end_line: int) -> Tuple[int, int]:
        s = offsets[max(0, min(start_line - 1, len(offsets) - 1))]
        e = offsets[max(0, min(end_line, len(offsets) - 1))]
        return s, e

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                stmt = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else f"import {alias.name}"
                imports.append((node.lineno, stmt, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            stmt = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else f"from {mod} import ..."
            if node.names:
                for alias in node.names:
                    target = f"{mod}.{alias.name}" if mod else alias.name
                    imports.append((node.lineno, stmt, target))
            elif mod:
                imports.append((node.lineno, stmt, mod))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            s_line = node. decorator_list[0].lineno if node.decorator_list else node.lineno
            e_line = getattr(node, "end_lineno", None) or node.lineno
            s_byte, e_byte = byte_range(s_line, e_line)
            sig = _format_py_signature(node, lines, "function")
            calls = _extract_calls_from_py_node(node)
            symbols.append(
                SymbolSpan(
                    name=node.name,
                    kind="function",
                    start_line=s_line,
                    end_line=e_line,
                    start_byte=s_byte,
                    end_byte=e_byte,
                    signature=sig,
                    parent=None,
                    bases=[],
                    calls=calls,
                )
            )
        elif isinstance(node, ast.ClassDef):
            s_line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            e_line = getattr(node, "end_lineno", None) or node.lineno
            s_byte, e_byte = byte_range(s_line, e_line)
            bases: List[str] = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            sig = _format_py_signature(node, lines, "class")
            symbols.append(
                SymbolSpan(
                    name=node.name,
                    kind="class",
                    start_line=s_line,
                    end_line=e_line,
                    start_byte=s_byte,
                    end_byte=e_byte,
                    signature=sig,
                    parent=None,
                    bases=bases,
                    calls=[],
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ms_line = child.decorator_list[0].lineno if child.decorator_list else child.lineno
                    me_line = getattr(child, "end_lineno", None) or child.lineno
                    ms_byte, me_byte = byte_range(ms_line, me_line)
                    msig = _format_py_signature(child, lines, "method")
                    mcalls = _extract_calls_from_py_node(child)
                    symbols.append(
                        SymbolSpan(
                            name=child.name,
                            kind="method",
                            start_line=ms_line,
                            end_line=me_line,
                            start_byte=ms_byte,
                            end_byte=me_byte,
                            signature=msig,
                            parent=node.name,
                            bases=[],
                            calls=mcalls,
                        )
                    )

    return FileParseResult(
        file_path=file_path,
        language="python",
        line_count=len(lines),
        imports=imports,
        symbols=symbols,
        parser_backend="python_ast",
    )


_CALL_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_STOPWORD_CALLS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "sizeof",
    "typeof",
    "new",
    "delete",
    "await",
    "super",
    "function",
    "fn",
    "func",
    "def",
    "print",
    "println",
    "printf",
    "fmt",
    "console",
    "log",
    "len",
    "make",
    "append",
    "panic",
    "require",
    "import",
    "Ok",
    "Err",
    "Some",
}


def _find_brace_end_line(lines: List[str], start_idx: int) -> int:
    """Find matching closing brace line (1-based) starting from 0-based start_idx."""
    depth = 0
    opened = False
    in_str: Optional[str] = None
    escape = False

    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        # Strip // line comments outside strings
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == "\\":
                escape = True
                i += 1
                continue
            if in_str:
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
                i += 1
                continue
            if ch == "/" and i + 1 < n and line[i + 1] == "/":
                break
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return idx + 1
            i += 1
        if not opened and line.rstrip().endswith(";"):
            return idx + 1
    return min(len(lines), start_idx + 1)


def _extract_calls_from_lines(lines: List[str], start_line: int, end_line: int, self_name: str) -> List[str]:
    calls: List[str] = []
    seen = {self_name} | _STOPWORD_CALLS
    for idx in range(max(0, start_line - 1), min(len(lines), end_line)):
        line = lines[idx]
        for m in _CALL_IDENT_RE.finditer(line):
            ident = m.group(1)
            if ident not in seen:
                seen.add(ident)
                calls.append(ident)
    return calls


def _clean_header_signature(lines: List[str], start_idx: int) -> str:
    parts: List[str] = []
    for idx in range(start_idx, min(len(lines), start_idx + 6)):
        s = lines[idx].strip()
        if "{" in s:
            s = s.split("{", 1)[0].strip()
            if s:
                parts.append(s)
            break
        parts.append(s)
        if s.endswith(";"):
            break
    return _compact_ws(" ".join(parts))


def _parse_structural(source: str, file_path: str, language: str) -> FileParseResult:
    lines = source.splitlines()
    offsets = _line_byte_offsets(source)
    imports: List[Tuple[int, str, str]] = []
    symbols: List[SymbolSpan] = []

    def byte_range(start_line: int, end_line: int) -> Tuple[int, int]:
        s = offsets[max(0, min(start_line - 1, len(offsets) - 1))]
        e = offsets[max(0, min(end_line, len(offsets) - 1))]
        return s, e

    # Language-aware regex patterns
    ts_import_re = re.compile(r"""^\s*import\s+.*?\s+from\s+['"]([^'"]+)['"]|^\s*import\s+['"]([^'"]+)['"]""")
    require_re = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
    go_import_single_re = re.compile(r"""^\s*import\s+(?:[A-Za-z0-9_.]+\s+)?["']([^"']+)["']""")
    rust_use_re = re.compile(r"""^\s*(?:pub\s+)?use\s+([^;]+);""")
    c_include_re = re.compile(r"""^\s*#include\s+[<"]([^>"]+)[>"]""")

    class_re = re.compile(
        r"""^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:pub\s+)?(class|interface|struct|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)(?:[^{;]*?(?:extends|implements|:)\s*([A-Za-z0-9_,\s<>]+))?"""
    )
    type_re = re.compile(
        r"""^\s*(?:export\s+)?(?:pub\s+)?type\s+([A-Za-z_][A-Za-z0-9_]*)\b"""
    )
    go_type_re = re.compile(
        r"""^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(struct|interface)\b"""
    )
    func_re = re.compile(
        r"""^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:function\s+|fn\s+|func\s+|def\s+)([A-Za-z_][A-Za-z0-9_]*)\s*[<(]"""
    )
    go_method_re = re.compile(
        r"""^\s*func\s*\(\s*(?:[A-Za-z_][A-Za-z0-9_]*\s+)?\*?([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\("""
    )
    arrow_func_re = re.compile(
        r"""^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*=>"""
    )
    method_re = re.compile(
        r"""^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+|override\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^{=]+)?\{"""
    )

    in_go_import_block = False
    active_classes: List[Tuple[str, int]] = []  # (class_name, end_line)

    for idx, line in enumerate(lines):
        line_no = idx + 1
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            continue

        while active_classes and line_no > active_classes[-1][1]:
            active_classes.pop()

        # Imports
        if language == "go":
            if re.match(r"^\s*import\s*\(\s*$", line):
                in_go_import_block = True
                continue
            if in_go_import_block:
                if stripped.startswith(")"):
                    in_go_import_block = False
                    continue
                m_imp = re.search(r"""["']([^"']+)["']""", stripped)
                if m_imp:
                    imports.append((line_no, f'import "{m_imp.group(1)}"', m_imp.group(1)))
                continue
            m_go_imp = go_import_single_re.match(line)
            if m_go_imp:
                imports.append((line_no, stripped, m_go_imp.group(1)))
                continue

        m_ts = ts_import_re.match(line)
        if m_ts:
            target = m_ts.group(1) or m_ts.group(2)
            imports.append((line_no, stripped, target))
            continue

        m_req = require_re.search(line)
        if m_req and ("const " in line or "let " in line or "var " in line):
            imports.append((line_no, stripped, m_req.group(1)))
            continue

        m_rs = rust_use_re.match(line)
        if m_rs:
            imports.append((line_no, stripped, m_rs.group(1).strip()))
            continue

        m_c = c_include_re.match(line)
        if m_c:
            imports.append((line_no, stripped, m_c.group(1).strip()))
            continue

        # Go struct / interface
        if language == "go":
            m_gt = go_type_re.match(line)
            if m_gt:
                name, kind_raw = m_gt.group(1), m_gt.group(2)
                end_line = _find_brace_end_line(lines, idx)
                s_byte, e_byte = byte_range(line_no, end_line)
                kind = "interface" if kind_raw == "interface" else "class"
                sig = _clean_header_signature(lines, idx)
                symbols.append(
                    SymbolSpan(
                        name=name,
                        kind=kind,
                        start_line=line_no,
                        end_line=end_line,
                        start_byte=s_byte,
                        end_byte=e_byte,
                        signature=sig,
                    )
                )
                active_classes.append((name, end_line))
                continue

            m_gm = go_method_re.match(line)
            if m_gm:
                recv, name = m_gm.group(1), m_gm.group(2)
                end_line = _find_brace_end_line(lines, idx)
                s_byte, e_byte = byte_range(line_no, end_line)
                sig = _clean_header_signature(lines, idx)
                calls = _extract_calls_from_lines(lines, line_no, end_line, name)
                symbols.append(
                    SymbolSpan(
                        name=name,
                        kind="method",
                        start_line=line_no,
                        end_line=end_line,
                        start_byte=s_byte,
                        end_byte=e_byte,
                        signature=sig,
                        parent=recv,
                        calls=calls,
                    )
                )
                continue

        # Rust impl blocks
        if language == "rust":
            m_impl = re.match(
                r"^\s*impl(?:<[^>]*>)?\s+(?:([A-Za-z_][A-Za-z0-9_]*)\s+for\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                line,
            )
            if m_impl:
                trait_name, struct_name = m_impl.group(1), m_impl.group(2)
                end_line = _find_brace_end_line(lines, idx)
                active_classes.append((struct_name, end_line))
                if trait_name:
                    for sym in symbols:
                        if sym.name == struct_name and trait_name not in sym.bases:
                            sym.bases.append(trait_name)
                continue

        # Class / interface / struct / trait
        m_cls = class_re.match(line)
        if m_cls:
            raw_kind, name, raw_bases = m_cls.group(1), m_cls.group(2), m_cls.group(3)
            end_line = _find_brace_end_line(lines, idx)
            s_byte, e_byte = byte_range(line_no, end_line)
            kind = "interface" if raw_kind in ("interface", "trait") else "class"
            bases: List[str] = []
            if raw_bases:
                for tok in re.split(r"[,\s]+", re.sub(r"<[^>]*>", "", raw_bases)):
                    tok = tok.strip()
                    if tok and tok not in ("extends", "implements", "{") and re.match(r"^[A-Za-z_]", tok):
                        bases.append(tok)
            sig = _clean_header_signature(lines, idx)
            symbols.append(
                SymbolSpan(
                    name=name,
                    kind=kind,
                    start_line=line_no,
                    end_line=end_line,
                    start_byte=s_byte,
                    end_byte=e_byte,
                    signature=sig,
                    bases=bases,
                )
            )
            active_classes.append((name, end_line))
            continue

        # Type alias
        m_type = type_re.match(line)
        if m_type:
            name = m_type.group(1)
            end_line = _find_brace_end_line(lines, idx) if "{" in line else line_no
            s_byte, e_byte = byte_range(line_no, end_line)
            sig = _clean_header_signature(lines, idx)
            symbols.append(
                SymbolSpan(
                    name=name,
                    kind="type",
                    start_line=line_no,
                    end_line=end_line,
                    start_byte=s_byte,
                    end_byte=e_byte,
                    signature=sig,
                )
            )
            continue

        # Standard function / Rust fn / Go func / Ruby def
        m_fn = func_re.match(line)
        if m_fn:
            name = m_fn.group(1)
            end_line = _find_brace_end_line(lines, idx)
            s_byte, e_byte = byte_range(line_no, end_line)
            parent = active_classes[-1][0] if active_classes else None
            kind = "method" if parent else "function"
            sig = _clean_header_signature(lines, idx)
            calls = _extract_calls_from_lines(lines, line_no, end_line, name)
            symbols.append(
                SymbolSpan(
                    name=name,
                    kind=kind,
                    start_line=line_no,
                    end_line=end_line,
                    start_byte=s_byte,
                    end_byte=e_byte,
                    signature=sig,
                    parent=parent,
                    calls=calls,
                )
            )
            continue

        # Arrow function
        m_arr = arrow_func_re.match(line)
        if m_arr:
            name = m_arr.group(1)
            end_line = _find_brace_end_line(lines, idx) if "{" in line else line_no
            s_byte, e_byte = byte_range(line_no, end_line)
            sig = _clean_header_signature(lines, idx)
            calls = _extract_calls_from_lines(lines, line_no, end_line, name)
            symbols.append(
                SymbolSpan(
                    name=name,
                    kind="function",
                    start_line=line_no,
                    end_line=end_line,
                    start_byte=s_byte,
                    end_byte=e_byte,
                    signature=sig,
                    calls=calls,
                )
            )
            continue

        # Class method in TS/JS/Java/C#
        if active_classes:
            m_meth = method_re.match(line)
            if m_meth:
                name = m_meth.group(1)
                if name not in _STOPWORD_CALLS:
                    end_line = _find_brace_end_line(lines, idx)
                    s_byte, e_byte = byte_range(line_no, end_line)
                    sig = _clean_header_signature(lines, idx)
                    calls = _extract_calls_from_lines(lines, line_no, end_line, name)
                    symbols.append(
                        SymbolSpan(
                            name=name,
                            kind="method",
                            start_line=line_no,
                            end_line=end_line,
                            start_byte=s_byte,
                            end_byte=e_byte,
                            signature=sig,
                            parent=active_classes[-1][0],
                            calls=calls,
                        )
                    )

    return FileParseResult(
        file_path=file_path,
        language=language,
        line_count=len(lines),
        imports=imports,
        symbols=symbols,
        parser_backend="treesitter" if HAS_TREESITTER else "structural",
    )


def parse_source_file(file_path: Path | str, root_dir: Optional[Path] = None) -> FileParseResult:
    path_obj = Path(file_path)
    rel_path = str(path_obj.relative_to(root_dir)) if root_dir and path_obj.is_relative_to(root_dir) else str(path_obj)
    ext = path_obj.suffix.lower()
    language = EXT_TO_LANG.get(ext, "text")
    try:
        source = path_obj.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return FileParseResult(rel_path, language, 0, [], [], "structural")

    if language == "python":
        res = _parse_python_ast(source, rel_path)
        if res is not None:
            return res

    return _parse_structural(source, rel_path, language)


def render_skeleton(
    parse_result: FileParseResult,
    max_tokens: Optional[int] = None,
) -> str:
    """Format a compact AST signature skeleton with exact line ranges."""
    lines_out: List[str] = [
        f"# [TokWiz AST Skeleton: {parse_result.file_path} ({parse_result.line_count} lines)]"
    ]

    if parse_result.imports:
        seen_imp = set()
        for line_no, stmt, _ in parse_result.imports:
            key = (line_no, stmt)
            if key not in seen_imp:
                seen_imp.add(key)
                lines_out.append(f"L{line_no}: {stmt}")

    for sym in parse_result.symbols:
        indent = "  " if sym.parent else ""
        span = f"L{sym.start_line}-L{sym.end_line}" if sym.end_line > sym.start_line else f"L{sym.start_line}"
        suffix = " { ... }" if sym.kind in ("function", "method", "class", "interface") else ""
        lines_out.append(f"{indent}{span}: {sym.signature}{suffix}")

    if len(lines_out) == 1:
        lines_out.append("# (No structural symbols extracted)")

    if max_tokens is not None and max_tokens > 0:
        capped: List[str] = []
        used = 0
        for line in lines_out:
            t = estimate_tokens(line + "\n")
            if used + t > max_tokens and capped:
                capped.append(f"# ... [truncated at {max_tokens} token budget]")
                break
            capped.append(line)
            used += t
        lines_out = capped

    return "\n".join(lines_out)


def _compact_json_value(value: object, depth: int = 0) -> object:
    if depth >= 4:
        if isinstance(value, dict):
            return {f"... ({len(value)} keys)": "..."}
        if isinstance(value, list):
            return [f"... ({len(value)} items)"]
    if isinstance(value, dict):
        items = list(value.items())
        out = {}
        for k, v in items[:25]:
            out[k] = _compact_json_value(v, depth + 1)
        if len(items) > 25:
            out["..."] = f"+{len(items) - 25} more keys omitted"
        return out
    if isinstance(value, list):
        if len(value) <= 2:
            return [_compact_json_value(v, depth + 1) for v in value]
        return [
            _compact_json_value(value[0], depth + 1),
            _compact_json_value(value[1], depth + 1),
            f"... +{len(value) - 2} similar entries omitted",
        ]
    if isinstance(value, str) and len(value) > 120:
        return value[:117] + "..."
    return value


def _summarize_json(source: str, rel_path: str) -> str:
    lines = source.splitlines()
    header = f"# [TokWiz Structured Skeleton: {rel_path} ({len(lines)} lines, json)]"
    try:
        parsed = json.loads(source)
        compacted = _compact_json_value(parsed, 0)
        body = json.dumps(compacted, indent=2, ensure_ascii=False)
        return f"{header}\n{body}"
    except Exception:
        preview = "\n".join(f"L{i + 1}: {ln}" for i, ln in enumerate(lines[:25]))
        omitted = max(0, len(lines) - 25)
        tail = f"\n# ... +{omitted} lines omitted" if omitted else ""
        return f"{header}\n{preview}{tail}"


def _summarize_yaml_or_toml(source: str, rel_path: str, language: str) -> str:
    lines = source.splitlines()
    out: List[str] = [
        f"# [TokWiz Structured Skeleton: {rel_path} ({len(lines)} lines, {language})]"
    ]
    list_run = 0
    list_indent = -1
    for idx, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        is_list_item = stripped.startswith("- ")
        if is_list_item:
            if indent == list_indent:
                list_run += 1
            else:
                if list_run > 2:
                    out.append(f"{' ' * list_indent}# ... +{list_run - 2} similar entries omitted")
                list_indent = indent
                list_run = 1
            if list_run <= 2:
                out.append(f"L{idx}: {raw[:140]}")
        else:
            if list_run > 2:
                out.append(f"{' ' * max(0, list_indent)}# ... +{list_run - 2} similar entries omitted")
            list_run = 0
            list_indent = -1
            if indent <= 4 or stripped.startswith("["):
                out.append(f"L{idx}: {raw[:140]}")
    if list_run > 2:
        out.append(f"{' ' * max(0, list_indent)}# ... +{list_run - 2} similar entries omitted")
    return "\n".join(out)


def _summarize_sql(source: str, rel_path: str) -> str:
    lines = source.splitlines()
    out: List[str] = [
        f"# [TokWiz Structured Skeleton: {rel_path} ({len(lines)} lines, sql)]"
    ]
    ddl_re = re.compile(
        r"^\s*(?:CREATE|ALTER|DROP)\s+(?:OR\s+REPLACE\s+)?(?:TABLE|INDEX|VIEW|FUNCTION|TRIGGER|TYPE|SCHEMA)\b",
        re.IGNORECASE,
    )
    in_ddl = False
    insert_run = 0
    for idx, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if ddl_re.match(stripped):
            if insert_run > 1:
                out.append(f"-- ... +{insert_run - 1} similar INSERT rows omitted")
            insert_run = 0
            in_ddl = True
            out.append(f"L{idx}: {raw[:140]}")
            if stripped.endswith(";"):
                in_ddl = False
        elif in_ddl:
            out.append(f"L{idx}: {raw[:140]}")
            if stripped.endswith(";"):
                in_ddl = False
        elif stripped.upper().startswith("INSERT INTO"):
            insert_run += 1
            if insert_run == 1:
                out.append(f"L{idx}: {raw[:140]}")
        elif stripped.startswith("(") and insert_run >= 1:
            insert_run += 1
            if insert_run <= 2:
                out.append(f"L{idx}: {raw[:140]}")
    if insert_run > 2:
        out.append(f"-- ... +{insert_run - 2} similar INSERT rows omitted")
    if len(out) == 1:
        for idx, raw in enumerate(lines[:20], 1):
            out.append(f"L{idx}: {raw[:140]}")
        if len(lines) > 20:
            out.append(f"-- ... +{len(lines) - 20} lines omitted")
    return "\n".join(out)


def _cap_skeleton_tokens(text: str, max_tokens: Optional[int]) -> str:
    if max_tokens is None or max_tokens <= 0:
        return text
    capped: List[str] = []
    used = 0
    for line in text.splitlines():
        t = estimate_tokens(line + "\n")
        if used + t > max_tokens and capped:
            capped.append(f"# ... [truncated at {max_tokens} token budget]")
            break
        capped.append(line)
        used += t
    return "\n".join(capped)


def skeletonize_file(
    file_path: Path | str,
    root_dir: Optional[Path] = None,
    max_tokens: Optional[int] = None,
) -> str:
    path_obj = Path(file_path)
    ext = path_obj.suffix.lower()
    if ext in STRUCTURED_DATA_EXTS:
        rel_path = (
            str(path_obj.relative_to(root_dir))
            if root_dir and path_obj.is_relative_to(root_dir)
            else str(path_obj)
        )
        try:
            source = path_obj.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return f"# [TokWiz Structured Skeleton: {rel_path} (unreadable)]"
        if ext == ".json":
            rendered = _summarize_json(source, rel_path)
        elif ext in (".yaml", ".yml", ".toml"):
            lang = "yaml" if ext in (".yaml", ".yml") else "toml"
            rendered = _summarize_yaml_or_toml(source, rel_path, lang)
        else:
            rendered = _summarize_sql(source, rel_path)
        return _cap_skeleton_tokens(rendered, max_tokens)

    parsed = parse_source_file(file_path, root_dir=root_dir)
    return render_skeleton(parsed, max_tokens=max_tokens)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz skeleton",
        description="Extract compact AST signatures with exact line numbers.",
    )
    parser.add_argument("file", help="Path to source file to skeletonize")
    parser.add_argument("--max-tokens", type=int, default=None, help="Maximum token budget for output")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    args = parser.parse_args(argv)

    target = Path(args.file)
    if not target.exists() or not target.is_file():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        return 1

    parsed = parse_source_file(target)
    if args.json:
        payload = {
            "file_path": parsed.file_path,
            "language": parsed.language,
            "line_count": parsed.line_count,
            "parser_backend": parsed.parser_backend,
            "imports": [
                {"line": ln, "statement": stmt, "target": tgt}
                for ln, stmt, tgt in parsed.imports
            ],
            "symbols": [asdict(s) for s in parsed.symbols],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(skeletonize_file(target, max_tokens=args.max_tokens))
    return 0


if __name__ == "__main__":
    sys.exit(main())
