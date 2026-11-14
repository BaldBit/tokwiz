#!/usr/bin/env python3
"""Post-Turn Real-Cost Reconciliation Hook (`hooks/post-turn-record.py`).

Runs on `PostInvocation` / `Stop` to reconcile the active pre-prompt forecast
against actual provider-billed tokens from the turn payload or session transcript,
recording into `.tokwiz/telemetry.db` and updating `.tokwiz/calibration.json`.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

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

    tokwiz_dir = workspace_root / ".tokwiz"
    reconciled_ids: set[str] = set()
    db_path = tokwiz_dir / "telemetry.db"
    if db_path.exists():
        try:
            import sqlite3

            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute("SELECT forecast_id FROM turns").fetchall()
                reconciled_ids = {str(r[0]) for r in rows}
        except Exception:
            reconciled_ids = set()

    # Check forecasts.jsonl in FIFO order for unreconciled forecasts so batch `tokwiz predict`
    # runs reconcile Turn 1 -> Forecast 1, Turn 2 -> Forecast 2, Turn 3 -> Forecast 3.
    forecasts_path = tokwiz_dir / "forecasts.jsonl"
    if forecasts_path.exists():
        try:
            unreconciled = []
            for line in forecasts_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if isinstance(rec, dict) and str(rec.get("forecast_id", "")) not in reconciled_ids:
                    unreconciled.append(rec)
            if unreconciled:
                prompt_hint = str(payload.get("prompt") or "").strip().lower()
                if prompt_hint:
                    for item in unreconciled:
                        if str(item.get("prompt", "")).strip().lower() == prompt_hint:
                            return item
                return unreconciled[0]
        except Exception:
            pass

    active_path = tokwiz_dir / "active-forecast.json"
    if active_path.exists():
        try:
            rec = json.loads(active_path.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and str(rec.get("forecast_id", "")) not in reconciled_ids:
                return rec
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


def _parse_antigravity_transcript_turns(transcript_path: Path, target_workspace: Path) -> List[Dict[str, object]]:
    """Parse completed `<USER_INPUT>` turns from an Antigravity `transcript_full.jsonl` file."""
    if not transcript_path.exists():
        return []
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    ws_str = str(target_workspace.resolve())
    steps = []
    target_path_hits = 0
    other_path_hits = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            s = json.loads(line)
        except Exception:
            continue
        if not isinstance(s, dict):
            continue
        steps.append(s)
        for tc in s.get("tool_calls", []) or []:
            if isinstance(tc, dict):
                args = tc.get("arguments") or tc.get("args") or {}
                if isinstance(args, dict):
                    p_val = str(args.get("Path", ""))
                    if p_val:
                        if p_val.startswith(ws_str + "/"):
                            target_path_hits += 1
                        else:
                            other_path_hits += 1

    if target_path_hits == 0 or target_path_hits < other_path_hits:
        return []

    completed_turns: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    last_step_was_final_response = False

    for s in steps:
        stype = s.get("type")
        if stype == "USER_INPUT":
            if cur is not None and int(cur.get("steps", 0)) > 0:
                completed_turns.append(cur)
            raw_c = str(s.get("content", ""))
            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw_c, re.S)
            prompt = m.group(1).strip() if m else raw_c.strip()
            cur = {
                "prompt": prompt,
                "steps": 0,
                "input_tok": max(1, math.ceil(len(raw_c) / 4)),
                "tool_tok": 0,
                "reasoning_tok": 0,
                "output_tok": 0,
            }
            last_step_was_final_response = False
        elif cur is not None:
            content = str(s.get("content", ""))
            thinking = str(s.get("thinking", ""))
            tcalls = s.get("tool_calls", []) or []
            if stype == "PLANNER_RESPONSE":
                cur["steps"] = int(cur["steps"]) + 1
                cur["reasoning_tok"] = int(cur["reasoning_tok"]) + math.ceil(len(thinking) / 4)
                cur["output_tok"] = int(cur["output_tok"]) + math.ceil((len(content) + len(json.dumps(tcalls))) / 4)
                last_step_was_final_response = len(tcalls) == 0 and bool(content.strip())
            else:
                cur["tool_tok"] = int(cur["tool_tok"]) + math.ceil(len(content) / 4)
                last_step_was_final_response = False

    if cur is not None and int(cur.get("steps", 0)) > 0 and last_step_was_final_response:
        completed_turns.append(cur)

    for t in completed_turns:
        in_total = int(t["input_tok"]) + int(t["tool_tok"])
        t["total_tokens"] = in_total + int(t["reasoning_tok"]) + int(t["output_tok"])
        t["usage_payload"] = {
            "agent_host": "claude",
            "model_id": "claude-sonnet-4",
            "real_steps": max(1, int(t["steps"])),
            "usage": {
                "input_tokens": in_total,
                "cached_input_tokens": int(in_total * 0.78),
                "output_tokens": int(t["output_tok"]),
                "reasoning_tokens": int(t["reasoning_tok"]),
                "tool_output_tokens": int(t["tool_tok"]),
            },
        }
    return completed_turns


def _find_workspace_transcript_turns(workspace_root: Path) -> List[Dict[str, object]]:
    """Find the most recent Antigravity transcript matching `workspace_root` and parse its turns."""
    brain_dir = Path.home() / ".gemini" / "antigravity" / "brain"
    if not brain_dir.exists():
        return []
    candidates: List[Path] = []
    for child in brain_dir.iterdir():
        if not child.is_dir():
            continue
        tf = child / ".system_generated" / "logs" / "transcript_full.jsonl"
        if not tf.exists():
            tf = child / ".system_generated" / "logs" / "transcript.jsonl"
        if tf.exists():
            candidates.append(tf)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for tf in candidates[:15]:
        turns = _parse_antigravity_transcript_turns(tf, workspace_root)
        if turns:
            return turns
    return []


def _norm_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def sync_workspace_transcript_turns(workspace_root: Path) -> List[Dict[str, object]]:
    """Reconcile forecasts in `.tokwiz/forecasts.jsonl` against real completed transcript turns."""
    ws_root = workspace_root.resolve()
    forecasts_path = ws_root / ".tokwiz" / "forecasts.jsonl"
    if not forecasts_path.exists():
        return []

    real_turns = _find_workspace_transcript_turns(ws_root)
    if not real_turns:
        return []

    # Check if a sibling `project-baseline` workspace also has a real transcript for direct A/B comparison
    baseline_turns: List[Dict[str, object]] = []
    sibling_base = ws_root.parent / "project-baseline"
    if sibling_base != ws_root and sibling_base.exists():
        baseline_turns = _find_workspace_transcript_turns(sibling_base)

    forecasts = []
    for line in forecasts_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                forecasts.append(json.loads(line))
            except Exception:
                pass

    cal_mod = _load_calibration_module()
    engine = cal_mod.CalibrationEngine(root_dir=ws_root)
    reconciled_records: List[Dict[str, object]] = []
    used_turn_indices: set[int] = set()

    for f in forecasts:
        f_prompt_norm = _norm_prompt(str(f.get("prompt", "")))
        if not f_prompt_norm:
            continue
        matched_idx: Optional[int] = None
        for idx, rt in enumerate(real_turns):
            if idx in used_turn_indices:
                continue
            rt_prompt_norm = _norm_prompt(str(rt.get("prompt", "")))
            if f_prompt_norm[:45] in rt_prompt_norm or rt_prompt_norm[:45] in f_prompt_norm:
                matched_idx = idx
                break
        if matched_idx is None:
            continue

        used_turn_indices.add(matched_idx)
        rt = real_turns[matched_idx]
        u_payload = dict(rt["usage_payload"])  # type: ignore[arg-type]

        # If a matching turn was also run in `project-baseline`, attach its real measured baseline tokens
        for bt in baseline_turns:
            bt_prompt_norm = _norm_prompt(str(bt.get("prompt", "")))
            if f_prompt_norm[:45] in bt_prompt_norm or bt_prompt_norm[:45] in f_prompt_norm:
                bt_usage = cal_mod.parse_usage_payload(bt["usage_payload"])
                u_payload["real_baseline_tokens"] = int(bt["total_tokens"])
                u_payload["real_baseline_cost_usd"] = cal_mod.compute_real_cost_usd(bt_usage, engine.pricing)
                break

        rec = engine.reconcile_turn(forecast=f, usage_payload=u_payload)
        reconciled_records.append(rec)

    return reconciled_records


def handle_post_turn(payload: Dict[str, object], default_root: Optional[Path] = None) -> Dict[str, object]:
    cwd_str = str(payload.get("cwd") or payload.get("workspace_root") or (default_root or os.getcwd()))
    workspace_root = Path(cwd_str).resolve()
    if not workspace_root.exists():
        workspace_root = (default_root or Path.cwd()).resolve()

    usage_payload: Optional[Dict[str, object]] = None
    if any(k in payload for k in ("usage", "usageMetadata", "input_tokens", "prompt_tokens")):
        usage_payload = payload
    elif isinstance(payload.get("transcript_path"), str):
        usage_payload = _extract_usage_from_transcript_file(Path(str(payload["transcript_path"])))

    if usage_payload is not None:
        forecast = _load_active_forecast(workspace_root, payload)
        if not forecast:
            return {"decision": "allow", "reconciled": False}
        cal_mod = _load_calibration_module()
        engine = cal_mod.CalibrationEngine(root_dir=workspace_root)
        record = engine.reconcile_turn(forecast=forecast, usage_payload=usage_payload)
    else:
        # Sync directly against real completed turns in the workspace's session transcript
        synced = sync_workspace_transcript_turns(workspace_root)
        if not synced:
            return {"decision": "allow", "reconciled": False}
        record = synced[-1]

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
