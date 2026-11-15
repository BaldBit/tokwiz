#!/usr/bin/env python3
"""Tests for the automated headless Live A/B runner (`evaluations/run-live-ab.py`)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_AB_PATH = REPO_ROOT / "evaluations" / "run-live-ab.py"

spec = importlib.util.spec_from_file_location("run_live_ab", str(LIVE_AB_PATH))
assert spec and spec.loader
live_ab = importlib.util.module_from_spec(spec)
sys.modules["run_live_ab"] = live_ab
spec.loader.exec_module(live_ab)


class TestLiveABRunner(unittest.TestCase):
    def test_parse_agent_usage_json_across_providers(self) -> None:
        claude_out = json.dumps(
            {
                "total_cost_usd": 0.014,
                "usage": {
                    "input_tokens": 1200,
                    "cache_read_input_tokens": 3000,
                    "output_tokens": 400,
                },
            }
        )
        inp, cached, out, total, cost = live_ab.parse_agent_usage_json(claude_out, "claude")
        self.assertEqual((inp, cached, out, total), (1200, 3000, 400, 4600))
        self.assertAlmostEqual(cost or 0.0, 0.014, places=4)

        codex_ndjson = (
            '{"type":"thread.started"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":900,"cached_input_tokens":500,"output_tokens":200}}\n'
        )
        inp, cached, out, total, _ = live_ab.parse_agent_usage_json(codex_ndjson, "codex")
        self.assertEqual((inp, cached, out, total), (900, 500, 200, 1600))

        gemini_out = json.dumps(
            {
                "stats": {
                    "models": {
                        "gemini-2.5-pro": {
                            "tokens": {"prompt": 800, "cached": 200, "candidates": 150, "thoughts": 50}
                        }
                    }
                }
            }
        )
        inp, cached, out, total, _ = live_ab.parse_agent_usage_json(gemini_out, "gemini")
        self.assertEqual((inp, cached, out, total), (800, 200, 200, 1200))

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mcp_file = Path(td) / ".mcp.json"
            mcp_file.write_text("{}", encoding="utf-8")
            cmd_with_mcp = live_ab.build_agent_command("claude", "test", mcp_config_path=mcp_file)
            self.assertIn("--mcp-config", cmd_with_mcp)
            self.assertIn(str(mcp_file), cmd_with_mcp)
            cmd_no_mcp = live_ab.build_agent_command("claude", "test")
            self.assertNotIn("--mcp-config", cmd_no_mcp)

    def test_run_live_ab_with_mocked_runner_and_dry_run_cli(self) -> None:
        def fake_runner(cmd, cwd: Path, timeout_sec: int):
            is_tokwiz = cwd.name == "project-tokwiz"
            payload = {
                "total_cost_usd": 0.005 if is_tokwiz else 0.020,
                "usage": {
                    "input_tokens": 1500 if is_tokwiz else 6000,
                    "cache_read_input_tokens": 500,
                    "output_tokens": 250 if is_tokwiz else 800,
                },
            }
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )

        report = live_ab.run_live_ab(
            agent="claude",
            tasks=[{"id": "t1", "name": "Auth Refactor", "prompt": "Refactor validate_token"}],
            runner=fake_runner,
        )
        self.assertEqual(report["baseline_total_tokens"], 7300)
        self.assertEqual(report["tokwiz_total_tokens"], 2250)
        self.assertEqual(report["net_saved_tokens"], 5050)
        self.assertGreater(report["net_reduction_pct"], 65.0)
        self.assertTrue(report["baseline_tests"]["passed"])
        self.assertTrue(report["tokwiz_tests"]["passed"])
        md = live_ab.render_live_ab_markdown(report)
        self.assertIn("TokWiz Live A/B Report", md)
        self.assertIn("+5,050", md)

        # Verify `tokwiz live-ab --dry-run --json` works via CLI without calling an LLM
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "cli.py"),
                "live-ab",
                "--agent",
                "claude",
                "--prompt",
                "Ping",
                "--dry-run",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        dry_report = json.loads(proc.stdout)
        self.assertTrue(dry_report["dry_run"])
        self.assertTrue(dry_report["baseline_tests"]["passed"])
        self.assertTrue(dry_report["tokwiz_tests"]["passed"])


if __name__ == "__main__":
    unittest.main()
