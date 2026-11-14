#!/usr/bin/env python3
"""Deterministic Anti-Wastage Tool Guardrail Hook (`hooks/pre-tool-guard.py`).

Enforces:
1. Blocking reads of high-waste files: lockfiles (`package-lock.json`, `pnpm-lock.yaml`,
   `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`), build artifacts (`dist/`, `build/`,
   `.next/`, `out/`), vendor directories (`node_modules/`, `.venv/`), binaries, and `.min.js`.
2. Unbounded File Read Interception: if an agent attempts to read a source file >300 lines
   without specifying line range parameters (`StartLine`/`EndLine` or `offset`/`limit`),
   returns the file's compact AST Skeleton with exact line numbers instead.
3. Fail-open resilience (<200ms SLA).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HOOKS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOKS_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

LOCKFILES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.lock",
    "poetry.lock",
    "uv.lock",
    "composer.lock",
    "gemfile.lock",
}

BLOCKED_DIR_SEGMENTS = {
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "out",
    "__pycache__",
    ".git",
    "vendor",
    "target",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
    ".wasm", ".pyc", ".so", ".dylib", ".dll", ".exe", ".bin",
}

READ_TOOL_NAMES = {
    "read",
    "read_file",
    "view_file",
    "cat",
    "mcp_filesystem_read_file",
}

MAX_UNBOUNDED_LINES = 300
MAX_STRUCTURED_UNBOUNDED_LINES = 150
STRUCTURED_DATA_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".sql"}


def _load_skeletonizer():
    spec = importlib.util.spec_from_file_location(
        "code_skeletonizer", _SCRIPTS_DIR / "code-skeletonizer.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["code_skeletonizer"] = mod
    spec.loader.exec_module(mod)
    return mod


def _extract_tool_info(payload: Dict[str, object]) -> Tuple[str, Dict[str, object]]:
    tool_name = str(
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("name")
        or ""
    ).strip()
    tool_input = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("arguments")
        or payload.get("parameters")
        or {}
    )
    if not isinstance(tool_input, dict):
        tool_input = {}
    return tool_name, tool_input


def _extract_file_path(tool_input: Dict[str, object]) -> Optional[str]:
    for key in ("Path", "path", "file_path", "filePath", "file", "target_file"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _has_line_range(tool_input: Dict[str, object]) -> bool:
    range_keys = (
        "StartLine",
        "EndLine",
        "start_line",
        "end_line",
        "offset",
        "limit",
        "line_start",
        "line_end",
    )
    for k in range_keys:
        val = tool_input.get(k)
        if val is not None and val != "":
            return True
    return False


def is_high_waste_path(path_str: str) -> Tuple[bool, str]:
    norm = path_str.replace("\\", "/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return False, ""

    basename = parts[-1].lower()
    if basename in LOCKFILES:
        return True, f"lockfile `{parts[-1]}`"

    if basename.endswith((".min.js", ".min.css", ".bundle.js", ".map")):
        return True, f"minified/bundle asset `{parts[-1]}`"

    ext = os.path.splitext(basename)[1].lower()
    if ext in BINARY_EXTENSIONS:
        return True, f"binary asset `{parts[-1]}`"

    for seg in parts[:-1]:
        if seg.lower() in BLOCKED_DIR_SEGMENTS:
            return True, f"excluded directory `{seg}/`"

    return False, ""


def _record_guardrail_event(
    workspace_root: Path,
    action: str,
    file_path: str,
    tokens_saved: int,
) -> None:
    try:
        tokwiz_dir = workspace_root / ".tokwiz"
        tokwiz_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "action": action,
            "file_path": file_path,
            "tokens_saved": max(0, tokens_saved),
        }
        with open(tokwiz_dir / "guardrail-events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        pass


def _build_deny_response(reason: str) -> Dict[str, object]:
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _build_allow_response() -> Dict[str, object]:
    return {
        "decision": "allow",
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        },
    }


def _extract_line_range(tool_input: Dict[str, object], total_lines: int) -> Tuple[int, int]:
    """Return 1-indexed (start_line, end_line) clamped to [1, total_lines]."""
    total = max(1, total_lines)
    start_val = tool_input.get("StartLine") or tool_input.get("start_line") or tool_input.get("offset")
    end_val = tool_input.get("EndLine") or tool_input.get("end_line")
    limit_val = tool_input.get("limit")
    try:
        start = max(1, int(start_val)) if start_val is not None else 1
    except (TypeError, ValueError):
        start = 1
    try:
        if end_val is not None:
            end = min(total, max(start, int(end_val)))
        elif limit_val is not None:
            end = min(total, start + max(1, int(limit_val)) - 1)
        else:
            end = total
    except (TypeError, ValueError):
        end = total
    return start, end


def _check_and_update_read_ledger(
    workspace_root: Path,
    session_id: str,
    resolved_path: Path,
    raw_path: str,
    content: str,
    lines: List[str],
    req_start: int,
    req_end: int,
) -> Optional[str]:
    """Check if (file, range, sha1) is already in session context; otherwise record it."""
    if os.environ.get("TOKWIZ_DISABLE_READ_CACHE") == "1":
        return None
    sha1 = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    ledger_path = workspace_root / ".tokwiz" / "session-reads.json"
    ledger: Dict[str, Dict[str, object]] = {}
    try:
        if ledger_path.exists():
            loaded = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                ledger = loaded
    except Exception:
        ledger = {}

    sess_map = ledger.get(session_id)
    if not isinstance(sess_map, dict):
        sess_map = {}

    file_key = str(resolved_path)
    entry = sess_map.get(file_key)
    if isinstance(entry, dict) and entry.get("sha1") == sha1:
        ranges = entry.get("ranges") or []
        for r in ranges:
            if isinstance(r, list) and len(r) == 2:
                if int(r[0]) <= req_start and int(r[1]) >= req_end:
                    slice_lines = lines[max(0, req_start - 1) : req_end]
                    saved = max(25, sum(len(ln) + 1 for ln in slice_lines) // 4)
                    _record_guardrail_event(
                        workspace_root, "session_read_cache_hit", raw_path, saved
                    )
                    return (
                        f"[TokWiz Cache Hit] `{raw_path}` L{req_start}-L{req_end} unchanged "
                        f"(sha1: {sha1}) — reuse content already in context (saved ~{saved} tokens)."
                    )
        ranges.append([req_start, req_end])
        entry["ranges"] = ranges[-20:]
    else:
        sess_map[file_key] = {"sha1": sha1, "ranges": [[req_start, req_end]]}

    ledger[session_id] = sess_map
    # Keep ledger bounded across sessions
    if len(ledger) > 10:
        oldest = next(iter(ledger))
        if oldest != session_id:
            del ledger[oldest]

    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    except Exception:
        pass
    return None


def handle_pre_tool_use(
    payload: Dict[str, object],
    default_root: Optional[Path] = None,
) -> Dict[str, object]:
    cwd_str = str(payload.get("cwd") or payload.get("workspace_root") or (default_root or os.getcwd()))
    workspace_root = Path(cwd_str).resolve()
    if not workspace_root.exists():
        workspace_root = (default_root or Path.cwd()).resolve()

    tool_name, tool_input = _extract_tool_info(payload)
    tool_low = tool_name.lower()

    # Check shell commands attempting to cat/head/grep lockfiles directly
    if tool_low in ("bash", "run_command", "shell", "exec"):
        cmd_str = str(tool_input.get("command") or tool_input.get("CommandLine") or "")
        for lf in LOCKFILES:
            if re.search(rf"\b(?:cat|less|more)\s+[^\n;|&]*\b{re.escape(lf)}\b", cmd_str, re.IGNORECASE):
                _record_guardrail_event(workspace_root, "blocked_lockfile_cmd", lf, 12000)
                return _build_deny_response(
                    f"[TokWiz Guardrail] Blocked reading lockfile `{lf}` via shell. "
                    "Inspect `package.json`, `pyproject.toml`, or `Cargo.toml` instead."
                )
        return _build_allow_response()

    if tool_low not in READ_TOOL_NAMES and "read" not in tool_low:
        return _build_allow_response()

    raw_path = _extract_file_path(tool_input)
    if not raw_path:
        return _build_allow_response()

    blocked, waste_reason = is_high_waste_path(raw_path)
    if blocked:
        _record_guardrail_event(workspace_root, "blocked_high_waste_file", raw_path, 8000)
        return _build_deny_response(
            f"[TokWiz Guardrail] Blocked reading high-waste {waste_reason} (`{raw_path}`). "
            "Read source manifests or query `tokwiz graph query <symbol>` instead."
        )

    resolved_path = Path(raw_path)
    if not resolved_path.is_absolute():
        resolved_path = (workspace_root / resolved_path).resolve()

    if not resolved_path.exists() or not resolved_path.is_file():
        return _build_allow_response()

    try:
        content = resolved_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return _build_allow_response()

    lines = content.splitlines()
    has_range = _has_line_range(tool_input)
    is_structured = resolved_path.suffix.lower() in STRUCTURED_DATA_EXTENSIONS
    line_limit = MAX_STRUCTURED_UNBOUNDED_LINES if is_structured else MAX_UNBOUNDED_LINES

    if not has_range and len(lines) > line_limit:
        skel_mod = _load_skeletonizer()
        skeleton_str = skel_mod.skeletonize_file(resolved_path, root_dir=workspace_root, max_tokens=450)
        full_tokens = skel_mod.estimate_tokens(content)
        skel_tokens = skel_mod.estimate_tokens(skeleton_str)
        saved = max(0, full_tokens - skel_tokens)
        event_kind = "skeletonized_structured_read" if is_structured else "skeletonized_unbounded_read"
        _record_guardrail_event(workspace_root, event_kind, raw_path, saved)

        reason = (
            f"[TokWiz Guardrail] `{raw_path}` has {len(lines)} lines (>{line_limit}-line unbounded read limit). "
            f"Here is its compact AST Skeleton (saved ~{saved} tokens). "
            "Re-call read_file with targeted `StartLine` and `EndLine` ranges:\n\n"
            f"{skeleton_str}"
        )
        return _build_deny_response(reason)

    req_start, req_end = _extract_line_range(tool_input, len(lines))
    session_id = str(payload.get("session_id") or "default")
    cache_hit_msg = _check_and_update_read_ledger(
        workspace_root=workspace_root,
        session_id=session_id,
        resolved_path=resolved_path,
        raw_path=raw_path,
        content=content,
        lines=lines,
        req_start=req_start,
        req_end=req_end,
    )
    if cache_hit_msg:
        return _build_deny_response(cache_hit_msg)

    return _build_allow_response()


def main() -> int:
    try:
        raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        payload = json.loads(raw_stdin) if raw_stdin else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle_pre_tool_use(payload)
        print(json.dumps(out))
    except Exception:
        # Always fail open on unexpected errors
        print(json.dumps(_build_allow_response()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
