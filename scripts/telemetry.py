#!/usr/bin/env python3
"""Telemetry Ledger & CLI Statistics Reporter for TokWiz (`scripts/telemetry.py`).

Records and queries Forecasted vs. Real billed tokens & cost, MAPE accuracy,
root-cause variance tags, KV-cache efficiency, and net token savings in
`.tokwiz/telemetry.db`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional


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


class TelemetryStore:
    """SQLite ledger storing per-turn forecast vs. real reconciliation and guardrail metrics."""

    def __init__(self, root_dir: Path | str = ".", db_path: Optional[Path | str] = None) -> None:
        self.root_dir = _safe_resolve(root_dir)
        self.db_path = _safe_resolve(db_path) if db_path else (self.root_dir / ".tokwiz" / "telemetry.db")
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
                CREATE TABLE IF NOT EXISTS turns (
                    forecast_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    agent_host TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    profile_code TEXT NOT NULL,
                    optimization_level INTEGER NOT NULL,
                    forecast_steps INTEGER NOT NULL,
                    real_steps INTEGER NOT NULL,
                    forecast_input_tokens INTEGER NOT NULL,
                    real_input_tokens INTEGER NOT NULL,
                    real_cached_input_tokens INTEGER NOT NULL,
                    forecast_tool_tokens INTEGER NOT NULL,
                    real_tool_tokens INTEGER NOT NULL,
                    forecast_reasoning_tokens INTEGER NOT NULL,
                    real_reasoning_tokens INTEGER NOT NULL,
                    forecast_output_tokens INTEGER NOT NULL,
                    real_output_tokens INTEGER NOT NULL,
                    forecast_total_tokens INTEGER NOT NULL,
                    real_total_tokens INTEGER NOT NULL,
                    unoptimized_baseline_tokens INTEGER NOT NULL,
                    skill_overhead_tokens INTEGER NOT NULL,
                    net_tokens_saved INTEGER NOT NULL,
                    forecast_cost_usd REAL NOT NULL,
                    real_cost_usd REAL NOT NULL,
                    unoptimized_cost_usd REAL NOT NULL,
                    net_cost_saved_usd REAL NOT NULL,
                    token_delta INTEGER NOT NULL,
                    cost_delta_usd REAL NOT NULL,
                    ape_pct REAL NOT NULL,
                    variance_tags TEXT NOT NULL,
                    telemetry_source TEXT NOT NULL DEFAULT 'API_EXACT',
                    transcript_path TEXT NOT NULL DEFAULT '',
                    receipts_json TEXT NOT NULL DEFAULT '[]',
                    ab_baseline_tokens INTEGER NOT NULL DEFAULT 0,
                    ab_baseline_steps INTEGER NOT NULL DEFAULT 0,
                    ab_baseline_cost_usd REAL NOT NULL DEFAULT 0.0,
                    last_step_context_tokens INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(timestamp);
                """
            )
            # Safe schema migration for existing telemetry.db files
            for col_def in (
                "telemetry_source TEXT NOT NULL DEFAULT 'API_EXACT'",
                "transcript_path TEXT NOT NULL DEFAULT ''",
                "receipts_json TEXT NOT NULL DEFAULT '[]'",
                "ab_baseline_tokens INTEGER NOT NULL DEFAULT 0",
                "ab_baseline_steps INTEGER NOT NULL DEFAULT 0",
                "ab_baseline_cost_usd REAL NOT NULL DEFAULT 0.0",
                "last_step_context_tokens INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    conn.execute(f"ALTER TABLE turns ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass

    def record_turn(self, record: Dict[str, object]) -> None:
        tags = record.get("variance_tags", ["ON_TARGET"])
        tags_str = json.dumps(tags) if isinstance(tags, list) else str(tags)
        receipts = record.get("receipts", [])
        receipts_str = json.dumps(receipts) if isinstance(receipts, list) else str(receipts or "[]")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO turns (
                    forecast_id, timestamp, agent_host, model_id, prompt,
                    profile_code, optimization_level, forecast_steps, real_steps,
                    forecast_input_tokens, real_input_tokens, real_cached_input_tokens,
                    forecast_tool_tokens, real_tool_tokens,
                    forecast_reasoning_tokens, real_reasoning_tokens,
                    forecast_output_tokens, real_output_tokens,
                    forecast_total_tokens, real_total_tokens,
                    unoptimized_baseline_tokens, skill_overhead_tokens, net_tokens_saved,
                    forecast_cost_usd, real_cost_usd, unoptimized_cost_usd, net_cost_saved_usd,
                    token_delta, cost_delta_usd, ape_pct, variance_tags,
                    telemetry_source, transcript_path, receipts_json,
                    ab_baseline_tokens, ab_baseline_steps, ab_baseline_cost_usd,
                    last_step_context_tokens
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?
                )
                """,
                (
                    str(record.get("forecast_id", f"tw_{int(time.time()*1000)}")),
                    float(record.get("timestamp", time.time())),
                    str(record.get("agent_host", "claude")),
                    str(record.get("model_id", "claude-sonnet-4")),
                    str(record.get("prompt", "")),
                    str(record.get("profile_code", "B")),
                    int(record.get("optimization_level", 1)),
                    int(record.get("forecast_steps", 1)),
                    int(record.get("real_steps", 1)),
                    int(record.get("forecast_input_tokens", 0)),
                    int(record.get("real_input_tokens", 0)),
                    int(record.get("real_cached_input_tokens", 0)),
                    int(record.get("forecast_tool_tokens", 0)),
                    int(record.get("real_tool_tokens", 0)),
                    int(record.get("forecast_reasoning_tokens", 0)),
                    int(record.get("real_reasoning_tokens", 0)),
                    int(record.get("forecast_output_tokens", 0)),
                    int(record.get("real_output_tokens", 0)),
                    int(record.get("forecast_total_tokens", 0)),
                    int(record.get("real_total_tokens", 0)),
                    int(record.get("unoptimized_baseline_tokens", 0)),
                    int(record.get("skill_overhead_tokens", 290)),
                    int(record.get("net_tokens_saved", 0)),
                    float(record.get("forecast_cost_usd", 0.0)),
                    float(record.get("real_cost_usd", 0.0)),
                    float(record.get("unoptimized_cost_usd", 0.0)),
                    float(record.get("net_cost_saved_usd", 0.0)),
                    int(record.get("token_delta", 0)),
                    float(record.get("cost_delta_usd", 0.0)),
                    float(record.get("ape_pct", 0.0)),
                    tags_str,
                    str(record.get("telemetry_source", "API_EXACT")),
                    str(record.get("transcript_path", "")),
                    receipts_str,
                    int(record.get("ab_baseline_tokens", 0)),
                    int(record.get("ab_baseline_steps", 0)),
                    float(record.get("ab_baseline_cost_usd", 0.0)),
                    int(record.get("last_step_context_tokens", 0)),
                ),
            )

    def update_ab_baseline(
        self,
        forecast_id: str,
        ab_baseline_tokens: int,
        ab_baseline_steps: int,
        ab_baseline_cost_usd: float,
    ) -> None:
        """Attach paired A/B baseline metrics without mutating reconciled turn status or forecast numbers."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE turns
                SET ab_baseline_tokens = ?, ab_baseline_steps = ?, ab_baseline_cost_usd = ?
                WHERE forecast_id = ?
                """,
                (int(ab_baseline_tokens), int(ab_baseline_steps), float(ab_baseline_cost_usd), str(forecast_id)),
            )

    def update_last_step_context(
        self,
        forecast_id: str,
        last_step_context_tokens: int,
    ) -> None:
        """Backfill or live-update exact final-step context without mutating reconciled forecast/cost/APE."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE turns SET last_step_context_tokens = ? WHERE forecast_id = ?",
                (int(last_step_context_tokens), str(forecast_id)),
            )

    def get_summary(self, days: Optional[int] = None, limit: int = 50) -> Dict[str, object]:
        # Automatically sync any completed real Antigravity transcript turns for this workspace
        ab_comparison: Optional[Dict[str, object]] = None
        try:
            import importlib.util

            hook_path = Path(__file__).resolve().parent.parent / "hooks" / "post-turn-record.py"
            if hook_path.exists() and (self.root_dir / ".tokwiz").exists():
                spec = importlib.util.spec_from_file_location("post_turn_sync", hook_path)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    mod.sync_workspace_transcript_turns(self.root_dir)
                    sibling_base = self.root_dir.parent / "project-baseline"
                    if sibling_base != self.root_dir and sibling_base.exists():
                        ab_comparison = mod.build_ab_comparison_report(
                            baseline_root=sibling_base,
                            tokwiz_root=self.root_dir,
                        )
        except Exception:
            pass

        cutoff = (time.time() - days * 86400.0) if days is not None else 0.0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM turns WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()

        def _is_xml_system_prompt(raw_p: object) -> bool:
            s = str(raw_p or "").strip()
            return (
                bool(re.match(r"^<[a-zA-Z_][a-zA-Z0-9_:-]*\b[^>]*>", s))
                or s.startswith("This session is being continued from a previous conversation")
                or s.startswith("Base directory for this skill:")
                or s.startswith("[Request interrupted")
            )

        turns = [dict(r) for r in rows if not _is_xml_system_prompt(r["prompt"])]
        for t in turns:
            try:
                t["variance_tags"] = json.loads(str(t["variance_tags"]))
            except Exception:
                t["variance_tags"] = [str(t["variance_tags"])]
            try:
                t["receipts"] = json.loads(str(t.get("receipts_json") or "[]"))
            except Exception:
                t["receipts"] = []

        # Also read real pre-prompt forecasts (.tokwiz/forecasts.jsonl) and guardrail events
        installed_at: Optional[float] = None
        cfg_path = self.root_dir / ".tokwiz" / "config.json"
        if cfg_path.exists():
            try:
                cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if cfg_data.get("installed_at") is not None:
                    installed_at = float(cfg_data["installed_at"])
            except Exception:
                installed_at = None

        forecasts_list: List[Dict[str, object]] = []
        forecasts_path = self.root_dir / ".tokwiz" / "forecasts.jsonl"
        if forecasts_path.exists():
            for line in forecasts_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    try:
                        f_obj = json.loads(line)
                        if installed_at is not None and float(f_obj.get("timestamp", 0.0)) < installed_at:
                            continue
                        if _is_xml_system_prompt(f_obj.get("prompt")):
                            continue
                        forecasts_list.append(f_obj)
                    except Exception:
                        pass

        guardrail_list: List[Dict[str, object]] = []
        guardrail_path = self.root_dir / ".tokwiz" / "guardrail-events.jsonl"
        if guardrail_path.exists():
            for line in guardrail_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    try:
                        guardrail_list.append(json.loads(line))
                    except Exception:
                        pass
        guardrail_saved_tokens = sum(int(g.get("tokens_saved", 0)) for g in guardrail_list)

        total_turns = len(turns)
        if total_turns == 0:
            f_tok = sum(int(f.get("total_tokens", 0)) for f in forecasts_list)
            u_tok = sum(int(f.get("unoptimized_tokens", 0)) for f in forecasts_list)
            f_saved_tok = sum(int(f.get("savings_tokens", max(0, int(f.get("unoptimized_tokens", 0)) - int(f.get("total_tokens", 0))))) for f in forecasts_list)
            est_red_pct = round((f_saved_tok / max(1, u_tok)) * 100.0, 1) if u_tok > 0 else 0.0

            f_cost_total = 0.0
            u_cost_total = 0.0
            f_model_stats: Dict[str, Dict[str, float]] = {}
            for f in forecasts_list:
                costs_map = f.get("costs", {})
                if isinstance(costs_map, dict):
                    c_sonnet = costs_map.get("claude-sonnet-4", {})
                    if isinstance(c_sonnet, dict):
                        f_cost_total += float(c_sonnet.get("cached_cost_usd", c_sonnet.get("uncached_cost_usd", 0.0)))
                        u_cost_total += float(c_sonnet.get("unoptimized_cost_usd", 0.0))
                    for m_id in ("claude-sonnet-4", "o4-mini", "gemini-2.5-pro"):
                        m_info = costs_map.get(m_id)
                        if isinstance(m_info, dict):
                            m_entry = f_model_stats.setdefault(
                                m_id,
                                {"turns": 0, "forecast_cost_usd": 0.0, "real_cost_usd": 0.0, "saved_cost_usd": 0.0},
                            )
                            m_f_cost = float(m_info.get("cached_cost_usd", m_info.get("uncached_cost_usd", 0.0)))
                            m_u_cost = float(m_info.get("unoptimized_cost_usd", 0.0))
                            m_entry["turns"] += 1
                            m_entry["forecast_cost_usd"] = round(m_entry["forecast_cost_usd"] + m_f_cost, 6)
                            m_entry["saved_cost_usd"] = round(m_entry["saved_cost_usd"] + max(0.0, m_u_cost - m_f_cost), 6)

            f_phase_breakdown = {
                "context_input": sum(int(f.get("context_tokens", 0)) + int(f.get("prompt_tokens", 0)) for f in forecasts_list),
                "graph_skill": 290 * len(forecasts_list),
                "tool_output": sum(int(f.get("tool_loop_tokens", 0)) for f in forecasts_list),
                "reasoning": sum(int(f.get("reasoning_tokens", 0)) for f in forecasts_list),
                "output": sum(int(f.get("output_tokens", 0)) for f in forecasts_list),
            }

            return {
                "total_turns": 0,
                "total_forecasts": len(forecasts_list),
                "guardrail_events_count": len(guardrail_list),
                "guardrail_tokens_saved": guardrail_saved_tokens,
                "total_forecast_tokens": f_tok,
                "total_real_tokens": 0,
                "total_unoptimized_tokens": u_tok,
                "net_tokens_saved": guardrail_saved_tokens + f_saved_tok,
                "avg_reduction_pct": est_red_pct,
                "total_forecast_cost_usd": round(f_cost_total, 6),
                "total_real_cost_usd": 0.0,
                "total_unoptimized_cost_usd": round(u_cost_total, 6),
                "net_cost_saved_usd": round(max(0.0, u_cost_total - f_cost_total), 6),
                "total_cost_delta_usd": 0.0,
                "mape_pct": 0.0,
                "recent_mape_pct": 0.0,
                "cache_hit_ratio_pct": 80.0 if forecasts_list else 0.0,
                "phase_breakdown": f_phase_breakdown,
                "variance_breakdown": {},
                "level_distribution": {"1": 0, "2": 0, "3": 0},
                "model_breakdown": f_model_stats,
                "turns": [],
                "forecasts": forecasts_list[-limit:],
                "guardrail_events": guardrail_list[-limit:],
                "ab_comparison": ab_comparison,
                "context_health": {
                    "status": "HEALTHY",
                    "tokens_per_step": 0,
                    "recommendation": "Healthy (<40k/step) · Auto-checkpoint ready",
                },
                "burn_diagnostics": [],
            }

        tot_f_tok = sum(int(t["forecast_total_tokens"]) for t in turns)
        tot_r_tok = sum(int(t["real_total_tokens"]) for t in turns)
        tot_u_tok = sum(int(t["unoptimized_baseline_tokens"]) for t in turns)
        tot_saved_tok = sum(int(t["net_tokens_saved"]) for t in turns)
        tot_eff_tok = sum(
            max(0, int(t["real_input_tokens"]) - int(t["real_cached_input_tokens"]))
            + int(round(int(t["real_cached_input_tokens"]) * 0.1))
            + int(t["real_reasoning_tokens"])
            + int(t["real_output_tokens"])
            for t in turns
        )

        tot_f_cost = sum(float(t["forecast_cost_usd"]) for t in turns)
        tot_r_cost = sum(float(t["real_cost_usd"]) for t in turns)
        tot_u_cost = sum(float(t["unoptimized_cost_usd"]) for t in turns)
        guardrail_saved_cost = (guardrail_saved_tokens / 1_000_000.0) * 3.0 if guardrail_saved_tokens > 0 else 0.0
        tot_saved_cost = sum(float(t["net_cost_saved_usd"]) for t in turns) + guardrail_saved_cost
        tot_cost_delta = tot_r_cost - tot_f_cost

        mape = sum(float(t["ape_pct"]) for t in turns) / total_turns
        recent_slice = turns[-min(5, total_turns) :]
        recent_mape = sum(float(t["ape_pct"]) for t in recent_slice) / len(recent_slice)

        tot_in = sum(int(t["real_input_tokens"]) for t in turns)
        tot_cached = sum(int(t["real_cached_input_tokens"]) for t in turns)
        cache_ratio = (tot_cached / max(1, tot_in)) * 100.0
        total_net_saved = tot_saved_tok + guardrail_saved_tokens
        avg_red_pct = (total_net_saved / max(1, tot_eff_tok + total_net_saved)) * 100.0

        variance_counts: Dict[str, int] = {}
        level_dist: Dict[str, int] = {"1": 0, "2": 0, "3": 0}
        model_stats: Dict[str, Dict[str, float]] = {}

        for t in turns:
            lvl = str(t["optimization_level"])
            level_dist[lvl] = level_dist.get(lvl, 0) + 1
            for tag in t["variance_tags"]:
                variance_counts[tag] = variance_counts.get(tag, 0) + 1
            m_id = str(t["model_id"])
            m_entry = model_stats.setdefault(
                m_id,
                {"turns": 0, "forecast_cost_usd": 0.0, "real_cost_usd": 0.0, "saved_cost_usd": 0.0},
            )
            m_entry["turns"] += 1
            m_entry["forecast_cost_usd"] = round(m_entry["forecast_cost_usd"] + float(t["forecast_cost_usd"]), 6)
            m_entry["real_cost_usd"] = round(m_entry["real_cost_usd"] + float(t["real_cost_usd"]), 6)
            m_entry["saved_cost_usd"] = round(m_entry["saved_cost_usd"] + float(t["net_cost_saved_usd"]), 6)

        phase_breakdown = {
            "context_input": sum(
                max(0, int(t["real_input_tokens"]) - int(t["real_cached_input_tokens"]) - int(t["real_tool_tokens"]))
                for t in turns
            ),
            "cached_input": sum(int(t["real_cached_input_tokens"]) for t in turns),
            "graph_skill": sum(int(t["skill_overhead_tokens"]) for t in turns),
            "tool_output": sum(int(t["real_tool_tokens"]) for t in turns),
            "reasoning": sum(int(t["real_reasoning_tokens"]) for t in turns),
            "output": sum(int(t["real_output_tokens"]) for t in turns),
        }

        last_turn = turns[-1]
        last_in = int(last_turn["real_input_tokens"])
        last_cached = int(last_turn["real_cached_input_tokens"])
        last_total = int(last_turn["real_total_tokens"])
        last_out = int(last_turn["real_output_tokens"]) + int(last_turn["real_reasoning_tokens"])
        # Handle both new rows (real_input_tokens includes cached) and legacy rows (cached stored separately)
        last_effective_ctx_total = max(last_in, last_total - last_out, last_in + (last_cached if last_total > last_in + last_out else 0))
        avg_tok_per_step = last_effective_ctx_total // max(1, int(last_turn["real_steps"]))
        exact_last_step_ctx = int(last_turn.get("last_step_context_tokens") or 0)
        current_ctx = exact_last_step_ctx if exact_last_step_ctx > 0 else avg_tok_per_step
        latest_tok_per_step = current_ctx

        model_low = str(last_turn.get("model_id") or "").lower()
        if any(k in model_low for k in ("opus-5", "opus-4-6", "sonnet-4-6", "gemini-2.5", "gemini-3", "gemini-4")):
            ctx_limit = 1_000_000
        else:
            ctx_limit = 200_000
        ctx_pct = round((current_ctx / max(1, ctx_limit)) * 100.0, 1)

        if current_ctx > 100_000:
            ctx_status = "BLOATED"
            ctx_rec = "Bloated (>100k ctx) — Run /clear + tokwiz_resume"
        elif current_ctx >= 40_000:
            ctx_status = "WARM"
            ctx_rec = "Warm (40k–100k ctx) — Consider /clear + tokwiz_resume soon"
        else:
            ctx_status = "HEALTHY"
            ctx_rec = "Healthy (<40k ctx) · Auto-checkpoint ready"

        # Compute deterministic Token Burn Diagnostics & Manual Action Plan
        burn_diagnostics: List[Dict[str, object]] = []
        extra_loops_cnt = int(variance_counts.get("EXTRA_TOOL_LOOPS", 0))
        cache_miss_cnt = int(variance_counts.get("CACHE_MISS_DRIFT", 0))
        unbounded_cnt = int(variance_counts.get("UNBOUNDED_TOOL_OUTPUT", 0))

        # 1. Heavy Session Prefix / Context Bloat
        cached_share_pct = round((tot_cached / max(1, tot_r_tok)) * 100.0, 1)
        if latest_tok_per_step >= 40_000 or (cached_share_pct >= 75.0 and tot_r_tok >= 250_000):
            sev = "HIGH" if (latest_tok_per_step > 100_000 or tot_r_tok >= 1_000_000) else "MEDIUM"
            burn_diagnostics.append(
                {
                    "id": "session_prefix_bloat",
                    "severity": sev,
                    "title": f"Heavy Session Prefix & Context Re-Reads ({latest_tok_per_step // 1000}k tok/step · {cached_share_pct}% cached)",
                    "why": (
                        f"{tot_cached:,} of {tot_r_tok:,} raw tokens ({cached_share_pct}%) are repeated KV-cached context "
                        f"(conversation history, system tools, and MCP schemas) re-sent on every tool step."
                    ),
                    "steps": [
                        "Run `/clear` between unrelated stories or PRs, then ask the agent to call `tokwiz_resume` (<200 tokens) instead of carrying 100k+ old messages.",
                        "Run `/mcp` in your IDE/CLI and disable unused MCP servers (e.g., Google Drive, Figma, Docs) when coding — unused servers inject 20k–60k tool schema tokens on every step.",
                    ],
                }
            )

        # 2. Excessive Tool Loops
        if extra_loops_cnt >= 2 or (total_turns >= 3 and (extra_loops_cnt / total_turns) >= 0.4):
            loop_pct = round((extra_loops_cnt / max(1, total_turns)) * 100.0)
            burn_diagnostics.append(
                {
                    "id": "extra_tool_loops",
                    "severity": "HIGH" if loop_pct >= 50 else "MEDIUM",
                    "title": f"Excessive Sequential Tool Loops ({extra_loops_cnt}/{total_turns} turns · {loop_pct}%)",
                    "why": (
                        f"{extra_loops_cnt} of {total_turns} turns triggered `EXTRA_TOOL_LOOPS`. "
                        "Each extra search/read step re-sends your entire session prefix, multiplying total token burn."
                    ),
                    "steps": [
                        "Scope prompts to 1 story or 1 module per turn instead of broad multi-story/epic prompts (`next 2 stories`, `complete the epic`).",
                        "Mention exact target files/symbols or run `tokwiz optimize \"<prompt>\"` so the agent jumps straight to `file:Lstart-Lend` on Step 1.",
                    ],
                }
            )

        # 3. Expensive Model Used on Routine Profile A/B Turns
        opus_ab_turns = [
            t for t in turns
            if any(k in str(t.get("model_id", "")).lower() for k in ("opus", "o3"))
            and str(t.get("profile_code", "")) in ("A", "B")
        ]
        if opus_ab_turns:
            ab_spend = sum(float(t.get("real_cost_usd", 0.0)) for t in opus_ab_turns)
            burn_diagnostics.append(
                {
                    "id": "model_routing",
                    "severity": "HIGH" if ab_spend >= 1.0 else "MEDIUM",
                    "title": f"Premium Model Used on {len(opus_ab_turns)} Simple Profile A/B Turn(s) (${ab_spend:.2f} spent)",
                    "why": (
                        f"A top-tier reasoning model ({opus_ab_turns[-1].get('model_id')}) ran on routine Profile A/B prompts "
                        f"(Q&A, git/PR ops, or single-file edits) where Sonnet/Flash costs ~80% less."
                    ),
                    "steps": [
                        "Switch to Sonnet or Flash for git branches, PR merges, test fixes, and single-file edits.",
                        "Reserve Opus / o3 for complex multi-file architecture or deep debugging.",
                    ],
                }
            )

        # 4. Cache Miss Drift or Unbounded Tool Output
        if cache_miss_cnt > 0:
            burn_diagnostics.append(
                {
                    "id": "cache_miss_drift",
                    "severity": "MEDIUM",
                    "title": f"KV-Cache Invalidation Detected ({cache_miss_cnt} turn(s))",
                    "why": "Cache hit rate dropped below 45%, billing repeated context at full uncached input rates (up to 10× pricier).",
                    "steps": [
                        "Avoid editing `CLAUDE.md`, `AGENTS.md`, or MCP tool lists mid-session — prefix mutations invalidate the KV cache.",
                    ],
                }
            )
        if unbounded_cnt > 0:
            burn_diagnostics.append(
                {
                    "id": "unbounded_tool_output",
                    "severity": "MEDIUM",
                    "title": f"Oversized Tool Outputs Entered Context ({unbounded_cnt} turn(s))",
                    "why": "Large raw test logs, diffs, or full file dumps inflated tool-output tokens.",
                    "steps": [
                        "Use `tokwiz test`, `tokwiz diff-slice`, or `tokwiz filter -- <cmd>` instead of unfiltered test or `git diff` outputs.",
                    ],
                }
            )

        return {
            "total_turns": total_turns,
            "total_forecasts": len(forecasts_list),
            "guardrail_events_count": len(guardrail_list),
            "guardrail_tokens_saved": guardrail_saved_tokens,
            "total_forecast_tokens": tot_f_tok,
            "total_real_tokens": tot_r_tok,
            "total_effective_tokens": tot_eff_tok,
            "total_unoptimized_tokens": tot_u_tok,
            "net_tokens_saved": tot_saved_tok + guardrail_saved_tokens,
            "avg_reduction_pct": round(avg_red_pct, 1),
            "total_forecast_cost_usd": round(tot_f_cost, 6),
            "total_real_cost_usd": round(tot_r_cost, 6),
            "total_unoptimized_cost_usd": round(tot_u_cost, 6),
            "net_cost_saved_usd": round(tot_saved_cost, 6),
            "total_cost_delta_usd": round(tot_cost_delta, 6),
            "mape_pct": round(mape, 2),
            "recent_mape_pct": round(recent_mape, 2),
            "cache_hit_ratio_pct": round(cache_ratio, 1),
            "phase_breakdown": phase_breakdown,
            "variance_breakdown": variance_counts,
            "level_distribution": level_dist,
            "model_breakdown": model_stats,
            "turns": turns[-limit:],
            "forecasts": forecasts_list[-limit:],
            "guardrail_events": guardrail_list[-limit:],
            "ab_comparison": ab_comparison,
            "context_health": {
                "status": ctx_status,
                "tokens_per_step": current_ctx,
                "current_context_tokens": current_ctx,
                "avg_tokens_per_step": avg_tok_per_step,
                "context_window_limit": ctx_limit,
                "context_pct": ctx_pct,
                "measurement_mode": "EXACT_LAST_STEP" if exact_last_step_ctx > 0 else "TURN_AVG",
                "recommendation": ctx_rec,
            },
            "burn_diagnostics": burn_diagnostics,
        }


