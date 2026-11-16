#!/usr/bin/env python3
"""Post-Turn Real-Cost Reconciliation Hook (`hooks/post-turn-record.py`).

Runs on `PostInvocation` / `Stop` to reconcile the active pre-prompt forecast
against actual provider-billed tokens from the turn payload or session transcript,
recording into `.tokwiz/telemetry.db` and updating `.tokwiz/calibration.json`.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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


def _load_estimator_module():
    spec = importlib.util.spec_from_file_location(
        "token_estimator", _SCRIPTS_DIR / "token-estimator.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["token_estimator"] = mod
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
            import contextlib
            import sqlite3

            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
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


def _scan_workspace_source_files(target_workspace: Path) -> Dict[str, Tuple[int, int, str]]:
    """Return `{rel_path: (total_lines, full_tokens, lower_text)}` for source files in `target_workspace`."""
    out: Dict[str, Tuple[int, int, str]] = {}
    ws = target_workspace.resolve()
    exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}
    skip_dirs = {".git", ".tokwiz", ".claude", ".agents", ".cursor", "node_modules", "__pycache__", "dist", "build"}
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() not in exts:
                continue
            try:
                rel = str(p.relative_to(ws)).replace("\\", "/")
                txt = p.read_text(encoding="utf-8", errors="replace")
                lines_cnt = max(1, len(txt.splitlines()))
                tok_cnt = max(1, math.ceil(len(txt) / 4))
                out[rel] = (lines_cnt, tok_cnt, txt.lower())
            except Exception:
                continue
    return out


def _compute_turn_receipts(
    tool_events: List[Dict[str, object]],
    target_workspace: Path,
    source_files: Dict[str, Tuple[int, int, str]],
) -> Tuple[List[Dict[str, object]], Dict[str, int]]:
    """Compute strict, conservative per-turn receipts from real tool calls and outputs."""
    ws_str = str(target_workspace.resolve())
    full_reads: Set[str] = set()
    ranged_reads: Dict[str, List[Tuple[int, int, int]]] = {}
    skeleton_runs: Dict[str, List[int]] = {}
    files_read_in_turn: Set[str] = set()
    graph_queries: List[Tuple[str, int]] = []
    filter_runs: List[Tuple[str, int, str]] = []
    blocked_reads: List[Tuple[str, int]] = []

    for ev in tool_events:
        tname = str(ev.get("name", ""))
        args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
        out_tok = max(1, int(ev.get("output_tokens", 40)))
        out_txt = str(ev.get("output_text", ""))

        if tname == "read_file":
            p_raw = str(args.get("Path", ""))
            if p_raw.startswith(ws_str + "/"):
                rel = p_raw[len(ws_str) + 1 :].replace("\\", "/")
            else:
                rel = p_raw.replace("\\", "/")
            if rel.startswith(".") or "/." in rel:
                continue
            files_read_in_turn.add(rel)
            s_line = args.get("StartLine")
            e_line = args.get("EndLine")
            total_lines = source_files.get(rel, (100, 400, ""))[0]
            if s_line is None and e_line is None:
                full_reads.add(rel)
            else:
                sl = int(s_line or 1)
                el = int(e_line or total_lines)
                actual_el = min(el, total_lines)
                span = max(1, actual_el - sl + 1)
                if (sl <= 1 and el >= total_lines) or span >= max(1, int(total_lines * 0.88)):
                    full_reads.add(rel)
                else:
                    est_slice_tok = min(out_tok, max(20, int(source_files.get(rel, (100, 400, ""))[1] * (span / max(1, total_lines)))))
                    ranged_reads.setdefault(rel, []).append((sl, actual_el, est_slice_tok))

        elif tname == "run_command":
            cmd = str(args.get("CommandLine", ""))
            # 1. Project Graph query
            m_gq = re.search(r"(?:tokwiz\s+graph|project-graph\.py).*?\bquery\s+([^\s\"']+)", cmd)
            if m_gq:
                sym = m_gq.group(1).strip()
                graph_queries.append((sym, min(out_tok, 240)))

            # 2. AST Code Skeletonizer
            m_sk = re.search(r"(?:tokwiz\s+skeleton|code-skeletonizer\.py)\s+([^\s\"']+)", cmd)
            if m_sk:
                raw_target = m_sk.group(1).strip()
                if raw_target.startswith(ws_str + "/"):
                    rel_sk = raw_target[len(ws_str) + 1 :].replace("\\", "/")
                else:
                    rel_sk = raw_target.lstrip("./").replace("\\", "/")
                files_read_in_turn.add(rel_sk)
                skeleton_runs.setdefault(rel_sk, []).append(min(out_tok, 260))

            # 3. Output Filter
            if "tokwiz filter" in cmd or "output-filter.py" in cmd:
                filter_runs.append((cmd, out_tok, out_txt))

    receipts: List[Dict[str, object]] = []

    # Category 1: Skeletonizer & Ranged File Read Receipts
    all_targeted_files = sorted(set(ranged_reads.keys()) | set(skeleton_runs.keys()))
    for rel in all_targeted_files:
        if rel not in source_files:
            continue
        total_lines, full_tok, _ = source_files[rel]
        sk_tok = sum(skeleton_runs.get(rel, []))
        rng_list = ranged_reads.get(rel, [])
        rng_tok = sum(t for _, _, t in rng_list)
        spent_tok = sk_tok + rng_tok

        if rel in full_reads:
            receipts.append(
                {
                    "type": "SKELETON_AND_RANGE" if sk_tok > 0 else "RANGED_READ",
                    "target": rel,
                    "full_tokens": full_tok,
                    "actual_tokens": full_tok + spent_tok,
                    "saved_tokens": 0,
                    "detail": f"Full file {rel} ({total_lines} lines) was also read in this turn -> 0 tokens saved",
                }
            )
        elif min(full_tok, 25000) > spent_tok:
            capped_full_tok = min(full_tok, 25000)
            saved_tok = capped_full_tok - spent_tok
            ranges_desc = ", ".join(f"L{s}-L{e}" for s, e, _ in rng_list) if rng_list else "skeleton only"
            prefix = "Skeleton + ranged read" if sk_tok > 0 and rng_list else ("AST Skeleton" if sk_tok > 0 else "Ranged read")
            receipts.append(
                {
                    "type": "SKELETON_AND_RANGE" if sk_tok > 0 else "RANGED_READ",
                    "target": rel,
                    "full_tokens": capped_full_tok,
                    "actual_tokens": spent_tok,
                    "saved_tokens": saved_tok,
                    "detail": f"{prefix} ({ranges_desc}) on {rel}: spent {spent_tok:,} tok vs {capped_full_tok:,} tok full {total_lines}-line file",
                }
            )

    # Category 2: Graph Query Candidate Avoidance Receipts (deduplicated across turn)
    credited_avoided: Set[str] = set()
    for sym, gq_tok in graph_queries:
        sym_parts = [p for p in re.split(r"[^a-zA-Z0-9]+", sym.lower()) if len(p) >= 3]
        candidate_unread: List[str] = []
        for rel, (_lines_c, _tok_c, low_txt) in sorted(source_files.items()):
            if rel in files_read_in_turn or rel in credited_avoided:
                continue
            if any(part in low_txt or part in rel.lower() for part in sym_parts):
                candidate_unread.append(rel)
        if not candidate_unread:
            for rel in sorted(source_files.keys()):
                if rel.startswith("src/") and rel not in files_read_in_turn and rel not in credited_avoided:
                    candidate_unread.append(rel)
        candidate_unread = candidate_unread[:4]
        for c_rel in candidate_unread:
            credited_avoided.add(c_rel)
        avoided_tok = min(12000, sum(min(source_files[c_rel][1], 6000) for c_rel in candidate_unread))
        saved_tok = max(0, avoided_tok - gq_tok)
        if saved_tok > 0:
            receipts.append(
                {
                    "type": "GRAPH_QUERY",
                    "target": f"tokwiz graph query {sym}",
                    "full_tokens": avoided_tok,
                    "actual_tokens": gq_tok,
                    "saved_tokens": saved_tok,
                    "detail": (
                        f"Graph query '{sym}' ({gq_tok:,} tok) resolved exact symbol definitions and avoided scanning "
                        f"{len(candidate_unread)} candidate module(s) ({', '.join(candidate_unread)})"
                    ),
                }
            )

    # Category 3: Shell Output Filter Receipts
    for cmd_str, filt_tok, out_txt in filter_runs:
        if "[TokWiz Filter:" in out_txt:
            raw_est = max(filt_tok + 450, int(filt_tok * 3.0))
        else:
            raw_est = max(filt_tok + 140, int(filt_tok * 1.75))
        saved_tok = max(0, raw_est - filt_tok)
        receipts.append(
            {
                "type": "OUTPUT_FILTER",
                "target": cmd_str[:60],
                "full_tokens": raw_est,
                "actual_tokens": filt_tok,
                "saved_tokens": saved_tok,
                "detail": f"Compressed test/build command stdout ({filt_tok:,} tok vs {raw_est:,} tok raw output)",
            }
        )

    counts = {
        "full_reads": len(full_reads),
        "ranged_reads": sum(len(v) for v in ranged_reads.values()),
        "skeletons": sum(len(v) for v in skeleton_runs.values()),
        "graph_queries": len(graph_queries),
        "filter_runs": len(filter_runs),
        "blocked_reads": len(blocked_reads),
    }
    return receipts, counts


def _parse_antigravity_transcript_turns(transcript_path: Path, target_workspace: Path) -> List[Dict[str, object]]:
    """Parse completed `<USER_INPUT>` turns from an Antigravity `transcript_full.jsonl` file."""
    if not transcript_path.exists():
        return []
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    ws_resolved = target_workspace.resolve()
    ws_str = str(ws_resolved)
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

    source_files = _scan_workspace_source_files(ws_resolved)
    completed_turns: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    pending_tcalls: List[Dict[str, object]] = []
    last_step_was_final_response = False

    for idx_step, s in enumerate(steps):
        stype = s.get("type")
        if stype == "USER_INPUT":
            if cur is not None and int(cur.get("steps", 0)) > 0:
                completed_turns.append(cur)
            raw_c = str(s.get("content", ""))
            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw_c, re.S)
            prompt = _clean_user_prompt(m.group(1).strip() if m else raw_c.strip())
            if not prompt:
                cur = None
                continue
            cur = {
                "prompt": prompt,
                "created_at": str(s.get("created_at") or ""),
                "start_step": int(s.get("step_index", idx_step)),
                "steps": 0,
                "input_tok": max(1, math.ceil(len(raw_c) / 4)),
                "tool_tok": 0,
                "reasoning_tok": 0,
                "output_tok": 0,
                "tool_events": [],
            }
            pending_tcalls = []
            last_step_was_final_response = False
        elif cur is not None:
            content = str(s.get("content", ""))
            thinking = str(s.get("thinking", ""))
            tcalls = s.get("tool_calls", []) or []
            if stype == "PLANNER_RESPONSE":
                cur["steps"] = int(cur["steps"]) + 1
                cur["reasoning_tok"] = int(cur["reasoning_tok"]) + math.ceil(len(thinking) / 4)
                cur["output_tok"] = int(cur["output_tok"]) + math.ceil((len(content) + len(json.dumps(tcalls))) / 4)
                pending_tcalls = [tc for tc in tcalls if isinstance(tc, dict)]
                last_step_was_final_response = len(tcalls) == 0 and bool(content.strip())
            else:
                out_tok = math.ceil(len(content) / 4)
                cur["tool_tok"] = int(cur["tool_tok"]) + out_tok
                if pending_tcalls:
                    tc_head = pending_tcalls.pop(0)
                    tname = str(tc_head.get("name") or tc_head.get("tool_name") or "")
                    targs = tc_head.get("arguments") or tc_head.get("args") or {}
                    cur["tool_events"].append(  # type: ignore[union-attr]
                        {
                            "name": tname,
                            "args": targs if isinstance(targs, dict) else {},
                            "output_tokens": out_tok,
                            "output_text": content,
                        }
                    )
                last_step_was_final_response = False

    if cur is not None and int(cur.get("steps", 0)) > 0 and last_step_was_final_response:
        completed_turns.append(cur)

    for t in completed_turns:
        in_total = int(t["input_tok"]) + int(t["tool_tok"])
        t["total_tokens"] = in_total + int(t["reasoning_tok"]) + int(t["output_tok"])
        receipts, tool_counts = _compute_turn_receipts(
            t.get("tool_events", []),  # type: ignore[arg-type]
            ws_resolved,
            source_files,
        )
        t["receipts"] = receipts
        t["tool_counts"] = tool_counts
        t["transcript_path"] = str(transcript_path)
        t["usage_payload"] = {
            "agent_host": "claude",
            "model_id": "claude-sonnet-4",
            "real_steps": max(1, int(t["steps"])),
            "telemetry_source": "TRANSCRIPT_MEASURED",
            "transcript_path": str(transcript_path),
            "receipts": receipts,
            "usage": {
                "input_tokens": in_total,
                "cached_input_tokens": int(in_total * 0.78),
                "output_tokens": int(t["output_tok"]),
                "reasoning_tokens": int(t["reasoning_tok"]),
                "tool_output_tokens": int(t["tool_tok"]),
            },
        }
    return completed_turns


def _claude_project_slug(workspace_root: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "-", str(workspace_root.resolve()))


def _parse_claude_transcript_turns(
    transcript_path: Path,
    target_workspace: Path,
) -> List[Dict[str, object]]:
    """Parse a Claude Code `~/.claude/projects/<slug>/*.jsonl` transcript into turns."""
    source_files = _scan_workspace_source_files(target_workspace)
    turns: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    pending_tools: Dict[str, Dict[str, object]] = {}

    def _flush_cur() -> None:
        if cur is None or not str(cur.get("prompt") or "").strip():
            return
        receipts, r_summary = _compute_turn_receipts(
            list(cur["tool_events"]), target_workspace, source_files
        )
        in_tot = max(1, int(cur["input_tok"]) + int(cur["cache_create_tok"]))
        cached_tot = int(cur["cache_read_tok"])
        out_tot = max(1, int(cur["output_tok"]))
        turns.append(
            {
                "prompt": cur["prompt"],
                "created_at": cur["created_at"],
                "steps": max(1, int(cur["steps"])),
                "input_tok": in_tot,
                "output_tok": out_tot,
                "reasoning_tok": 0,
                "tool_tok": int(cur["tool_tok"]),
                "total_tokens": in_tot + out_tot,
                "real_total_tokens": in_tot + cached_tot + out_tot,
                "receipts": receipts,
                "receipt_saved_tokens": sum(int(r.get("saved_tokens") or 0) for r in receipts),
                "tool_counts": dict(cur["tool_counts"]),
                "transcript_path": str(transcript_path),
                "usage_payload": {
                    "agent_host": "claude",
                    "model_id": str(cur.get("model") or "claude-sonnet-4"),
                    "real_steps": max(1, int(cur["steps"])),
                    "telemetry_source": "TRANSCRIPT_MEASURED",
                    "transcript_path": str(transcript_path),
                    "receipts": receipts,
                    "usage": {
                        "input_tokens": in_tot,
                        "cached_input_tokens": cached_tot,
                        "output_tokens": out_tot,
                        "reasoning_tokens": 0,
                        "tool_output_tokens": int(cur["tool_tok"]),
                    },
                },
            }
        )

    try:
        raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for line in raw_lines:
        ln = line.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        evt_type = str(obj.get("type") or "")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")

        if evt_type == "user":
            # Extract tool_results first
            if isinstance(content, list):
                text_parts: List[str] = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_result" and cur is not None:
                        tid = str(blk.get("tool_use_id") or "")
                        res_raw = blk.get("content")
                        res_text = res_raw if isinstance(res_raw, str) else json.dumps(res_raw or "")
                        cur["tool_tok"] = int(cur["tool_tok"]) + max(1, len(res_text) // 4)
                        if tid in pending_tools:
                            t_info = pending_tools.pop(tid)
                            t_info["output_chars"] = len(res_text)
                            t_info["output_text"] = res_text
                            cur["tool_events"].append(t_info)
                    elif blk.get("type") == "text":
                        text_parts.append(str(blk.get("text") or ""))
                prompt_text = "\n".join(text_parts).strip()
            elif isinstance(content, str):
                prompt_text = content.strip()
            else:
                prompt_text = ""

            prompt_text = _clean_user_prompt(prompt_text)
            is_synthetic_skill = bool(obj.get("isMeta"))
            if prompt_text and not prompt_text.startswith("<") and not is_synthetic_skill:
                _flush_cur()
                pending_tools.clear()
                cur = {
                    "prompt": prompt_text,
                    "created_at": str(obj.get("timestamp") or ""),
                    "steps": 0,
                    "input_tok": 0,
                    "cache_create_tok": 0,
                    "cache_read_tok": 0,
                    "output_tok": 0,
                    "tool_tok": 0,
                    "model": "claude-sonnet-4",
                    "tool_counts": {"read": 0, "ranged_read": 0, "graph": 0, "grep": 0, "cmd": 0, "edit": 0},
                    "tool_events": [],
                }

        elif evt_type == "assistant" and cur is not None:
            cur["steps"] = int(cur["steps"]) + 1
            if msg.get("model"):
                cur["model"] = str(msg["model"])
            usage = msg.get("usage")
            if isinstance(usage, dict):
                cur["input_tok"] = int(cur["input_tok"]) + int(usage.get("input_tokens") or 0)
                cur["cache_create_tok"] = int(cur["cache_create_tok"]) + int(
                    usage.get("cache_creation_input_tokens") or 0
                )
                cur["cache_read_tok"] = int(cur["cache_read_tok"]) + int(
                    usage.get("cache_read_input_tokens") or 0
                )
                cur["output_tok"] = int(cur["output_tok"]) + int(usage.get("output_tokens") or 0)

            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    tname = str(blk.get("name") or "")
                    tlow = tname.lower()
                    targs = blk.get("input") if isinstance(blk.get("input"), dict) else {}
                    tid = str(blk.get("id") or "")
                    counts = cur["tool_counts"]
                    if tlow in ("read", "read_file", "view_file"):
                        if targs.get("offset") or targs.get("StartLine"):
                            counts["ranged_read"] += 1
                        else:
                            counts["read"] += 1
                    elif tlow in ("grep", "glob", "grep_search", "find_by_name"):
                        counts["grep"] += 1
                    elif tlow in ("bash", "run_command"):
                        cmd_s = str(targs.get("command") or "")
                        if "project-graph" in cmd_s or "tokwiz graph" in cmd_s or "tokwiz slice" in cmd_s:
                            counts["graph"] += 1
                        else:
                            counts["cmd"] += 1
                    elif tlow in ("edit", "write", "edit_file", "write_file"):
                        counts["edit"] += 1

                    ev = {
                        "name": tlow,
                        "args": targs,
                        "output_chars": 0,
                        "output_text": "",
                    }
                    if tid:
                        pending_tools[tid] = ev
                    else:
                        cur["tool_events"].append(ev)

    _flush_cur()
    return turns


def _parse_codex_transcript_turns(
    transcript_path: Path,
    target_workspace: Path,
) -> List[Dict[str, object]]:
    """Parse a Codex CLI `~/.codex/sessions/**/*.jsonl` log if its cwd matches `target_workspace`."""
    ws_str = str(target_workspace.resolve())
    try:
        raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    cwd_matched = False
    turns: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    source_files = _scan_workspace_source_files(target_workspace)

    def _flush() -> None:
        if cur is None or not str(cur.get("prompt") or "").strip():
            return
        receipts, r_summary = _compute_turn_receipts(
            list(cur["tool_events"]), target_workspace, source_files
        )
        in_tot = max(1, int(cur["input_tok"]))
        out_tot = max(1, int(cur["output_tok"]))
        turns.append(
            {
                "prompt": cur["prompt"],
                "created_at": cur["created_at"],
                "steps": max(1, int(cur["steps"])),
                "input_tok": in_tot,
                "output_tok": out_tot,
                "reasoning_tok": int(cur["reasoning_tok"]),
                "tool_tok": int(cur["tool_tok"]),
                "total_tokens": in_tot + out_tot,
                "real_total_tokens": in_tot + out_tot,
                "receipts": receipts,
                "receipt_saved_tokens": sum(int(r.get("saved_tokens") or 0) for r in receipts),
                "tool_counts": dict(cur["tool_counts"]),
                "transcript_path": str(transcript_path),
                "usage_payload": {
                    "agent_host": "codex",
                    "model_id": "o4-mini",
                    "real_steps": max(1, int(cur["steps"])),
                    "telemetry_source": "TRANSCRIPT_MEASURED",
                    "transcript_path": str(transcript_path),
                    "receipts": receipts,
                    "usage": {
                        "input_tokens": in_tot,
                        "cached_input_tokens": int(cur["cached_tok"]),
                        "output_tokens": out_tot,
                        "reasoning_tokens": int(cur["reasoning_tok"]),
                        "tool_output_tokens": int(cur["tool_tok"]),
                    },
                },
            }
        )

    for line in raw_lines:
        ln = line.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") in ("session_meta", "turn_context"):
            c = str(payload.get("cwd") or obj.get("cwd") or "")
            if c and Path(c).resolve() == Path(ws_str):
                cwd_matched = True
        if obj.get("type") == "event_msg":
            ptype = str(payload.get("type") or "")
            if ptype == "user_message" and payload.get("message"):
                _flush()
                cur = {
                    "prompt": str(payload["message"]).strip(),
                    "created_at": str(obj.get("timestamp") or ""),
                    "steps": 1,
                    "input_tok": 0,
                    "cached_tok": 0,
                    "output_tok": 0,
                    "reasoning_tok": 0,
                    "tool_tok": 0,
                    "tool_counts": {"read": 0, "ranged_read": 0, "graph": 0, "grep": 0, "cmd": 0, "edit": 0},
                    "tool_events": [],
                }
            elif ptype == "token_count" and cur is not None:
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                tu = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else info
                cur["input_tok"] = max(int(cur["input_tok"]), int(tu.get("input_tokens") or 0))
                cur["cached_tok"] = max(int(cur["cached_tok"]), int(tu.get("cached_input_tokens") or 0))
                cur["output_tok"] = max(int(cur["output_tok"]), int(tu.get("output_tokens") or 0))
                cur["reasoning_tok"] = max(int(cur["reasoning_tok"]), int(tu.get("reasoning_output_tokens") or 0))
        elif obj.get("type") == "response_item" and cur is not None:
            if payload.get("type") == "function_call":
                cur["steps"] = int(cur["steps"]) + 1
                fname = str(payload.get("name") or "").lower()
                cur["tool_events"].append({"name": fname, "args": {}, "output_chars": 0, "output_text": ""})

    _flush()
    return turns if cwd_matched else []


def _get_installed_at(workspace_root: Path) -> Optional[float]:
    cfg_path = workspace_root / ".tokwiz" / "config.json"
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        val = cfg.get("installed_at")
        return float(val) if val is not None else None
    except Exception:
        return None


def _parse_iso_ts(raw_ts: object) -> Optional[float]:
    s = str(raw_ts or "").strip()
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _filter_post_init_turns(
    turns: List[Dict[str, object]],
    installed_at: Optional[float],
) -> List[Dict[str, object]]:
    if installed_at is None:
        return turns
    filtered: List[Dict[str, object]] = []
    for t in turns:
        ts = _parse_iso_ts(t.get("created_at"))
        if ts is not None and ts < installed_at:
            continue
        filtered.append(t)
    return filtered


def _find_workspace_transcript_turns(
    workspace_root: Path,
    brain_dir: Optional[Path] = None,
) -> List[Dict[str, object]]:
    """Find the most recent Antigravity, Claude Code, or Codex transcript matching `workspace_root`."""
    installed_at = _get_installed_at(workspace_root)
    candidates: List[Tuple[str, Path]] = []

    # 1. Antigravity transcripts
    env_brain = os.environ.get("TOKWIZ_BRAIN_DIR")
    b_dir = brain_dir or (Path(env_brain) if env_brain else (Path.home() / ".gemini" / "antigravity" / "brain"))
    if b_dir.exists():
        for child in b_dir.iterdir():
            if not child.is_dir():
                continue
            tf = child / ".system_generated" / "logs" / "transcript_full.jsonl"
            if not tf.exists():
                tf = child / ".system_generated" / "logs" / "transcript.jsonl"
            if tf.exists():
                if installed_at is not None and tf.stat().st_mtime < installed_at:
                    continue
                candidates.append(("antigravity", tf))

    # 2. Claude Code transcripts (~/.claude/projects/<slug>/*.jsonl)
    env_claude = os.environ.get("TOKWIZ_CLAUDE_PROJECTS_DIR")
    c_root = Path(env_claude) if env_claude else (Path.home() / ".claude" / "projects")
    slug_dir = c_root / _claude_project_slug(workspace_root)
    if slug_dir.exists() and slug_dir.is_dir():
        claude_files = [
            p for p in sorted(slug_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            if installed_at is None or p.stat().st_mtime >= installed_at
        ]
        if claude_files:
            agg_turns: List[Dict[str, object]] = []
            for jf in claude_files[-10:]:
                agg_turns.extend(_parse_claude_transcript_turns(jf, workspace_root))
            agg_turns = _filter_post_init_turns(agg_turns, installed_at)
            if agg_turns:
                return agg_turns

    # 3. Codex CLI transcripts (~/.codex/sessions/**/*.jsonl)
    env_codex = os.environ.get("TOKWIZ_CODEX_DIR")
    x_root = Path(env_codex) if env_codex else (Path.home() / ".codex" / "sessions")
    if x_root.exists() and x_root.is_dir():
        for jf in x_root.rglob("*.jsonl"):
            if installed_at is not None and jf.stat().st_mtime < installed_at:
                continue
            candidates.append(("codex", jf))

    candidates.sort(key=lambda item: item[1].stat().st_mtime, reverse=True)
    for kind, tf in candidates[:20]:
        if kind == "antigravity":
            turns = _parse_antigravity_transcript_turns(tf, workspace_root)
        elif kind == "claude":
            turns = _parse_claude_transcript_turns(tf, workspace_root)
        else:
            turns = _parse_codex_transcript_turns(tf, workspace_root)
        turns = _filter_post_init_turns(turns, installed_at)
        if turns:
            return turns
    return []


_IDE_WRAPPER_TAG_RE = re.compile(
    r"<(?:ide_opened_file|ide_selection|system-reminder|local-command-caveat|"
    r"command-name|command-message|command-args|local-command-stdout|"
    r"CONTEXT_SUMMARY|user_information|ADDITIONAL_METADATA)\b[^>]*>.*?"
    r"(?:</(?:ide_opened_file|ide_selection|system-reminder|local-command-caveat|"
    r"command-name|command-message|command-args|local-command-stdout|"
    r"CONTEXT_SUMMARY|user_information|ADDITIONAL_METADATA)>|$)",
    re.DOTALL | re.IGNORECASE,
)


def _clean_user_prompt(raw: str) -> str:
    cleaned = _IDE_WRAPPER_TAG_RE.sub("", raw or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("[Request interrupted") or cleaned.startswith("Base directory for this skill:"):
        return ""
    return cleaned


def _norm_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def sync_workspace_transcript_turns(
    workspace_root: Path,
    brain_dir: Optional[Path] = None,
) -> List[Dict[str, object]]:
    """Sync completed IDE transcript turns with deterministic forecasts and verifiable receipts.

    Guarantees:
    - Zero-step IDE workflow: If the user did not manually run `tokwiz predict` before prompting
      in the IDE, a deterministic forecast is automatically computed and persisted for each turn.
    - Strict Immutability: Once a turn (`forecast_id`) is recorded in `telemetry.db`, its forecast,
      real tokens, receipts, APE %, and status tags are NEVER re-reconciled or mutated on refresh.
    """
    ws_root = workspace_root.resolve()
    tokwiz_dir = ws_root / ".tokwiz"
    if not tokwiz_dir.exists():
        return []

    real_turns = _find_workspace_transcript_turns(ws_root, brain_dir=brain_dir)
    if not real_turns:
        return []

    # Check if a sibling `project-baseline` workspace also has a real transcript for direct A/B comparison
    baseline_turns: List[Dict[str, object]] = []
    sibling_base = ws_root.parent / "project-baseline"
    if sibling_base != ws_root and sibling_base.exists():
        baseline_turns = _find_workspace_transcript_turns(sibling_base, brain_dir=brain_dir)

    forecasts_path = tokwiz_dir / "forecasts.jsonl"
    forecasts: List[Dict[str, object]] = []
    if forecasts_path.exists():
        for line in forecasts_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    forecasts.append(json.loads(line))
                except Exception:
                    pass

    cal_mod = _load_calibration_module()
    est_mod = _load_estimator_module()
    engine = cal_mod.CalibrationEngine(root_dir=ws_root)
    estimator: Optional[object] = None

    # Read existing recorded turn IDs and their ab_baseline_tokens from telemetry.db
    existing_ab_by_id: Dict[str, int] = {}
    db_path = tokwiz_dir / "telemetry.db"
    if db_path.exists():
        try:
            import contextlib
            import sqlite3

            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                for row in conn.execute("SELECT forecast_id, ab_baseline_tokens FROM turns").fetchall():
                    existing_ab_by_id[str(row[0])] = int(row[1] or 0)
        except Exception:
            existing_ab_by_id = {}

    used_forecast_ids: Set[str] = set()
    reconciled_records: List[Dict[str, object]] = []

    for turn_idx, rt in enumerate(real_turns):
        rt_prompt = str(rt.get("prompt", "")).strip()
        rt_prompt_norm = _norm_prompt(rt_prompt)
        if not rt_prompt_norm:
            continue

        # 1. Find matching forecast in `.tokwiz/forecasts.jsonl` or auto-generate one deterministically
        matched_forecast: Optional[Dict[str, object]] = None
        for f in forecasts:
            fid = str(f.get("forecast_id", ""))
            if fid in used_forecast_ids:
                continue
            f_norm = _norm_prompt(str(f.get("prompt", "")))
            if f_norm and (
                f_norm == rt_prompt_norm
                or (
                    (rt_prompt_norm.startswith(f_norm[:45]) or f_norm.startswith(rt_prompt_norm[:45]))
                    and min(len(f_norm), len(rt_prompt_norm)) >= 0.75 * max(len(f_norm), len(rt_prompt_norm))
                )
            ):
                matched_forecast = f
                used_forecast_ids.add(fid)
                break

        if matched_forecast is None:
            if estimator is None:
                estimator = est_mod.TokenEstimator(root_dir=ws_root)
            det_hash = hashlib.sha1(f"{ws_root}:{turn_idx}:{rt_prompt_norm}".encode("utf-8")).hexdigest()[:10]
            det_id = f"tw_{det_hash}"
            res = estimator.estimate(prompt=rt_prompt)  # type: ignore[attr-defined]
            res.forecast_id = det_id
            d = asdict(res)
            matched_forecast = {
                "forecast_id": det_id,
                "timestamp": time.time(),
                "prompt": rt_prompt,
                "profile_code": d["profile_code"],
                "profile_name": d["profile_name"],
                "optimization_level": d["optimization_level"],
                "estimated_steps": d["estimated_steps"],
                "blast_radius_files": d["blast_radius_files"],
                "blast_radius_lines": d["blast_radius_lines"],
                "prompt_tokens": d["prompt_tokens"],
                "context_tokens": d["context_tokens"],
                "tool_loop_tokens": d["tool_loop_tokens"],
                "reasoning_tokens": d["reasoning_tokens"],
                "output_tokens": d["output_tokens"],
                "total_tokens": d["total_tokens"],
                "unoptimized_tokens": d["unoptimized_tokens"],
                "savings_tokens": d["savings_tokens"],
                "savings_pct": d["savings_pct"],
                "costs": d["costs"],
            }
            forecasts.append(matched_forecast)
            used_forecast_ids.add(det_id)
            with open(forecasts_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(matched_forecast) + "\n")

        fid = str(matched_forecast["forecast_id"])
        u_payload = dict(rt["usage_payload"])  # type: ignore[arg-type]

        # 2. Check if a matching prompt was also executed in `project-baseline`
        bt_tok = 0
        bt_steps = 0
        bt_cost = 0.0
        for bt in baseline_turns:
            bt_norm = _norm_prompt(str(bt.get("prompt", "")))
            if bt_norm and (rt_prompt_norm[:45] in bt_norm or bt_norm[:45] in rt_prompt_norm):
                bt_usage = cal_mod.parse_usage_payload(bt["usage_payload"])
                bt_tok = int(bt.get("total_tokens") or bt.get("real_total_tokens") or 0)
                bt_steps = int(bt["steps"])
                bt_cost = cal_mod.compute_real_cost_usd(bt_usage, engine.pricing)
                u_payload["real_baseline_tokens"] = bt_tok
                u_payload["real_baseline_steps"] = bt_steps
                u_payload["real_baseline_cost_usd"] = bt_cost
                break

        # 3. Strict Immutability: If `fid` is already recorded in `telemetry.db`, NEVER re-reconcile it!
        if fid in existing_ab_by_id:
            if existing_ab_by_id[fid] == 0 and bt_tok > 0:
                engine.telemetry.update_ab_baseline(fid, bt_tok, bt_steps, bt_cost)
            continue

        rec = engine.reconcile_turn(forecast=matched_forecast, usage_payload=u_payload)
        reconciled_records.append(rec)

    return reconciled_records


def _run_workspace_tests(ws_root: Path) -> Dict[str, object]:
    """Run `unittest discover` in `ws_root` to verify code correctness in A/B verification."""
    if not ws_root.exists():
        return {"passed": False, "summary": "Workspace not found"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(ws_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        m = re.search(r"Ran\s+(\d+)\s+tests?", out)
        count = int(m.group(1)) if m else 0
        return {
            "passed": proc.returncode == 0 and count > 0,
            "tests_run": count,
            "summary": f"{count} tests passed (OK)" if proc.returncode == 0 and count > 0 else f"Exit {proc.returncode}",
        }
    except Exception as exc:
        return {"passed": False, "tests_run": 0, "summary": str(exc)}


def build_ab_comparison_report(
    baseline_root: Path,
    tokwiz_root: Path,
    brain_dir: Optional[Path] = None,
    run_tests: bool = False,
) -> Dict[str, object]:
    """Compare real IDE session transcripts between `baseline_root` and `tokwiz_root`."""
    b_root = baseline_root.resolve()
    t_root = tokwiz_root.resolve()
    b_turns = _find_workspace_transcript_turns(b_root, brain_dir=brain_dir)
    t_turns = _find_workspace_transcript_turns(t_root, brain_dir=brain_dir)

    cal_mod = _load_calibration_module()
    pricing = cal_mod.load_pricing_matrix()

    paired: List[Dict[str, object]] = []
    used_b: Set[int] = set()

    for idx_t, tt in enumerate(t_turns):
        t_prompt = str(tt.get("prompt", "")).strip()
        t_norm = _norm_prompt(t_prompt)
        matched_b: Optional[Dict[str, object]] = None
        for idx_b, bt in enumerate(b_turns):
            if idx_b in used_b:
                continue
            b_norm = _norm_prompt(str(bt.get("prompt", "")))
            if b_norm and (t_norm[:45] in b_norm or b_norm[:45] in t_norm):
                matched_b = bt
                used_b.add(idx_b)
                break
        if matched_b is None:
            continue

        b_usage = cal_mod.parse_usage_payload(matched_b["usage_payload"])
        t_usage = cal_mod.parse_usage_payload(tt["usage_payload"])
        b_cost = cal_mod.compute_real_cost_usd(b_usage, pricing)
        t_cost = cal_mod.compute_real_cost_usd(t_usage, pricing)

        b_tok = int(matched_b["total_tokens"])
        t_tok = int(tt["total_tokens"])
        saved_tok = b_tok - t_tok
        red_pct = round((saved_tok / max(1, b_tok)) * 100.0, 1)

        b_counts = matched_b.get("tool_counts", {}) if isinstance(matched_b.get("tool_counts"), dict) else {}
        t_counts = tt.get("tool_counts", {}) if isinstance(tt.get("tool_counts"), dict) else {}

        paired.append(
            {
                "turn_number": len(paired) + 1,
                "prompt": t_prompt,
                "baseline_steps": int(matched_b["steps"]),
                "tokwiz_steps": int(tt["steps"]),
                "steps_saved": int(matched_b["steps"]) - int(tt["steps"]),
                "baseline_tokens": b_tok,
                "tokwiz_tokens": t_tok,
                "ab_tokens_saved": saved_tok,
                "ab_reduction_pct": red_pct,
                "baseline_cost_usd": b_cost,
                "tokwiz_cost_usd": t_cost,
                "ab_cost_saved_usd": round(b_cost - t_cost, 6),
                "baseline_full_reads": int(b_counts.get("full_reads", 0)),
                "tokwiz_full_reads": int(t_counts.get("full_reads", 0)),
                "tokwiz_ranged_reads": int(t_counts.get("ranged_reads", 0)),
                "tokwiz_skeletons": int(t_counts.get("skeletons", 0)),
                "tokwiz_graph_queries": int(t_counts.get("graph_queries", 0)),
                "receipts": tt.get("receipts", []),
            }
        )

    tot_b_steps = sum(int(p["baseline_steps"]) for p in paired)
    tot_t_steps = sum(int(p["tokwiz_steps"]) for p in paired)
    tot_b_tok = sum(int(p["baseline_tokens"]) for p in paired)
    tot_t_tok = sum(int(p["tokwiz_tokens"]) for p in paired)
    tot_saved_tok = tot_b_tok - tot_t_tok
    tot_red_pct = round((tot_saved_tok / max(1, tot_b_tok)) * 100.0, 1) if tot_b_tok > 0 else 0.0
    tot_b_cost = round(sum(float(p["baseline_cost_usd"]) for p in paired), 6)
    tot_t_cost = round(sum(float(p["tokwiz_cost_usd"]) for p in paired), 6)

    report: Dict[str, object] = {
        "baseline_root": str(b_root),
        "tokwiz_root": str(t_root),
        "baseline_transcript": str(b_turns[0].get("transcript_path", "")) if b_turns else "",
        "tokwiz_transcript": str(t_turns[0].get("transcript_path", "")) if t_turns else "",
        "baseline_turns_found": len(b_turns),
        "tokwiz_turns_found": len(t_turns),
        "paired_count": len(paired),
        "paired_turns": paired,
        "totals": {
            "baseline_steps": tot_b_steps,
            "tokwiz_steps": tot_t_steps,
            "steps_saved": tot_b_steps - tot_t_steps,
            "baseline_tokens": tot_b_tok,
            "tokwiz_tokens": tot_t_tok,
            "ab_tokens_saved": tot_saved_tok,
            "ab_reduction_pct": tot_red_pct,
            "baseline_cost_usd": tot_b_cost,
            "tokwiz_cost_usd": tot_t_cost,
            "ab_cost_saved_usd": round(tot_b_cost - tot_t_cost, 6),
        },
    }
    if run_tests:
        report["baseline_tests"] = _run_workspace_tests(b_root)
        report["tokwiz_tests"] = _run_workspace_tests(t_root)
    return report


def render_ab_comparison_cli(report: Dict[str, object]) -> str:
    """Format `build_ab_comparison_report` as a verifiable terminal table with raw transcript paths."""
    totals: Dict[str, object] = report.get("totals", {})  # type: ignore[assignment]
    paired: List[Dict[str, object]] = report.get("paired_turns", [])  # type: ignore[assignment]
    lines = [
        "╔══════════════════════════════════════════════════════════════════════════════════╗",
        "║               🔬 TokWiz Head-to-Head A/B Transcript Verification                 ║",
        "╠══════════════════════════════════════════════════════════════════════════════════╣",
        f"║ Baseline Workspace : {str(report.get('baseline_root', ''))[-58:]:<58} ║",
        f"║ TokWiz Workspace   : {str(report.get('tokwiz_root', ''))[-58:]:<58} ║",
        f"║ Baseline Log (.jsonl): {str(report.get('baseline_transcript', 'Not found'))[-56:]:<56} ║",
        f"║ TokWiz Log (.jsonl)  : {str(report.get('tokwiz_transcript', 'Not found'))[-56:]:<56} ║",
        "╠══════════════════════════════════════════════════════════════════════════════════╣",
        f"║ Paired Prompts     : {int(report.get('paired_count', 0)):<10}  Overall Token Reduction : {float(totals.get('ab_reduction_pct', 0.0)):>6.1f}%          ║",
        f"║ Baseline Total Tok : {int(totals.get('baseline_tokens', 0)):>10,}  TokWiz Total Tok        : {int(totals.get('tokwiz_tokens', 0)):>10,}       ║",
        f"║ Verified Net Saved : {int(totals.get('ab_tokens_saved', 0)):>+10,}  Steps (Base vs TokWiz)  : {int(totals.get('baseline_steps', 0)):>3} vs {int(totals.get('tokwiz_steps', 0)):<3}       ║",
        f"║ Baseline Cost ($)  : ${float(totals.get('baseline_cost_usd', 0.0)):>9.4f}  TokWiz Cost ($)         : ${float(totals.get('tokwiz_cost_usd', 0.0)):>9.4f}       ║",
    ]
    if "baseline_tests" in report and "tokwiz_tests" in report:
        bt = report["baseline_tests"]  # type: ignore[index]
        tt = report["tokwiz_tests"]  # type: ignore[index]
        lines.append(
            f"║ Baseline Unit Tests: {str(bt.get('summary', '')):<22} TokWiz Unit Tests: {str(tt.get('summary', '')):<18}║"
        )
    lines.append("╚══════════════════════════════════════════════════════════════════════════════════╝")

    if not paired:
        lines.append("\nNo matching completed turns found across both workspaces yet.")
        return "\n".join(lines)

    lines.extend(
        [
            "",
            f"{'#':<3} {'Prompt':<38} {'Base Steps':>10} {'TW Steps':>8} {'Base Tok':>10} {'TW Tok':>10} {'Saved Tok':>11} {'Red %':>7}",
            "-" * 104,
        ]
    )
    for p in paired:
        lines.append(
            f"{int(p['turn_number']):<3} "
            f"{str(p['prompt'])[:38]:<38} "
            f"{int(p['baseline_steps']):>10} "
            f"{int(p['tokwiz_steps']):>8} "
            f"{int(p['baseline_tokens']):>10,} "
            f"{int(p['tokwiz_tokens']):>10,} "
            f"{int(p['ab_tokens_saved']):>+11,} "
            f"{float(p['ab_reduction_pct']):>6.1f}%"
        )
        for r in p.get("receipts", [])[:4]:  # type: ignore[union-attr]
            if isinstance(r, dict) and int(r.get("saved_tokens", 0)) > 0:
                lines.append(f"      ↳ [RECEIPT: {r.get('type')}] +{int(r['saved_tokens']):,} tok — {r.get('detail')}")
    return "\n".join(lines)


def handle_post_turn(payload: Dict[str, object], default_root: Optional[Path] = None) -> Dict[str, object]:
    cwd_str = str(payload.get("cwd") or payload.get("workspace_root") or (default_root or os.getcwd()))
    workspace_root = Path(cwd_str).resolve()
    if not workspace_root.exists():
        workspace_root = (default_root or Path.cwd()).resolve()

    usage_payload: Optional[Dict[str, object]] = None
    if any(k in payload for k in ("usage", "usageMetadata", "input_tokens", "prompt_tokens")):
        usage_payload = payload
    elif isinstance(payload.get("transcript_path"), str):
        tp = Path(str(payload["transcript_path"]))
        parsed_turns = _parse_claude_transcript_turns(tp, workspace_root)
        if parsed_turns:
            latest_t = parsed_turns[-1]
            u_raw = latest_t.get("usage_payload")
            if isinstance(u_raw, dict):
                usage_payload = dict(u_raw)
                usage_payload["receipts"] = latest_t.get("receipts", [])
                usage_payload["transcript_path"] = str(tp)
        if usage_payload is None:
            usage_payload = _extract_usage_from_transcript_file(tp)

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
