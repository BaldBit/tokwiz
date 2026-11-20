#!/usr/bin/env python3
"""Ahead-of-Time Token & Cost Forecaster for TokWiz.

Predicts multi-step workflow tokens (input, tool-loop, reasoning, output) and
monetary cost ($) across Claude, OpenAI Codex, and Google Gemini using Tier 2
Project Graph blast-radius analysis and complexity classification (Profiles A-E).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import tiktoken  # type: ignore

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent

_GRAPH_SPEC = importlib.util.spec_from_file_location(
    "project_graph", _SCRIPTS_DIR / "project-graph.py"
)
assert _GRAPH_SPEC and _GRAPH_SPEC.loader
_graph_mod = importlib.util.module_from_spec(_GRAPH_SPEC)
sys.modules["project_graph"] = _graph_mod
_GRAPH_SPEC.loader.exec_module(_graph_mod)

ProjectGraph = _graph_mod.ProjectGraph
GraphSliceResult = _graph_mod.GraphSliceResult


@dataclass
class ProfileSpec:
    code: str  # "A" | "B" | "C" | "D" | "E"
    name: str
    optimization_level: int
    min_steps: int
    max_steps: int
    default_steps: int
    unoptimized_steps: int
    avg_tool_in_tokens: int
    avg_tool_out_tokens: int
    unoptimized_tool_out_tokens: int
    base_output_tokens: int
    reasoning_ratio: float


PROFILES: Dict[str, ProfileSpec] = {
    "A": ProfileSpec(
        code="A",
        name="Direct Q&A / Explanation",
        optimization_level=1,
        min_steps=0,
        max_steps=1,
        default_steps=1,
        unoptimized_steps=2,
        avg_tool_in_tokens=60,
        avg_tool_out_tokens=180,
        unoptimized_tool_out_tokens=600,
        base_output_tokens=220,
        reasoning_ratio=0.35,
    ),
    "B": ProfileSpec(
        code="B",
        name="Targeted Single-File Edit",
        optimization_level=1,
        min_steps=2,
        max_steps=4,
        default_steps=3,
        unoptimized_steps=6,
        avg_tool_in_tokens=90,
        avg_tool_out_tokens=260,
        unoptimized_tool_out_tokens=1100,
        base_output_tokens=320,
        reasoning_ratio=0.45,
    ),
    "C": ProfileSpec(
        code="C",
        name="Multi-File Feature / Refactor",
        optimization_level=2,
        min_steps=5,
        max_steps=10,
        default_steps=6,
        unoptimized_steps=14,
        avg_tool_in_tokens=120,
        avg_tool_out_tokens=340,
        unoptimized_tool_out_tokens=1500,
        base_output_tokens=480,
        reasoning_ratio=0.60,
    ),
    "D": ProfileSpec(
        code="D",
        name="Deep Debugging / Test Triage",
        optimization_level=2,
        min_steps=6,
        max_steps=14,
        default_steps=8,
        unoptimized_steps=18,
        avg_tool_in_tokens=130,
        avg_tool_out_tokens=380,
        unoptimized_tool_out_tokens=2200,
        base_output_tokens=450,
        reasoning_ratio=0.75,
    ),
    "E": ProfileSpec(
        code="E",
        name="Repo-Wide Exploration / Architecture",
        optimization_level=3,
        min_steps=8,
        max_steps=18,
        default_steps=10,
        unoptimized_steps=22,
        avg_tool_in_tokens=140,
        avg_tool_out_tokens=400,
        unoptimized_tool_out_tokens=2400,
        base_output_tokens=600,
        reasoning_ratio=0.80,
    ),
}


@dataclass
class ModelCostEstimate:
    model_id: str
    provider: str
    display_name: str
    uncached_cost_usd: float
    cached_cost_usd: float
    unoptimized_cost_usd: float


@dataclass
class ForecastResult:
    forecast_id: str
    prompt: str
    profile_code: str
    profile_name: str
    optimization_level: int
    estimated_steps: int
    blast_radius_files: int
    blast_radius_lines: int
    matched_symbols: List[str]
    caller_count: int
    callee_count: int
    prompt_tokens: int
    context_tokens: int
    tool_loop_tokens: int
    reasoning_tokens: int
    output_tokens: int
    total_tokens: int
    unoptimized_tokens: int
    savings_tokens: int
    savings_pct: float
    costs: Dict[str, ModelCostEstimate]
    banner: str


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        try:
            return max(1, len(_ENC.encode(text)))
        except Exception:
            pass
    return max(1, math.ceil(len(text) / 4.0))


def load_pricing_matrix(pricing_path: Optional[Path] = None) -> Dict[str, object]:
    path = pricing_path or (_REPO_ROOT / "references" / "pricing-matrix.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_resolve(path_val: Path | str) -> Path:
    try:
        return Path(path_val).resolve()
    except FileNotFoundError:
        pwd_env = os.environ.get("PWD")
        if pwd_env and os.path.isdir(pwd_env):
            os.chdir(pwd_env)
            p = Path(path_val)
            return p.resolve() if p.is_absolute() else (Path(pwd_env) / p).resolve()
        os.chdir(str(_REPO_ROOT))
        return _REPO_ROOT


class TokenEstimator:
    """Forecasts multi-step workflow tokens and cross-provider cost."""

    def __init__(
        self,
        root_dir: Path | str = ".",
        pricing_path: Optional[Path] = None,
        graph: Optional[ProjectGraph] = None,
    ) -> None:
        self.root_dir = _safe_resolve(root_dir)
        self.pricing = load_pricing_matrix(pricing_path)
        self.graph = graph or ProjectGraph(self.root_dir)
        self.calibration = self._load_calibration()

    def _load_calibration(self) -> Dict[str, object]:
        cal_path = self.root_dir / ".tokwiz" / "calibration.json"
        if cal_path.exists():
            try:
                return json.loads(cal_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    @staticmethod
    def classify_prompt(prompt: str, graph_slice: GraphSliceResult) -> ProfileSpec:
        p_low = prompt.lower().strip()
        words = re.findall(r"[a-z0-9_]+", p_low)
        word_set = set(words)

        debug_terms = {
            "bug", "fix", "error", "failing", "fail", "crash", "exception",
            "traceback", "stack", "debug", "triage", "flaky", "broken", "pytest",
        }
        repo_wide_terms = {
            "architecture", "entire", "codebase", "repo", "repository",
            "all", "everywhere", "migrate", "audit", "system-wide", "end-to-end",
        }
        refactor_terms = {
            "refactor", "implement", "feature", "support", "across", "update",
            "integrate", "add", "redesign", "extract", "rename",
        }
        qa_starters = (
            "what ", "why ", "how ", "explain ", "describe ", "where is ",
            "trace ", "show ", "list ", "which ", "map ", "inspect ",
            "assume ", "recover ", "summarize ",
        )
        edit_terms = {"edit", "change", "modify", "patch", "function", "method", "typo"}

        impacted_count = len(graph_slice.impacted_files)

        p_clean = re.sub(r"^(?:task\s*\d+\s*(?:\([^)]*\))?\s*[:.-]\s*)", "", p_low)

        # Profile A: Direct Q&A / Trace / Explanation (checked first when no write/edit/debug/audit verbs exist)
        if (p_clean.startswith(qa_starters) or p_clean.endswith("?")) and not (
            word_set & (refactor_terms | edit_terms | debug_terms | {"audit", "migrate"})
        ):
            return PROFILES["A"]

        # Profile D: Debugging / Test Triage
        if word_set & debug_terms and ("test" in word_set or "error" in word_set or "fix" in word_set or "bug" in word_set):
            if impacted_count > 1 or "test" in word_set or "failing" in word_set or "traceback" in word_set:
                return PROFILES["D"]

        # Profile E: Repo-Wide Exploration / Architecture
        if len(word_set & repo_wide_terms) >= 2 or impacted_count >= 6:
            return PROFILES["E"]

        # Profile C: Multi-File Feature / Refactor
        if impacted_count >= 2 or ("refactor" in word_set) or ("tests" in word_set and (word_set & refactor_terms)):
            return PROFILES["C"]

        # Profile B: Targeted Single-File Edit (default for actionable code requests)
        if word_set & (edit_terms | refactor_terms | debug_terms) or impacted_count == 1:
            return PROFILES["B"]

        return PROFILES["A"]

    @staticmethod
    def _is_continuation_prompt(prompt: str) -> bool:
        p = prompt.lower().strip()
        if not p:
            return False
        continuation_phrases = (
            "continue",
            "keep going",
            "go ahead",
            "proceed",
            "next story",
            "next stories",
            "next 2",
            "next two",
            "next task",
            "remaining",
            "merged pr",
            "maerged pr",
            "merged all",
            "work on next",
            "pick up",
        )
        return any(phrase in p for phrase in continuation_phrases)

    def _get_recent_session_context(self) -> Dict[str, object]:
        """Read recent turn telemetry to seed exact raw context, step baseline, cache hit ratio, and output rate."""
        default: Dict[str, object] = {
            "has_telemetry": False,
            "context_tokens": 450,
            "last_profile_code": "A",
            "last_real_steps": 0,
            "recent_median_steps": 0,
            "observed_cache_ratio": 0.80,
            "avg_output_per_step": 0,
        }
        db_path = self.root_dir / ".tokwiz" / "telemetry.db"
        if not db_path.exists():
            return default
        try:
            import contextlib
            import sqlite3
            import statistics

            with contextlib.closing(sqlite3.connect(str(db_path), timeout=0.3)) as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
                if "last_step_context_tokens" in cols:
                    rows = conn.execute(
                        "SELECT real_input_tokens, real_cached_input_tokens, real_steps, "
                        "profile_code, real_output_tokens, real_reasoning_tokens, last_step_context_tokens "
                        "FROM turns ORDER BY timestamp DESC LIMIT 5"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT real_input_tokens, real_cached_input_tokens, real_steps, "
                        "profile_code, real_output_tokens, real_reasoning_tokens, 0 "
                        "FROM turns ORDER BY timestamp DESC LIMIT 5"
                    ).fetchall()
            if not rows:
                return default

            latest = rows[0]
            steps = max(1, int(latest[2] or 1))
            raw_in = int(latest[0] or 0)
            last_step_ctx = int(latest[6] or 0)
            raw_per_step = last_step_ctx if last_step_ctx > 0 else (raw_in // steps)
            ctx_tok = min(1_000_000, max(450, raw_per_step))

            recent_steps = [max(1, int(r[2] or 1)) for r in rows[:3]]
            median_steps = int(statistics.median(recent_steps))

            tot_in_recent = sum(max(0, int(r[0] or 0)) for r in rows[:3])
            tot_cached_recent = sum(min(max(0, int(r[0] or 0)), max(0, int(r[1] or 0))) for r in rows[:3])
            if tot_in_recent > 1_000:
                obs_cache = max(0.50, min(0.96, tot_cached_recent / tot_in_recent))
            else:
                obs_cache = 0.80

            tot_out_recent = sum(max(0, int(r[4] or 0)) + max(0, int(r[5] or 0)) for r in rows[:3])
            tot_steps_recent = sum(recent_steps)
            avg_out_per_step = tot_out_recent // max(1, tot_steps_recent)

            return {
                "has_telemetry": True,
                "context_tokens": ctx_tok,
                "last_profile_code": str(latest[3] or "C"),
                "last_real_steps": steps,
                "recent_median_steps": median_steps,
                "observed_cache_ratio": obs_cache,
                "avg_output_per_step": avg_out_per_step,
            }
        except Exception:
            return default

    def _estimate_recent_context_tokens(self) -> int:
        """Seed starting context from the latest observed per-step raw context in telemetry.db."""
        return int(self._get_recent_session_context()["context_tokens"])

    def estimate(
        self,
        prompt: str,
        context_tokens: int = 450,
        forced_level: Optional[int] = None,
    ) -> ForecastResult:
        session_ctx = self._get_recent_session_context()
        has_telemetry = bool(session_ctx["has_telemetry"])
        if context_tokens == 450 and has_telemetry:
            context_tokens = int(session_ctx["context_tokens"])
        graph_slice = self.graph.query(prompt, max_tokens=400, auto_build=True)
        profile = self.classify_prompt(prompt, graph_slice)

        # Continuation prompts ("please continue", "merged pr, continue with next stories")
        # lack code symbol names; inherit the active workspace's recent workflow profile instead of Profile A.
        is_continuation = self._is_continuation_prompt(prompt)
        if is_continuation and profile.code == "A":
            last_prof = str(session_ctx.get("last_profile_code") or "C")
            if has_telemetry and last_prof in PROFILES and last_prof != "A":
                profile = PROFILES[last_prof]
            elif any(k in prompt.lower() for k in ("story", "stories", "epic", "remaining", "next 2", "next two")):
                profile = PROFILES["C"]

        if profile.code == "A":
            opt_level = forced_level if forced_level in (1, 2, 3) else 1
        else:
            opt_level = forced_level if forced_level in (1, 2, 3) else profile.optimization_level
            if len(graph_slice.impacted_files) > 2 or context_tokens > 25000:
                opt_level = max(opt_level, 2)

        # Apply optional calibration multipliers
        step_mult = float(
            self.calibration.get("profile_step_multipliers", {}).get(profile.code, 1.0)  # type: ignore[union-attr]
        )
        tool_mult = float(
            self.calibration.get("tool_output_multipliers", {}).get(profile.code, 1.0)  # type: ignore[union-attr]
        )
        reason_mult = float(
            self.calibration.get("reasoning_multipliers", {}).get(profile.code, 1.0)  # type: ignore[union-attr]
        )
        max_step_cap = 120 if (has_telemetry or step_mult > 1.5) else profile.max_steps
        if profile.code == "A" and len(graph_slice.matched_symbols) > 0:
            base_steps = min(2, max(1, (len(graph_slice.matched_symbols) + 1) // 2))
            n_steps = max(1, min(max_step_cap, round(base_steps * step_mult)))
        else:
            n_steps = max(profile.min_steps, min(max_step_cap, round(profile.default_steps * step_mult)))

        if has_telemetry and is_continuation:
            recent_med = int(session_ctx.get("recent_median_steps") or 0)
            if recent_med > n_steps:
                n_steps = min(120, round(0.4 * n_steps + 0.6 * recent_med))

        prompt_tok = count_tokens(prompt)
        blast_files = max(1, len(graph_slice.impacted_files)) if profile.code != "A" else len(graph_slice.impacted_files)
        blast_lines = graph_slice.total_lines_in_radius or (blast_files * 40)

        # Measure real token sizes of impacted files on disk for graph-grounded estimation
        actual_full_file_tokens = 0
        for rel_f in graph_slice.impacted_files:
            fp = self.root_dir / rel_f
            if fp.is_file():
                try:
                    actual_full_file_tokens += count_tokens(fp.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    actual_full_file_tokens += 400
            else:
                actual_full_file_tokens += 400

        blast_line_tokens = max(80, blast_lines * 6) if blast_files > 0 else 0

        # Multi-step Workflow Prediction Formula grounded in real symbol/file sizes:
        # Total = T_context + T_prompt + sum_{i=1..N}(T_context,i + T_tool_in,i + T_tool_out,i) + T_reasoning + T_output
        level_compression = {1: 1.0, 2: 0.85, 3: 0.68}[opt_level]
        max_slice_cap = 160 if profile.code == "A" else 360
        per_step_tool_out = int(
            (profile.avg_tool_out_tokens + min(max_slice_cap, blast_line_tokens // max(1, n_steps)))
            * level_compression
            * tool_mult
        )
        per_step_tool_in = profile.avg_tool_in_tokens

        # When grounded in live workspace telemetry (>2k context), every tool step re-sends
        # the full running context (1.0x) and KV-cache discounts are applied ONCE in _compute_model_costs().
        # Fresh workspaces with no telemetry retain the lightweight 0.25x prefix factor.
        step_ctx_factor = 1.0 if (has_telemetry and context_tokens > 2_000) else 0.25
        tool_loop_tokens = 0
        running_ctx = context_tokens + prompt_tok + (graph_slice.token_count if n_steps > 0 else 0)
        for _step in range(n_steps):
            step_tokens = int(running_ctx * step_ctx_factor) + per_step_tool_in + per_step_tool_out
            tool_loop_tokens += step_tokens
            running_ctx += per_step_tool_in + per_step_tool_out

        base_output = int(profile.base_output_tokens * level_compression)
        avg_out_per_step = int(session_ctx.get("avg_output_per_step") or 0)
        if has_telemetry and avg_out_per_step > 0:
            output_tokens = max(base_output, min(n_steps * 2_000, int(n_steps * avg_out_per_step)))
        else:
            output_tokens = base_output
        reasoning_tokens = int((prompt_tok + output_tokens + per_step_tool_out) * profile.reasoning_ratio * reason_mult)

        total_tokens = context_tokens + prompt_tok + tool_loop_tokens + reasoning_tokens + output_tokens

        # Unoptimized baseline grounded in full file reads + blind grep/test loops (bounded)
        unopt_steps = min(10, profile.unoptimized_steps + min(4, max(0, blast_files - 1)))
        per_step_unopt_out = max(
            profile.unoptimized_tool_out_tokens,
            actual_full_file_tokens // max(1, blast_files) if actual_full_file_tokens > 0 else profile.unoptimized_tool_out_tokens,
        )
        unopt_loop_tokens = unopt_steps * (
            context_tokens + prompt_tok + 140 + min(2400, per_step_unopt_out)
        )
        unopt_output = int(profile.base_output_tokens * 1.45)
        unopt_reasoning = int(reasoning_tokens * 1.7)
        unoptimized_tokens = max(
            context_tokens + prompt_tok + unopt_loop_tokens + unopt_reasoning + unopt_output,
            int(total_tokens * 1.35),
        )

        savings_tokens = max(0, unoptimized_tokens - total_tokens)
        savings_pct = round((savings_tokens / max(1, unoptimized_tokens)) * 100.0, 1)

        observed_cache_ratio = float(session_ctx.get("observed_cache_ratio") or 0.80)
        costs = self._compute_model_costs(
            input_tokens=context_tokens + prompt_tok + tool_loop_tokens,
            reasoning_tokens=reasoning_tokens,
            output_tokens=output_tokens,
            unoptimized_tokens=unoptimized_tokens,
            profile_code=profile.code,
            cache_hit_ratio=observed_cache_ratio,
        )

        matched_names = [str(s["name"]) for s in graph_slice.matched_symbols]
        forecast_id = f"tw_{uuid.uuid4().hex[:10]}"

        result = ForecastResult(
            forecast_id=forecast_id,
            prompt=prompt,
            profile_code=profile.code,
            profile_name=profile.name,
            optimization_level=opt_level,
            estimated_steps=n_steps,
            blast_radius_files=blast_files,
            blast_radius_lines=blast_lines,
            matched_symbols=matched_names,
            caller_count=len(graph_slice.callers),
            callee_count=len(graph_slice.callees),
            prompt_tokens=prompt_tok,
            context_tokens=context_tokens,
            tool_loop_tokens=tool_loop_tokens,
            reasoning_tokens=reasoning_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            unoptimized_tokens=unoptimized_tokens,
            savings_tokens=savings_tokens,
            savings_pct=savings_pct,
            costs=costs,
            banner="",
        )
        result.banner = self.render_forecast_banner(result)
        return result

    def _compute_model_costs(
        self,
        input_tokens: int,
        reasoning_tokens: int,
        output_tokens: int,
        unoptimized_tokens: int,
        profile_code: str = "B",
        cache_hit_ratio: float = 0.80,
    ) -> Dict[str, ModelCostEstimate]:
        models_cfg: Dict[str, Dict[str, object]] = self.pricing.get("models", {})  # type: ignore[assignment]
        per_unit = float(self.pricing.get("per_tokens", 1_000_000))
        scale_factors: Dict[str, float] = self.calibration.get("cost_scale_factors", {})  # type: ignore[assignment]
        estimates: Dict[str, ModelCostEstimate] = {}
        hit_ratio = max(0.50, min(0.96, float(cache_hit_ratio)))

        for model_id, m in models_cfg.items():
            in_rate = float(m["input_per_1m"]) / per_unit
            cached_rate = float(m["cached_input_per_1m"]) / per_unit
            out_rate = float(m["output_per_1m"]) / per_unit
            r_mult = float(m.get("reasoning_multiplier", 1.0))
            c_scale = float(scale_factors.get(f"{model_id}:{profile_code}", 1.0)) if isinstance(scale_factors, dict) else 1.0

            effective_out = output_tokens + int(reasoning_tokens * r_mult)
            uncached_usd = ((input_tokens * in_rate) + (effective_out * out_rate)) * c_scale
            # Apply observed workspace KV-cache hit rate (defaults to 80% on fresh workspaces)
            cached_input_part = int(input_tokens * hit_ratio)
            fresh_input_part = input_tokens - cached_input_part
            cached_usd = ((fresh_input_part * in_rate) + (cached_input_part * cached_rate) + (effective_out * out_rate)) * c_scale

            unopt_in = int(unoptimized_tokens * 0.82)
            unopt_out = unoptimized_tokens - unopt_in
            unopt_usd = (unopt_in * in_rate) + (unopt_out * out_rate)

            estimates[model_id] = ModelCostEstimate(
                model_id=model_id,
                provider=str(m["provider"]),
                display_name=str(m["display_name"]),
                uncached_cost_usd=round(uncached_usd, 6),
                cached_cost_usd=round(cached_usd, 6),
                unoptimized_cost_usd=round(unopt_usd, 6),
            )
        return estimates

    def render_forecast_banner(self, res: ForecastResult) -> str:
        defaults: Dict[str, str] = self.pricing.get("default_models", {})  # type: ignore[assignment]
        claude_m = res.costs.get(defaults.get("claude", "claude-sonnet-4"))
        codex_m = res.costs.get(defaults.get("codex", "o4-mini"))
        gemini_m = res.costs.get(defaults.get("gemini", "gemini-2.5-pro"))

        sym_label = res.matched_symbols[0] if res.matched_symbols else "repo"
        caller_info = f" + {res.caller_count} callers" if res.caller_count else ""
        blast_str = f"{res.blast_radius_files} files (`{sym_label}`{caller_info}, {res.blast_radius_lines} lines)"

        def fmt_usd(val: float) -> str:
            return f"${val:.4f}" if val < 0.01 else f"${val:.3f}"

        cost_parts: List[str] = []
        if claude_m:
            cost_parts.append(
                f"Claude Sonnet: **{fmt_usd(claude_m.uncached_cost_usd)}** (Cached: {fmt_usd(claude_m.cached_cost_usd)})"
            )
        if codex_m:
            cost_parts.append(f"Codex (`{codex_m.model_id}`): **{fmt_usd(codex_m.uncached_cost_usd)}**")
        if gemini_m:
            cost_parts.append(f"Gemini Pro: **{fmt_usd(gemini_m.uncached_cost_usd)}**")

        lines = [
            f"> **⚡ TokWiz Forecast** | **Complexity:** {res.profile_name} (Level {res.optimization_level}) | **Blast Radius:** {blast_str}",
            f"> * **Estimated Workflow:** ~{res.total_tokens:,} tokens *(vs. ~{res.unoptimized_tokens:,} unoptimized — **{res.savings_pct:.0f}% Est. Savings**)*",
            f"> * **Est. Cost:** {' · '.join(cost_parts)}",
        ]
        return "\n".join(lines)

    def record_forecast(self, res: ForecastResult) -> None:
        """Persist forecast to `.tokwiz/active-forecast.json` and `.tokwiz/forecasts.jsonl`."""
        tokwiz_dir = self.root_dir / ".tokwiz"
        tokwiz_dir.mkdir(parents=True, exist_ok=True)
        d = asdict(res)
        record = {
            "forecast_id": d["forecast_id"],
            "timestamp": time.time(),
            "prompt": d["prompt"],
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
        (tokwiz_dir / "active-forecast.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        with open(tokwiz_dir / "forecasts.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz predict",
        description="Forecast multi-step workflow tokens and $ cost ahead of prompt execution.",
    )
    parser.add_argument("prompt", help="User prompt to forecast")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--context-tokens", type=int, default=450, help="Existing conversation context tokens")
    parser.add_argument("--level", type=int, choices=[1, 2, 3], default=None, help="Force optimization level")
    parser.add_argument("--no-record", action="store_true", help="Do not persist forecast to .tokwiz/forecasts.jsonl")
    parser.add_argument("--json", action="store_true", help="Output full JSON forecast")
    args = parser.parse_args(argv)

    estimator = TokenEstimator(root_dir=Path(args.root))
    res = estimator.estimate(
        prompt=args.prompt,
        context_tokens=args.context_tokens,
        forced_level=args.level,
    )
    if not args.no_record:
        estimator.record_forecast(res)

    if args.json:
        payload = asdict(res)
        print(json.dumps(payload, indent=2))
    else:
        print(res.banner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
