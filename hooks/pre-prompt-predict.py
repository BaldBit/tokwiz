#!/usr/bin/env python3
"""Pre-Prompt Token & Cost Forecast Hook (`hooks/pre-prompt-predict.py`).

Supports:
- Gemini CLI / Antigravity `PreInvocation` hook (`metadata.user_markdown` with 0 model tokens)
- Claude Code `UserPromptSubmit` hook (status banner + optional budget gate)
- Fail-open resilience (<200ms SLA; never crashes or blocks on internal errors)
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional

_HOOKS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOKS_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_estimator_module():
    spec = importlib.util.spec_from_file_location(
        "token_estimator", _SCRIPTS_DIR / "token-estimator.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["token_estimator"] = mod
    spec.loader.exec_module(mod)
    return mod


_IDE_WRAPPER_TAG_RE = re.compile(
    r"<(?:ide_opened_file|ide_selection|system-reminder|local-command-caveat|"
    r"command-name|command-message|command-args|local-command-stdout|"
    r"task-notification|teammate-message|agent-notification|user-prompt-submit-hook|"
    r"SYSTEM_MESSAGE|CONTEXT_SUMMARY|user_information|ADDITIONAL_METADATA)\b[^>]*>.*?"
    r"(?:</(?:ide_opened_file|ide_selection|system-reminder|local-command-caveat|"
    r"command-name|command-message|command-args|local-command-stdout|"
    r"task-notification|teammate-message|agent-notification|user-prompt-submit-hook|"
    r"SYSTEM_MESSAGE|CONTEXT_SUMMARY|user_information|ADDITIONAL_METADATA)>|$)",
    re.DOTALL | re.IGNORECASE,
)


def _clean_user_prompt(raw: str) -> str:
    cleaned = _IDE_WRAPPER_TAG_RE.sub("", raw or "").strip()
    if not cleaned:
        return ""
    if (
        cleaned.startswith("[Request interrupted")
        or cleaned.startswith("Base directory for this skill:")
        or cleaned.startswith("This session is being continued from a previous conversation")
    ):
        return ""
    if re.match(r"^<[a-zA-Z_][a-zA-Z0-9_:-]*\b[^>]*>", cleaned):
        return ""
    return cleaned


def _extract_prompt(payload: Dict[str, object]) -> str:
    for key in ("prompt", "user_prompt", "query", "message", "content", "input"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return _clean_user_prompt(val)
    # Nested structures
    hook_input = payload.get("hook_input") or payload.get("request")
    if isinstance(hook_input, dict):
        for key in ("prompt", "user_prompt", "content", "query"):
            val = hook_input.get(key)
            if isinstance(val, str) and val.strip():
                return _clean_user_prompt(val)
    return ""


def _load_config(workspace_root: Path) -> Dict[str, object]:
    cfg_path = workspace_root / ".tokwiz" / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _record_forecast_state(workspace_root: Path, forecast_dict: Dict[str, object]) -> None:
    tokwiz_dir = workspace_root / ".tokwiz"
    tokwiz_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "forecast_id": forecast_dict["forecast_id"],
        "timestamp": time.time(),
        "prompt": forecast_dict["prompt"],
        "profile_code": forecast_dict["profile_code"],
        "profile_name": forecast_dict["profile_name"],
        "optimization_level": forecast_dict["optimization_level"],
        "estimated_steps": forecast_dict["estimated_steps"],
        "blast_radius_files": forecast_dict["blast_radius_files"],
        "blast_radius_lines": forecast_dict["blast_radius_lines"],
        "prompt_tokens": forecast_dict["prompt_tokens"],
        "context_tokens": forecast_dict["context_tokens"],
        "tool_loop_tokens": forecast_dict["tool_loop_tokens"],
        "reasoning_tokens": forecast_dict["reasoning_tokens"],
        "output_tokens": forecast_dict["output_tokens"],
        "total_tokens": forecast_dict["total_tokens"],
        "unoptimized_tokens": forecast_dict["unoptimized_tokens"],
        "savings_tokens": forecast_dict["savings_tokens"],
        "savings_pct": forecast_dict["savings_pct"],
        "costs": forecast_dict["costs"],
    }
    (tokwiz_dir / "active-forecast.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    with open(tokwiz_dir / "forecasts.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def handle_pre_prompt(payload: Dict[str, object], default_root: Optional[Path] = None) -> Dict[str, object]:
    """Process a PreInvocation / UserPromptSubmit hook payload and return JSON response."""
    cwd_str = str(payload.get("cwd") or payload.get("workspace_root") or (default_root or os.getcwd()))
    workspace_root = Path(cwd_str).resolve()
    if not workspace_root.exists():
        workspace_root = (default_root or Path.cwd()).resolve()

    prompt = _extract_prompt(payload)
    if not prompt:
        return {"systemMessage": "", "decision": "allow"}

    cfg = _load_config(workspace_root)
    forced_level = cfg.get("optimization_level")
    level_int = int(forced_level) if isinstance(forced_level, int) and forced_level in (1, 2, 3) else None

    est_mod = _load_estimator_module()
    estimator = est_mod.TokenEstimator(root_dir=workspace_root)
    res = estimator.estimate(prompt=prompt, forced_level=level_int)
    res_dict = asdict(res)

    try:
        _record_forecast_state(workspace_root, res_dict)
    except Exception:
        pass

    banner = res.banner
    event_name = str(payload.get("hook_event_name") or payload.get("event") or "PreInvocation")

    # Surface Context-Rot / Bloat alert in the zero-token UI banner if latest turn exceeded 100k tok/step
    try:
        db_path = workspace_root / ".tokwiz" / "telemetry.db"
        if db_path.exists():
            import contextlib
            import sqlite3

            with contextlib.closing(sqlite3.connect(str(db_path), timeout=0.3)) as conn:
                row = conn.execute(
                    "SELECT real_input_tokens, real_steps FROM turns ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
            if row:
                tok_per_step = int(row[0] or 0) // max(1, int(row[1] or 1))
                if tok_per_step > 100_000:
                    banner += (
                        f"\n> ⚠️ **Context Bloat Alert ({tok_per_step // 1000}k tok/step):** "
                        "Run `/clear` then call `tokwiz_resume` (`tokwiz resume`) to reload your <200-token checkpoint."
                    )
    except Exception:
        pass

    # Optional budget gate check
    max_cost = cfg.get("max_prompt_cost_usd")
    enforce_gate = bool(cfg.get("enforce_budget_gate", False))
    claude_cost = res.costs.get("claude-sonnet-4")
    est_usd = claude_cost.uncached_cost_usd if claude_cost else 0.0

    if isinstance(max_cost, (int, float)) and est_usd > float(max_cost):
        gate_warn = (
            f"\n> ⚠️ **Budget Gate Alert:** Forecasted cost (${est_usd:.3f}) "
            f"exceeds configured max-prompt-cost (${float(max_cost):.2f})."
        )
        banner = banner + gate_warn
        if enforce_gate:
            return {
                "decision": "block",
                "reason": banner,
                "systemMessage": "",
                "metadata": {
                    "user_markdown": banner,
                    "forecast_id": res.forecast_id,
                    "budget_exceeded": True,
                },
            }

    # Zero-token UI delivery: systemMessage is empty "" so 0 tokens are injected into LLM context
    response: Dict[str, object] = {
        "decision": "allow",
        "systemMessage": "",
        "metadata": {
            "user_markdown": banner,
            "forecast_id": res.forecast_id,
            "profile_code": res.profile_code,
            "optimization_level": res.optimization_level,
            "forecast_tokens": res.total_tokens,
            "unoptimized_tokens": res.unoptimized_tokens,
            "savings_pct": res.savings_pct,
        },
    }
    if event_name == "UserPromptSubmit":
        response["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "",
        }
    return response


def main() -> int:
    try:
        raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        payload = json.loads(raw_stdin) if raw_stdin else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle_pre_prompt(payload)
        print(json.dumps(out))
    except Exception:
        # Fail-open SLA: never block host agent on hook failure
        print(json.dumps({"decision": "allow", "systemMessage": ""}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