def render_cli_stats(summary: Dict[str, object]) -> str:
    turns_count = int(summary["total_turns"])
    forecasts_count = int(summary.get("total_forecasts", 0))
    if turns_count == 0 and forecasts_count == 0:
        return "[TokWiz Stats] No recorded turns or forecasts yet in `.tokwiz/`."

    if turns_count == 0:
        lines = [
            "╔══════════════════════════════════════════════════════════════════════════╗",
            "║                ⚡ TokWiz Pre-Prompt Forecast Summary Report              ║",
            "╠══════════════════════════════════════════════════════════════════════════╣",
            f"║ Forecasts Recorded   : {forecasts_count:<12}  Est. Token Reduction: {summary['avg_reduction_pct']:>6.1f}%       ║",
            f"║ Est. Tokens Saved    : {int(summary['net_tokens_saved']):>10,}  Est. KV Cache Hit   : {summary['cache_hit_ratio_pct']:>6.1f}%       ║",
            f"║ Forecasted Total Cost: ${float(summary['total_forecast_cost_usd']):>9.4f}  Unoptimized Cost    : ${float(summary['total_unoptimized_cost_usd']):>9.4f}    ║",
            f"║ Est. Net $ Saved     : ${float(summary['net_cost_saved_usd']):>9.4f}  Reconciled Turns    : {'0 (Pending)':<13}║",
            "╚══════════════════════════════════════════════════════════════════════════╝",
            "",
            "Recent Pre-Prompt Forecasts (Awaiting Post-Turn Reconciliation):",
            f"{'ID':<14} {'Profile':<8} {'Forecast Tok':>13} {'Baseline Tok':>13} {'Est Saved':>11} {'Forecast $':>11}  {'Prompt'}",
            "-" * 96,
        ]
        f_list: List[Dict[str, object]] = summary.get("forecasts", [])  # type: ignore[assignment]
        for f in f_list[-10:]:
            c_map = f.get("costs", {})
            c_sonnet = c_map.get("claude-sonnet-4", {}) if isinstance(c_map, dict) else {}
            f_usd = float(c_sonnet.get("cached_cost_usd", 0.0)) if isinstance(c_sonnet, dict) else 0.0
            lines.append(
                f"{str(f.get('forecast_id', '')):<14} "
                f"P-{str(f.get('profile_code', 'B'))}(L{f.get('optimization_level', 1)}) "
                f"{int(f.get('total_tokens', 0)):>13,} "
                f"{int(f.get('unoptimized_tokens', 0)):>13,} "
                f"+{int(f.get('savings_tokens', 0)):>10,} "
                f"${f_usd:>10.4f}  {str(f.get('prompt', ''))[:38]}"
            )
        return "\n".join(lines)

    lines = [
        "╔══════════════════════════════════════════════════════════════════════════╗",
        "║                    ⚡ TokWiz Optimization & Cost Report                  ║",
        "╠══════════════════════════════════════════════════════════════════════════╣",
        f"║ Turns Analyzed       : {turns_count:<12}  Avg Token Reduction : {summary['avg_reduction_pct']:>6.1f}%       ║",
        f"║ Net Tokens Saved     : {int(summary['net_tokens_saved']):>10,}  KV Cache Hit Rate   : {summary['cache_hit_ratio_pct']:>6.1f}%       ║",
        f"║ Forecasted Total Cost: ${float(summary['total_forecast_cost_usd']):>9.4f}  Real Billed Cost    : ${float(summary['total_real_cost_usd']):>9.4f}    ║",
        f"║ Cost Delta (ΔC)      : ${float(summary['total_cost_delta_usd']):>+9.4f}  Net $ Saved         : ${float(summary['net_cost_saved_usd']):>9.4f}    ║",
        f"║ All-Turn MAPE        : {float(summary['mape_pct']):>9.1f}%  Recent 5-Turn MAPE  : {float(summary['recent_mape_pct']):>6.1f}%       ║",
        "╚══════════════════════════════════════════════════════════════════════════╝",
        "",
        "Recent Prompt Reconciliations (Forecast vs. Real):",
        f"{'ID':<14} {'Profile':<8} {'Forecast Tok':>13} {'Real Tok':>10} {'Forecast $':>11} {'Real $':>11} {'APE %':>7}  {'Variance Tags'}",
        "-" * 96,
    ]
    turns_list: List[Dict[str, object]] = summary.get("turns", [])  # type: ignore[assignment]
    reconciled_ids = {str(t.get("forecast_id", "")) for t in turns_list}
    for t in turns_list[-10:]:
        tags = ", ".join(t.get("variance_tags", []))  # type: ignore[arg-type]
        lines.append(
            f"{str(t['forecast_id']):<14} "
            f"P-{str(t['profile_code'])}(L{t['optimization_level']}) "
            f"{int(t['forecast_total_tokens']):>13,} "
            f"{int(t['real_total_tokens']):>10,} "
            f"${float(t['forecast_cost_usd']):>10.4f} "
            f"${float(t['real_cost_usd']):>10.4f} "
            f"{float(t['ape_pct']):>6.1f}%  {tags}"
        )
    f_list: List[Dict[str, object]] = summary.get("forecasts", [])  # type: ignore[assignment]
    pending = [f for f in f_list if str(f.get("forecast_id", "")) not in reconciled_ids]
    for f in pending:
        c_map = f.get("costs", {})
        c_sonnet = c_map.get("claude-sonnet-4", {}) if isinstance(c_map, dict) else {}
        f_usd = float(c_sonnet.get("cached_cost_usd", 0.0)) if isinstance(c_sonnet, dict) else 0.0
        lines.append(
            f"{str(f.get('forecast_id', '')):<14} "
            f"P-{str(f.get('profile_code', 'B'))}(L{f.get('optimization_level', 1)}) "
            f"{int(f.get('total_tokens', 0)):>13,} "
            f"{'—':>10} "
            f"${f_usd:>10.4f} "
            f"{'—':>11} "
            f"{'—':>7}  PENDING (AWAITING TURN END)"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz stats",
        description="Display token savings, Forecast vs. Real cost delta, and MAPE accuracy.",
    )
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--days", type=int, default=None, help="Filter turns from the last N days")
    parser.add_argument("--session", action="store_true", help="Show current session (last 1 day)")
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    args = parser.parse_args(argv)

    days = 1 if args.session else args.days
    store = TelemetryStore(root_dir=Path(args.root))
    summary = store.get_summary(days=days)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render_cli_stats(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
