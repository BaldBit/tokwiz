#!/usr/bin/env python3
"""Forecast vs. Real Cost Reconciliation & Adaptive Self-Calibration (`scripts/calibration.py`).

Parses provider usage blocks from Claude Code, OpenAI Codex CLI, and Gemini/Antigravity
transcripts, computes real billed cost ($), attributes variance root causes
(`EXTRA_TOOL_LOOPS`, `CACHE_MISS_DRIFT`, `REASONING_SPIKE`, `UNBOUNDED_TOOL_OUTPUT`),
and updates `.tokwiz/calibration.json` via Exponential Moving Average (EMA).
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_est_mod = _load_script_module("token_estimator", "token-estimator.py")
_tel_mod = _load_script_module("telemetry", "telemetry.py")
load_pricing_matrix = _est_mod.load_pricing_matrix
count_tokens = _est_mod.count_tokens
TelemetryStore = _tel_mod.TelemetryStore


@dataclass
class ActualUsage:
    agent_host: str
    model_id: str
    real_steps: int
    input_tokens: int
    cached_input_tokens: int
    tool_output_tokens: int
    reasoning_tokens: int
    output_tokens: int

    @property
    def uncached_tokens(self) -> int:
        uncached_in = max(0, self.input_tokens - self.cached_input_tokens)
        return uncached_in + self.reasoning_tokens + self.output_tokens

    @property
    def effective_tokens(self) -> int:
        """Cache-weighted billing-equivalent tokens (cache reads weighted at 0.1x)."""
        uncached_in = max(0, self.input_tokens - self.cached_input_tokens)
        return uncached_in + int(round(self.cached_input_tokens * 0.1)) + self.reasoning_tokens + self.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.reasoning_tokens + self.output_tokens


def parse_usage_payload(payload: Dict[str, object]) -> ActualUsage:
    """Normalize usage payloads from Claude Code, OpenAI Codex CLI, or Gemini/Antigravity."""
    agent_host = str(payload.get("agent_host") or payload.get("provider") or "").lower()
    model_id = str(payload.get("model") or payload.get("model_id") or "")

    usage = payload.get("usage") or payload.get("usageMetadata") or payload
    if not isinstance(usage, dict):
        usage = {}

    # 1. Claude Code style (`input_tokens`, `cache_read_input_tokens`, `output_tokens`)
    if "input_tokens" in usage or agent_host == "claude":
        agent_host = agent_host or "claude"
        model_id = model_id or "claude-sonnet-4"
        raw_in = int(usage.get("input_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", usage.get("cached_input_tokens", 0)))
        cache_create = int(usage.get("cache_creation_input_tokens", 0))
        total_in = raw_in + cache_read + cache_create if "cache_read_input_tokens" in usage else raw_in
        out_tok = int(usage.get("output_tokens", 0))
        reason_tok = int(usage.get("reasoning_tokens", usage.get("thinking_tokens", 0)))
        cached_clamped = min(total_in, cache_read)
        uncached_in = max(0, total_in - cached_clamped)
        tool_tok = int(usage.get("tool_output_tokens", int(uncached_in * 0.35)))
        steps = int(payload.get("real_steps", usage.get("tool_steps", 3)))
        return ActualUsage(
            agent_host=agent_host,
            model_id=model_id,
            real_steps=steps,
            input_tokens=total_in,
            cached_input_tokens=cached_clamped,
            tool_output_tokens=tool_tok,
            reasoning_tokens=reason_tok,
            output_tokens=out_tok,
        )

    # 2. OpenAI Codex CLI style (`prompt_tokens`, `completion_tokens`, `prompt_tokens_details`)
    if "prompt_tokens" in usage or agent_host == "codex":
        agent_host = agent_host or "codex"
        model_id = model_id or "o4-mini"
        total_in = int(usage.get("prompt_tokens", 0))
        p_details = usage.get("prompt_tokens_details") or {}
        cached_in = int(p_details.get("cached_tokens", usage.get("cached_input_tokens", 0))) if isinstance(p_details, dict) else 0
        c_details = usage.get("completion_tokens_details") or {}
        reason_tok = int(c_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0))) if isinstance(c_details, dict) else 0
        comp_tok = int(usage.get("completion_tokens", 0))
        out_tok = max(0, comp_tok - reason_tok) if comp_tok >= reason_tok else comp_tok
        tool_tok = int(usage.get("tool_output_tokens", int(total_in * 0.35)))
        steps = int(payload.get("real_steps", usage.get("tool_steps", 3)))
        return ActualUsage(
            agent_host=agent_host,
            model_id=model_id,
            real_steps=steps,
            input_tokens=total_in,
            cached_input_tokens=min(total_in, cached_in),
            tool_output_tokens=tool_tok,
            reasoning_tokens=reason_tok,
            output_tokens=out_tok,
        )

    # 3. Gemini CLI / Antigravity style (`promptTokenCount`, `cachedContentTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount`)
    agent_host = agent_host or "gemini"
    model_id = model_id or "gemini-2.5-pro"
    total_in = int(usage.get("promptTokenCount", usage.get("total_input_tokens", 0)))
    cached_in = int(usage.get("cachedContentTokenCount", usage.get("cached_input_tokens", 0)))
    out_tok = int(usage.get("candidatesTokenCount", usage.get("output_tokens", 0)))
    reason_tok = int(usage.get("thoughtsTokenCount", usage.get("reasoning_tokens", 0)))
    tool_tok = int(usage.get("tool_output_tokens", int(total_in * 0.35)))
    steps = int(payload.get("real_steps", usage.get("tool_steps", 3)))
    return ActualUsage(
        agent_host=agent_host,
        model_id=model_id,
        real_steps=steps,
        input_tokens=total_in,
        cached_input_tokens=min(total_in, cached_in),
        tool_output_tokens=tool_tok,
        reasoning_tokens=reason_tok,
        output_tokens=out_tok,
    )


def compute_real_cost_usd(
    usage: ActualUsage,
    pricing: Optional[Dict[str, object]] = None,
) -> float:
    pricing_data = pricing or load_pricing_matrix()
    models: Dict[str, Dict[str, object]] = pricing_data.get("models", {})  # type: ignore[assignment]
    defaults: Dict[str, str] = pricing_data.get("default_models", {})  # type: ignore[assignment]
    per_unit = float(pricing_data.get("per_tokens", 1_000_000))

    m_cfg = models.get(usage.model_id)
    if not m_cfg:
        fallback_id = defaults.get(usage.agent_host, "claude-sonnet-4")
        m_cfg = models.get(fallback_id, list(models.values())[0])

    in_rate = float(m_cfg["input_per_1m"]) / per_unit
    cached_rate = float(m_cfg["cached_input_per_1m"]) / per_unit
    out_rate = float(m_cfg["output_per_1m"]) / per_unit

    uncached_in = max(0, usage.input_tokens - usage.cached_input_tokens)
    total_out = usage.output_tokens + usage.reasoning_tokens
    cost = (uncached_in * in_rate) + (usage.cached_input_tokens * cached_rate) + (total_out * out_rate)
    return round(cost, 6)


def attribute_variance_causes(
    forecast: Dict[str, object],
    usage: ActualUsage,
) -> List[str]:
    """Identify root causes when real usage drifts from forecast."""
    tags: List[str] = []
    f_steps = max(1, int(forecast.get("estimated_steps", 1)))
    f_tool = max(1, int(forecast.get("tool_loop_tokens", 200)))
    f_reason = max(1, int(forecast.get("reasoning_tokens", 100)))
    f_total = max(1, int(forecast.get("total_tokens", 1000)))

    effective_total = usage.effective_tokens

    if (
        usage.real_steps > f_steps + 2
        and usage.real_steps >= int(f_steps * 1.4)
        and effective_total > int(f_total * 1.15)
    ):
        tags.append("EXTRA_TOOL_LOOPS")

    cache_ratio = usage.cached_input_tokens / max(1, usage.input_tokens)
    if usage.input_tokens > 500 and cache_ratio < 0.45:
        tags.append("CACHE_MISS_DRIFT")

    if usage.reasoning_tokens > f_reason * 1.5 and (usage.reasoning_tokens - f_reason) > 200 and effective_total > int(f_total * 1.15):
        tags.append("REASONING_SPIKE")

    if usage.tool_output_tokens > f_tool * 1.5 and (usage.tool_output_tokens - f_tool) > 300 and effective_total > int(f_total * 1.15):
        tags.append("UNBOUNDED_TOOL_OUTPUT")

    if not tags:
        tags.append("ON_TARGET")
    return tags


class CalibrationEngine:
    """Reconciles Forecast vs. Real turns and updates EMA weights in `.tokwiz/calibration.json`."""

    def __init__(self, root_dir: Path | str = ".", ema_alpha: float = 0.35) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.ema_alpha = ema_alpha
        self.cal_path = self.root_dir / ".tokwiz" / "calibration.json"
        self.pricing = load_pricing_matrix()
        self.telemetry = TelemetryStore(root_dir=self.root_dir)

    def load_weights(self) -> Dict[str, object]:
        if self.cal_path.exists():
            try:
                return json.loads(self.cal_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "version": 1,
            "turns_calibrated": 0,
            "profile_step_multipliers": {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0},
            "tool_output_multipliers": {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0},
            "reasoning_multipliers": {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0},
            "cost_scale_factors": {},
        }

    def save_weights(self, weights: Dict[str, object]) -> None:
        self.cal_path.parent.mkdir(parents=True, exist_ok=True)
        self.cal_path.write_text(json.dumps(weights, indent=2) + "\n", encoding="utf-8")

    def reconcile_turn(
        self,
        forecast: Dict[str, object],
        usage_payload: Dict[str, object],
        skill_overhead_tokens: int = 290,
    ) -> Dict[str, object]:
        usage = parse_usage_payload(usage_payload)
        weights = self.load_weights()
        profile_code = str(forecast.get("profile_code", "B"))

        # Look up forecasted cost for the active model
        costs_map = forecast.get("costs", {})
        model_cost_info = costs_map.get(usage.model_id) if isinstance(costs_map, dict) else None
        if isinstance(model_cost_info, dict):
            base_f_cost = float(model_cost_info.get("cached_cost_usd") or model_cost_info.get("uncached_cost_usd") or 0.0)
            unopt_cost = float(model_cost_info.get("unoptimized_cost_usd") or base_f_cost * 2.5)
        elif hasattr(model_cost_info, "cached_cost_usd"):
            base_f_cost = float(model_cost_info.cached_cost_usd or model_cost_info.uncached_cost_usd)
            unopt_cost = float(model_cost_info.unoptimized_cost_usd)
        else:
            base_f_cost = 0.005
            unopt_cost = 0.015

        # Preserve the exact forecast tokens and cost that were predicted pre-prompt
        # (learned EMA multipliers in calibration.json are applied by TokenEstimator on future forecasts)
        scale_key = f"{usage.model_id}:{profile_code}"
        scale_map: Dict[str, float] = weights.setdefault("cost_scale_factors", {})  # type: ignore[assignment]

        f_total_tok = int(forecast.get("total_tokens", 1000))
        f_cost = round(base_f_cost, 6)
        r_cost = compute_real_cost_usd(usage, self.pricing)

        f_in_tok = int(forecast.get("context_tokens", 0)) + int(forecast.get("prompt_tokens", 0)) + int(forecast.get("tool_loop_tokens", 0))
        f_tool_tok = int(forecast.get("tool_loop_tokens", 0))
        f_reason_tok = int(forecast.get("reasoning_tokens", 0))
        f_out_tok = int(forecast.get("output_tokens", 0))

        telemetry_source = str(usage_payload.get("telemetry_source", "API_EXACT"))
        transcript_path = str(usage_payload.get("transcript_path", ""))
        raw_receipts = usage_payload.get("receipts")

        ab_base_tok = int(usage_payload.get("real_baseline_tokens", 0))
        ab_base_steps = int(usage_payload.get("real_baseline_steps", 0))
        ab_base_cost = float(usage_payload.get("real_baseline_cost_usd", 0.0))

        models_cfg: Dict[str, Dict[str, object]] = self.pricing.get("models", {})  # type: ignore[assignment]
        per_unit = float(self.pricing.get("per_tokens", 1_000_000))
        m_cfg = models_cfg.get(usage.model_id) or models_cfg.get("claude-sonnet-4") or {}
        in_rate = float(m_cfg.get("input_per_1m", 3.0)) / per_unit
        cached_rate = float(m_cfg.get("cached_input_per_1m", 0.3)) / per_unit
        blended_in_rate = (0.35 * in_rate) + (0.65 * cached_rate)

        if isinstance(raw_receipts, list):
            receipts: List[Dict[str, object]] = [r for r in raw_receipts if isinstance(r, dict)]
            net_saved_tok = sum(max(0, int(r.get("saved_tokens", 0))) for r in receipts)
            unopt_tok = usage.effective_tokens + net_saved_tok
            net_saved_cost = round(net_saved_tok * blended_in_rate, 6)
            unopt_cost = round(r_cost + net_saved_cost, 6)
        else:
            eff_tok = usage.effective_tokens
            unopt_tok = int(forecast.get("unoptimized_tokens", max(f_total_tok * 2, eff_tok + 1000)))
            net_saved_tok = max(0, unopt_tok - (eff_tok + skill_overhead_tokens))
            net_saved_cost = round(max(0.0, unopt_cost - r_cost), 6)
            receipts = [
                {
                    "type": "WORKFLOW_COMPRESSION",
                    "target": "session_turn",
                    "full_tokens": unopt_tok,
                    "actual_tokens": eff_tok,
                    "saved_tokens": net_saved_tok,
                    "detail": f"Workflow context & tool output compression ({eff_tok:,} eff tok vs {unopt_tok:,} tok)",
                }
            ]

        token_delta = usage.total_tokens - f_total_tok
        cost_delta = round(r_cost - f_cost, 6)
        ape_pct = round((abs(cost_delta) / max(1e-6, r_cost)) * 100.0, 2)
        variance_tags = attribute_variance_causes(forecast, usage)

        record = {
            "forecast_id": str(forecast.get("forecast_id", f"tw_{int(time.time()*1000)}")),
            "timestamp": float(forecast.get("timestamp", time.time())),
            "agent_host": usage.agent_host,
            "model_id": usage.model_id,
            "prompt": str(forecast.get("prompt", "")),
            "profile_code": profile_code,
            "optimization_level": int(forecast.get("optimization_level", 1)),
            "forecast_steps": int(forecast.get("estimated_steps", 1)),
            "real_steps": usage.real_steps,
            "forecast_input_tokens": f_in_tok,
            "real_input_tokens": usage.input_tokens,
            "real_cached_input_tokens": usage.cached_input_tokens,
            "forecast_tool_tokens": f_tool_tok,
            "real_tool_tokens": usage.tool_output_tokens,
            "forecast_reasoning_tokens": f_reason_tok,
            "real_reasoning_tokens": usage.reasoning_tokens,
            "forecast_output_tokens": f_out_tok,
            "real_output_tokens": usage.output_tokens,
            "forecast_total_tokens": f_total_tok,
            "real_total_tokens": usage.total_tokens,
            "unoptimized_baseline_tokens": unopt_tok,
            "skill_overhead_tokens": skill_overhead_tokens,
            "net_tokens_saved": net_saved_tok,
            "forecast_cost_usd": f_cost,
            "real_cost_usd": r_cost,
            "unoptimized_cost_usd": unopt_cost,
            "net_cost_saved_usd": net_saved_cost,
            "token_delta": token_delta,
            "cost_delta_usd": cost_delta,
            "ape_pct": ape_pct,
            "variance_tags": variance_tags,
            "telemetry_source": telemetry_source,
            "transcript_path": transcript_path,
            "receipts": receipts,
            "ab_baseline_tokens": ab_base_tok,
            "ab_baseline_steps": ab_base_steps,
            "ab_baseline_cost_usd": ab_base_cost,
        }

        self.telemetry.record_turn(record)
        self._update_ema_weights(weights, forecast, usage, base_f_cost, r_cost, scale_key)
        return record

    def _update_ema_weights(
        self,
        weights: Dict[str, object],
        forecast: Dict[str, object],
        usage: ActualUsage,
        base_f_cost: float,
        r_cost: float,
        scale_key: str,
    ) -> None:
        alpha = self.ema_alpha
        p_code = str(forecast.get("profile_code", "B"))
        f_steps = max(1, int(forecast.get("estimated_steps", 1)))
        f_tool = max(1, int(forecast.get("tool_loop_tokens", 200)))
        f_reason = max(1, int(forecast.get("reasoning_tokens", 100)))

        step_map: Dict[str, float] = weights.setdefault("profile_step_multipliers", {})  # type: ignore[assignment]
        tool_map: Dict[str, float] = weights.setdefault("tool_output_multipliers", {})  # type: ignore[assignment]
        reason_map: Dict[str, float] = weights.setdefault("reasoning_multipliers", {})  # type: ignore[assignment]
        scale_map: Dict[str, float] = weights.setdefault("cost_scale_factors", {})  # type: ignore[assignment]

        obs_step_ratio = max(0.3, min(3.0, usage.real_steps / f_steps))
        prev_step = float(step_map.get(p_code, 1.0))
        step_map[p_code] = round((1.0 - alpha) * prev_step + alpha * obs_step_ratio, 4)

        obs_tool_ratio = max(0.3, min(3.0, usage.tool_output_tokens / f_tool))
        prev_tool = float(tool_map.get(p_code, 1.0))
        tool_map[p_code] = round((1.0 - alpha) * prev_tool + alpha * obs_tool_ratio, 4)

        obs_reason_ratio = max(0.3, min(3.0, usage.reasoning_tokens / f_reason))
        prev_reason = float(reason_map.get(p_code, 1.0))
        reason_map[p_code] = round((1.0 - alpha) * prev_reason + alpha * obs_reason_ratio, 4)

        if base_f_cost > 0 and r_cost > 0:
            obs_cost_ratio = max(0.2, min(5.0, r_cost / base_f_cost))
            prev_scale = float(scale_map.get(scale_key, 1.0))
            scale_map[scale_key] = round((1.0 - alpha) * prev_scale + alpha * obs_cost_ratio, 4)

        weights["turns_calibrated"] = int(weights.get("turns_calibrated", 0)) + 1
        self.save_weights(weights)
