#!/usr/bin/env python3
"""Claude Code SessionStart Auto-Resume Hook (`hooks/session-start-resume.py`).

Automatically injects the live <200-token `.tokwiz/checkpoint.md` snapshot into
`hookSpecificOutput.additionalContext` whenever the user runs `/clear` or
`/compact`, eliminating the need to manually call `tokwiz_resume`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

_HOOKS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOKS_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_workspace_ops():
    spec = importlib.util.spec_from_file_location(
        "workspace_ops", _SCRIPTS_DIR / "workspace-ops.py"
    )
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["workspace_ops"] = mod
    spec.loader.exec_module(mod)
    return mod


def handle_session_start(
    payload: Dict[str, object],
    default_root: Optional[Path] = None,
) -> Dict[str, object]:
    source = str(payload.get("source") or "").strip().lower()
    if source and source not in ("clear", "compact"):
        return {}

    cwd_str = str(payload.get("cwd") or payload.get("workspace_root") or (default_root or os.getcwd()))
    workspace_root = Path(cwd_str).resolve()
    if not workspace_root.exists():
        workspace_root = (default_root or Path.cwd()).resolve()

    cp_path = workspace_root / ".tokwiz" / "checkpoint.md"
    if not cp_path.exists():
        return {}

    try:
        wops = _load_workspace_ops()
        if wops is not None:
            cp_text = wops.resume_checkpoint(workspace_root, max_tokens=200)
        else:
            cp_text = cp_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return {}

    if not cp_text:
        return {}

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "[TokWiz Auto-Resume after /clear or /compact — working state restored in <200 tokens]\n"
                + cp_text
            ),
        }
    }


def main() -> int:
    try:
        raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        payload = json.loads(raw_stdin) if raw_stdin else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle_session_start(payload)
        print(json.dumps(out))
    except Exception:
        print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
