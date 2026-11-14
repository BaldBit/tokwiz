"""Tier 1 & Tier 3 tests for Forecast vs. Real Cost Reconciliation, Self-Calibration, and Analytics."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
HOOKS_DIR = REPO_ROOT / "hooks"
DASHBOARD_DIR = REPO_ROOT / "dashboard"
EVAL_DIR = REPO_ROOT / "evaluations"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cal_mod = _load_module("calibration", SCRIPTS_DIR / "calibration.py")
tel_mod = _load_module("telemetry", SCRIPTS_DIR / "telemetry.py")
post_hook_mod = _load_module("post_turn_record", HOOKS_DIR / "post-turn-record.py")
pre_hook_mod = _load_module("pre_prompt_predict", HOOKS_DIR / "pre-prompt-predict.py")
srv_mod = _load_module("dashboard_server", DASHBOARD_DIR / "server.py")
replay_mod = _load_module("replay_transcripts", EVAL_DIR / "replay-transcripts.py")


class TestCostReconciliationAndCalibration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "auth.ts").write_text(
            "export function validateToken(t: string): boolean {\n  return t.length > 10;\n}\n"
            "export function requireAuth(h: string): boolean {\n  return validateToken(h);\n}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_multi_provider_usage_parsing(self) -> None:
        # 1. Claude Code usage payload
        u_claude = cal_mod.parse_usage_payload(
            {
                "agent_host": "claude",
                "model": "claude-sonnet-4",
                "real_steps": 5,
                "usage": {
                    "input_tokens": 600,
                    "cache_read_input_tokens": 2400,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 350,
                    "reasoning_tokens": 200,
                },
            }
        )
        self.assertEqual(u_claude.agent_host, "claude")
        self.assertEqual(u_claude.input_tokens, 3000)
        self.assertEqual(u_claude.cached_input_tokens, 2400)
        self.assertEqual(u_claude.output_tokens, 350)

        # 2. OpenAI Codex CLI usage payload
        u_codex = cal_mod.parse_usage_payload(
            {
                "agent_host": "codex",
                "model": "o4-mini",
                "real_steps": 4,
                "usage": {
                    "prompt_tokens": 2500,
                    "prompt_tokens_details": {"cached_tokens": 2000},
                    "completion_tokens": 600,
                    "completion_tokens_details": {"reasoning_tokens": 250},
                },
            }
        )
        self.assertEqual(u_codex.agent_host, "codex")
        self.assertEqual(u_codex.input_tokens, 2500)
        self.assertEqual(u_codex.cached_input_tokens, 2000)
        self.assertEqual(u_codex.reasoning_tokens, 250)
        self.assertEqual(u_codex.output_tokens, 350)

        # 3. Gemini CLI / Antigravity usageMetadata payload
        u_gemini = cal_mod.parse_usage_payload(
            {
                "agent_host": "gemini",
                "model": "gemini-2.5-pro",
                "real_steps": 3,
                "usageMetadata": {
                    "promptTokenCount": 1800,
                    "cachedContentTokenCount": 1400,
                    "candidatesTokenCount": 300,
                    "thoughtsTokenCount": 150,
                },
            }
        )
        self.assertEqual(u_gemini.agent_host, "gemini")
        self.assertEqual(u_gemini.input_tokens, 1800)
        self.assertEqual(u_gemini.cached_input_tokens, 1400)
        self.assertEqual(u_gemini.output_tokens, 300)
        self.assertEqual(u_gemini.reasoning_tokens, 150)

    def test_variance_root_cause_attribution(self) -> None:
        forecast = {
            "estimated_steps": 3,
            "tool_loop_tokens": 600,
            "reasoning_tokens": 200,
        }
        drifted_usage = cal_mod.ActualUsage(
            agent_host="claude",
            model_id="claude-sonnet-4",
            real_steps=8,              # > 3 * 1.35 -> EXTRA_TOOL_LOOPS
            input_tokens=4000,
            cached_input_tokens=400,   # 10% < 45% -> CACHE_MISS_DRIFT
            tool_output_tokens=1600,   # > 600 * 1.5 -> UNBOUNDED_TOOL_OUTPUT
            reasoning_tokens=600,      # > 200 * 1.5 -> REASONING_SPIKE
            output_tokens=300,
        )
        tags = cal_mod.attribute_variance_causes(forecast, drifted_usage)
        self.assertIn("EXTRA_TOOL_LOOPS", tags)
        self.assertIn("CACHE_MISS_DRIFT", tags)
        self.assertIn("REASONING_SPIKE", tags)
        self.assertIn("UNBOUNDED_TOOL_OUTPUT", tags)

    def test_pre_post_turn_reconciliation_and_telemetry_summary(self) -> None:
        # 1. Trigger PreInvocation hook to create .tokwiz/active-forecast.json
        pre_out = pre_hook_mod.handle_pre_prompt(
            {
                "hook_event_name": "PreInvocation",
                "prompt": "Refactor validateToken and update requireAuth",
                "cwd": str(self.root),
            }
        )
        forecast_id = pre_out["metadata"]["forecast_id"]

        # 2. Trigger PostInvocation hook with actual usage
        post_out = post_hook_mod.handle_post_turn(
            {
                "cwd": str(self.root),
                "agent_host": "claude",
                "model": "claude-sonnet-4",
                "real_steps": 6,
                "usage": {
                    "input_tokens": 900,
                    "cache_read_input_tokens": 3600,
                    "output_tokens": 410,
                    "reasoning_tokens": 260,
                },
            }
        )
        self.assertTrue(post_out["reconciled"])
        self.assertEqual(post_out["forecast_id"], forecast_id)

        # 3. Verify TelemetryStore summary & CLI report
        store = tel_mod.TelemetryStore(root_dir=self.root)
        summary = store.get_summary()
        self.assertEqual(summary["total_turns"], 1)
        self.assertGreater(summary["net_tokens_saved"], 0)
        self.assertAlmostEqual(summary["cache_hit_ratio_pct"], 80.0, places=1)
        report_str = tel_mod.render_cli_stats(summary)
        self.assertIn("TokWiz Optimization & Cost Report", report_str)
        self.assertIn(forecast_id, report_str)

    def test_ema_self_calibration_converges_under_20_pct_mape(self) -> None:
        # Simulate 8 historical turns with consistent repository-specific usage characteristics
        turns = [
            {
                "prompt": f"Refactor validateToken and requireAuth step {i}",
                "agent_host": "claude",
                "model": "claude-sonnet-4",
                "real_steps": 8,
                "usage": {
                    "input_tokens": 1100,
                    "cache_read_input_tokens": 4400,
                    "output_tokens": 520,
                    "reasoning_tokens": 340,
                },
            }
            for i in range(1, 9)
        ]
        res = replay_mod.replay_turns(self.root, turns, ema_alpha=0.45)
        self.assertEqual(res["turns_replayed"], 8)
        self.assertLessEqual(
            res["converged_mape_pct"],
            20.0,
            f"Expected converged MAPE <= 20%, got {res['converged_mape_pct']}%",
        )
        self.assertTrue((self.root / ".tokwiz" / "calibration.json").exists())

    def test_dashboard_http_server_endpoints(self) -> None:
        store = tel_mod.TelemetryStore(root_dir=self.root)
        store.record_turn(
            {
                "forecast_id": "tw_testdash",
                "prompt": "Refactor validateToken",
                "profile_code": "C",
                "optimization_level": 2,
                "forecast_total_tokens": 4000,
                "real_total_tokens": 4100,
                "unoptimized_baseline_tokens": 12000,
                "net_tokens_saved": 7610,
                "forecast_cost_usd": 0.015,
                "real_cost_usd": 0.0155,
                "unoptimized_cost_usd": 0.045,
                "net_cost_saved_usd": 0.0295,
                "cost_delta_usd": 0.0005,
                "ape_pct": 3.2,
                "variance_tags": ["ON_TARGET"],
            }
        )
        handler = srv_mod.create_handler(self.root)
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/stats") as resp:
                self.assertEqual(resp.status, 200)
                api_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(api_data["total_turns"], 1)
                self.assertEqual(api_data["turns"][0]["forecast_id"], "tw_testdash")

            with urlopen(f"http://127.0.0.1:{port}/") as resp_html:
                self.assertEqual(resp_html.status, 200)
                html = resp_html.read().decode("utf-8")
                self.assertIn("TokWiz Optimization & Cost Analytics", html)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
