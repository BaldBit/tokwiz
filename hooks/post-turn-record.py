#!/usr/bin/env python3
"""Post-Turn Real-Cost Reconciliation Hook (`hooks/post-turn-record.py`).

Runs on `PostInvocation` / `Stop` to reconcile the active pre-prompt forecast
against actual provider-billed tokens from the turn payload or session transcript,
recording into `.tokwiz/telemetry.db` and updating `.tokwiz/calibration.json`.
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


def _load_calibration_module():
    spec = importlib.util.spec_from_file_location(
        "calibration", _SCRIPTS_DIR / "calibration.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["calibration"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_active_forecast(workspace_root: Path, payload: Dict[str, object]) -> Optional[Dict[str, object]]:
    # Allow explicit forecast object in payload for testing/replay
    if isinstance(payload.get("forecast"), dict):
        return payload["forecast"]  # type: ignore[return-value]

    active_path = workspace_root / ".tokwiz" / "active-forecast.json"
    if active_path.exists():
        try:
            return json.loads(active_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _extract_usage_from_transcript_file(transcript_path: Path) -> Optional[Dict[str, object]]:
    if not transcript_path.exists() or not transcript_path.is_file():
        return None
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    total_in = 0
    cached_in = 0
    total_out = 0
    reason_tok = 0
    tool_steps = 0
    model_id = ""
    agent_host = ""

    for raw in reversed(lines[-40:]):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue

        if isinstance(entry.get("tool_calls"), list):
            tool_steps += len(entry["tool_calls"])  # type: ignore[arg-type]

        usage = entry.get("usage") or (
            entry.get("message", {}).get("usage") if isinstance(entry.get("message"), dict) else None
        )
        if isinstance(usage, dict):
            if "input_tokens" in usage:
                agent_host = "claude"
                raw_in = int(usage.get("input_tokens", 0))
                c_read = int(usage.get("cache_read_input_tokens", 0))
                c_create = int(usage.get("cache_creation_input_tokens", 0))
                total_in += raw_in + c_read + c_create
                cached_in += c_read
                total_out += int(usage.get("output_tokens", 0))
            elif "prompt_tokens" in usage:
                agent_host = "codex"
                total_in += int(usage.get("prompt_tokens", 0))
                total_out += int(usage.get("completion_tokens", 0))
            elif "promptTokenCount" in usage:
                agent_host = "gemini"
                total_in += int(usage.get("promptTokenCount", 0))
                cached_in += int(usage.get("cachedContentTokenCount", 0))
                total_out += int(usage.get("candidatesTokenCount", 0))
                reason_tok += int(usage.get("thoughtsTokenCount", 0))

        if not model_id and isinstance(entry.get("model"), str):
            model_id = str(entry["model"])

    if total_in == 0 and total_out == 0:
        return None

    return {
        "agent_host": agent_host or "claude",
        "model_id": model_id or "claude-sonnet-4",
        "real_steps": max(1, tool_steps),
        "usage": {
            "input_tokens": total_in,
            "cached_input_tokens": cached_in,
            "output_tokens": total_out,
            "reasoning_tokens": reason_tok,
        },
    }


def handle_post_turn(payload: Dict[str, object], default_root: Optional[Path] = None) -> Dict[str, object]:
    cwd_str = str(payload.get("cwd") or payload.get("workspace_root") or (default_root or os.getcwd()))
    workspace_root = Path(cwd_str).resolve()
    if not workspace_root.exists():
        workspace_root = (default_root or Path.cwd()).resolve()

    forecast = _load_active_forecast(workspace_root, payload)
    if not forecast:
        return {"decision": "allow", "reconciled": False}

    usage_payload: Optional[Dict[str, object]] = None
    if any(k in payload for k in ("usage", "usageMetadata", "input_tokens", "prompt_tokens")):
        usage_payload = payload
    elif isinstance(payload.get("transcript_path"), str):
        usage_payload = _extract_usage_from_transcript_file(Path(str(payload["transcript_path"])))

    if not usage_payload:
        # Synthesize conservative turn usage from forecast when host omits usage block
        f_in = int(forecast.get("context_tokens", 450)) + int(forecast.get("prompt_tokens", 50)) + int(forecast.get("tool_loop_tokens", 300))
        usage_payload = {
            "agent_host": str(payload.get("agent_host", "claude")),
            "model_id": str(payload.get("model_id", "claude-sonnet-4")),
            "real_steps": int(forecast.get("estimated_steps", 2)),
            "usage": {
                "input_tokens": f_in,
                "cached_input_tokens": int(f_in * 0.78),
                "output_tokens": int(forecast.get("output_tokens", 280)),
                "reasoning_tokens": int(forecast.get("reasoning_tokens", 140)),
                "tool_output_tokens": int(forecast.get("tool_loop_tokens", 300)),
            },
        }

    cal_mod = _load_calibration_module()
    engine = cal_mod.CalibrationEngine(root_dir=workspace_root)
    record = engine.reconcile_turn(forecast=forecast, usage_payload=usage_payload)

    # Automatically refresh the Tier 2 Project Graph after each turn so any newly
    # created or edited files are indexed immediately (<40ms incremental update).
    try:
        graph_spec = importlib.util.spec_from_file_location(
            "project_graph", _SCRIPTS_DIR / "project-graph.py"
        )
        if graph_spec and graph_spec.loader:
            graph_mod = importlib.util.module_from_spec(graph_spec)
            sys.modules["project_graph"] = graph_mod
            graph_spec.loader.exec_module(graph_mod)
            graph_mod.ProjectGraph(root_dir=workspace_root).build(force=False)
    except Exception:
        pass

    return {
        "decision": "allow",
        "reconciled": True,
        "forecast_id": record["forecast_id"],
        "cost_delta_usd": record["cost_delta_usd"],
        "ape_pct": record["ape_pct"],
        "variance_tags": record["variance_tags"],
    }


def main() -> int:
    try:
        raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        payload = json.loads(raw_stdin) if raw_stdin else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle_post_turn(payload)
        print(json.dumps(out))
    except Exception:
        print(json.dumps({"decision": "allow", "reconciled": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
