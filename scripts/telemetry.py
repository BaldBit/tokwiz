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
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional


class TelemetryStore:
    """SQLite ledger storing per-turn forecast vs. real reconciliation and guardrail metrics."""

    def __init__(self, root_dir: Path | str = ".", db_path: Optional[Path | str] = None) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.db_path = Path(db_path).resolve() if db_path else (self.root_dir / ".tokwiz" / "telemetry.db")
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
                    variance_tags TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(timestamp);
                """
            )

    def record_turn(self, record: Dict[str, object]) -> None:
        tags = record.get("variance_tags", ["ON_TARGET"])
        tags_str = json.dumps(tags) if isinstance(tags, list) else str(tags)
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
                    token_delta, cost_delta_usd, ape_pct, variance_tags
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
                    ?, ?, ?, ?
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
                ),
            )

    def get_summary(self, days: Optional[int] = None, limit: int = 50) -> Dict[str, object]:
        cutoff = (time.time() - days * 86400.0) if days is not None else 0.0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM turns WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()

        turns = [dict(r) for r in rows]
        for t in turns:
            try:
                t["variance_tags"] = json.loads(str(t["variance_tags"]))
            except Exception:
                t["variance_tags"] = [str(t["variance_tags"])]

        total_turns = len(turns)
        if total_turns == 0:
            return {
                "total_turns": 0,
                "total_forecast_tokens": 0,
                "total_real_tokens": 0,
                "total_unoptimized_tokens": 0,
                "net_tokens_saved": 0,
                "avg_reduction_pct": 0.0,
                "total_forecast_cost_usd": 0.0,
                "total_real_cost_usd": 0.0,
                "total_unoptimized_cost_usd": 0.0,
                "net_cost_saved_usd": 0.0,
                "total_cost_delta_usd": 0.0,
                "mape_pct": 0.0,
                "recent_mape_pct": 0.0,
                "cache_hit_ratio_pct": 0.0,
                "phase_breakdown": {
                    "context_input": 0,
                    "graph_skill": 0,
                    "tool_output": 0,
                    "reasoning": 0,
                    "output": 0,
                },
                "variance_breakdown": {},
                "level_distribution": {"1": 0, "2": 0, "3": 0},
                "model_breakdown": {},
                "turns": [],
            }

        tot_f_tok = sum(int(t["forecast_total_tokens"]) for t in turns)
        tot_r_tok = sum(int(t["real_total_tokens"]) for t in turns)
        tot_u_tok = sum(int(t["unoptimized_baseline_tokens"]) for t in turns)
        tot_saved_tok = sum(int(t["net_tokens_saved"]) for t in turns)

        tot_f_cost = sum(float(t["forecast_cost_usd"]) for t in turns)
        tot_r_cost = sum(float(t["real_cost_usd"]) for t in turns)
        tot_u_cost = sum(float(t["unoptimized_cost_usd"]) for t in turns)
        tot_saved_cost = sum(float(t["net_cost_saved_usd"]) for t in turns)
        tot_cost_delta = tot_r_cost - tot_f_cost

        mape = sum(float(t["ape_pct"]) for t in turns) / total_turns
        recent_slice = turns[-min(5, total_turns) :]
        recent_mape = sum(float(t["ape_pct"]) for t in recent_slice) / len(recent_slice)

        tot_in = sum(int(t["real_input_tokens"]) for t in turns)
        tot_cached = sum(int(t["real_cached_input_tokens"]) for t in turns)
        cache_ratio = (tot_cached / max(1, tot_in)) * 100.0
        avg_red_pct = (tot_saved_tok / max(1, tot_u_tok)) * 100.0

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
            "context_input": sum(max(0, int(t["real_input_tokens"]) - int(t["real_tool_tokens"])) for t in turns),
            "graph_skill": sum(int(t["skill_overhead_tokens"]) for t in turns),
            "tool_output": sum(int(t["real_tool_tokens"]) for t in turns),
            "reasoning": sum(int(t["real_reasoning_tokens"]) for t in turns),
            "output": sum(int(t["real_output_tokens"]) for t in turns),
        }

        return {
            "total_turns": total_turns,
            "total_forecast_tokens": tot_f_tok,
            "total_real_tokens": tot_r_tok,
            "total_unoptimized_tokens": tot_u_tok,
            "net_tokens_saved": tot_saved_tok,
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
        }


def render_cli_stats(summary: Dict[str, object]) -> str:
    turns_count = int(summary["total_turns"])
    if turns_count == 0:
        return "[TokWiz Stats] No recorded turns yet in `.tokwiz/telemetry.db`."

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
