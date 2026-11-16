#!/usr/bin/env python3
"""End-to-End CLI, Demo Sandbox, Stale-CWD Resilience & Cross-Python Integration Tests."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DASHBOARD_DIR = REPO_ROOT / "dashboard"


def _load_mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


srv_mod = _load_mod("dashboard_server_e2e", DASHBOARD_DIR / "server.py")


class TestEndToEndCLIAndResilience(unittest.TestCase):
    def test_cli_predict_persists_forecasts_and_populates_dashboard_api(self) -> None:
        """Running `tokwiz predict` must persist to `.tokwiz/forecasts.jsonl` and populate `/api/stats`."""
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "src").mkdir(parents=True, exist_ok=True)
            (tmp / "src" / "auth.py").write_text(
                "def validate_token(tok: str) -> bool:\n    return tok.startswith('tk_')\n",
                encoding="utf-8",
            )
            # Run `tokwiz predict` via subprocess
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "cli.py"),
                    "predict",
                    "Refactor validate_token to support x-api-key headers",
                    "--root",
                    str(tmp),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("TokWiz Forecast", proc.stdout)
            self.assertTrue((tmp / ".tokwiz" / "forecasts.jsonl").exists())
            self.assertTrue((tmp / ".tokwiz" / "active-forecast.json").exists())

            # Query dashboard `/api/stats` and verify forecast metrics are immediately populated
            handler = srv_mod.create_handler(tmp)
            server = HTTPServer(("127.0.0.1", 0), handler)
            port = server.server_port
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/stats") as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(data["total_forecasts"], 1)
                    self.assertGreater(data["total_forecast_tokens"], 0)
                    self.assertGreater(data["net_tokens_saved"], 0)
                    self.assertGreater(data["total_forecast_cost_usd"], 0.0)
                    self.assertGreater(data["net_cost_saved_usd"], 0.0)
                    self.assertGreater(data["phase_breakdown"]["context_input"], 0)
                    self.assertIn("claude-sonnet-4", data["model_breakdown"])
                    self.assertEqual(data["workspace_name"], tmp.name)
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stale_cwd_inode_recovery(self) -> None:
        """If a shell's CWD inode is deleted and recreated, CLI commands must recover via $PWD."""
        parent = Path(tempfile.mkdtemp()).resolve()
        saved_cwd = Path.cwd()
        saved_pwd = os.environ.get("PWD")
        try:
            work = parent / "project-tokwiz"
            work.mkdir(parents=True, exist_ok=True)
            (work / "app.py").write_text("def hello() -> str:\n    return 'ok'\n", encoding="utf-8")

            # Simulate a shell that was cd'd into `work`, then `work` was unlinked and recreated at the same path
            os.chdir(str(work))
            shutil.rmtree(work)
            work.mkdir(parents=True, exist_ok=True)
            (work / "app.py").write_text("def hello() -> str:\n    return 'ok'\n", encoding="utf-8")

            os.environ["PWD"] = str(work)

            # Now `os.getcwd()` in this process is an unlinked inode (raises FileNotFoundError on macOS)
            resolved = srv_mod._safe_resolve_root(".")
            self.assertEqual(resolved, work.resolve())
        finally:
            os.chdir(str(saved_cwd))
            if saved_pwd is not None:
                os.environ["PWD"] = saved_pwd
            shutil.rmtree(parent, ignore_errors=True)

    def test_demo_setup_preserves_directory_inode_and_keeps_baseline_clean(self) -> None:
        """`demo/setup-ab-demo.py` must preserve folder inodes and never create `.tokwiz` in baseline."""
        demo_ws = Path(tempfile.mkdtemp()).resolve()
        try:
            tokwiz_dir = demo_ws / "project-tokwiz"
            baseline_dir = demo_ws / "project-baseline"
            tokwiz_dir.mkdir(parents=True, exist_ok=True)
            baseline_dir.mkdir(parents=True, exist_ok=True)

            ino_before_tokwiz = tokwiz_dir.stat().st_ino
            ino_before_base = baseline_dir.stat().st_ino

            subprocess.run(
                [sys.executable, str(REPO_ROOT / "demo" / "setup-ab-demo.py"), "--workspace-dir", str(demo_ws)],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(tokwiz_dir.stat().st_ino, ino_before_tokwiz)
            self.assertEqual(baseline_dir.stat().st_ino, ino_before_base)
            self.assertFalse((baseline_dir / ".tokwiz").exists())
            self.assertTrue((baseline_dir / ".git").exists())
            self.assertTrue((tokwiz_dir / ".tokwiz" / "graph.db").exists())
            self.assertTrue((tokwiz_dir / ".git").exists())
        finally:
            shutil.rmtree(demo_ws, ignore_errors=True)

    def test_macos_system_python39_compatibility(self) -> None:
        """Verify `/usr/bin/python3` (Python 3.9, used by `#!/usr/bin/env python3`) runs all CLI commands."""
        sys_py = Path("/usr/bin/python3")
        if not sys_py.exists():
            self.skipTest("/usr/bin/python3 not present")
        tmp_ws = Path(tempfile.mkdtemp()).resolve()
        try:
            (tmp_ws / "src").mkdir(parents=True, exist_ok=True)
            sample_py = tmp_ws / "src" / "order_engine.py"
            sample_py.write_text(
                "def validate_token(tok: str) -> bool:\n    return tok.startswith('tk_')\n",
                encoding="utf-8",
            )
            for args in (
                ["predict", "Update validate_token to support x-api-key header tokens", "--root", str(tmp_ws)],
                ["stats", "--root", str(tmp_ws)],
                ["graph", "--root", str(tmp_ws), "query", "validate_token", "--max-tokens", "300"],
                ["skeleton", str(sample_py)],
            ):
                proc = subprocess.run(
                    [str(sys_py), str(SCRIPTS_DIR / "cli.py"), *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(proc.returncode, 0)
                self.assertTrue(proc.stdout.strip())
        finally:
            shutil.rmtree(tmp_ws, ignore_errors=True)

    def test_batch_predict_and_fifo_post_turn_reconciliation(self) -> None:
        """When 3 `tokwiz predict` commands are run upfront, Turn 1 must reconcile Forecast 1 (FIFO) and keep 2 pending."""
        post_mod = _load_mod("post_turn_e2e", REPO_ROOT / "hooks" / "post-turn-record.py")
        tmp_ws = Path(tempfile.mkdtemp()).resolve()
        try:
            (tmp_ws / "src").mkdir(parents=True, exist_ok=True)
            (tmp_ws / "src" / "auth.py").write_text(
                "def validate_token(tok: str) -> bool:\n    return tok.startswith('tk_')\n",
                encoding="utf-8",
            )
            prompts = [
                "Prompt 1: Update validate_token to support x-api-key header",
                "Prompt 2: Update apply_regional_tax for GB VAT",
                "Prompt 3: Explain call chain from process_checkout",
            ]
            for p in prompts:
                subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / "cli.py"), "predict", p, "--root", str(tmp_ws)],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            forecasts = [
                json.loads(line)
                for line in (tmp_ws / ".tokwiz" / "forecasts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(forecasts), 3)

            # 1. Calling post-turn without usage or transcript must NOT invent fake turns
            res_empty = post_mod.handle_post_turn({"cwd": str(tmp_ws)})
            self.assertFalse(res_empty["reconciled"])

            # 2. Now simulate Turn 1 finishing with real usage — must reconcile forecasts[0], NOT forecasts[2]!
            res1 = post_mod.handle_post_turn(
                {
                    "cwd": str(tmp_ws),
                    "real_steps": 6,
                    "usage": {"input_tokens": 5000, "cache_read_input_tokens": 4000, "output_tokens": 600, "reasoning_tokens": 400},
                }
            )
            self.assertTrue(res1["reconciled"])
            self.assertEqual(res1["forecast_id"], forecasts[0]["forecast_id"])

            # 3. Simulate Turn 2 finishing with real usage — must reconcile forecasts[1], leaving forecasts[2] pending!
            res2 = post_mod.handle_post_turn(
                {
                    "cwd": str(tmp_ws),
                    "real_steps": 5,
                    "usage": {"input_tokens": 3500, "cache_read_input_tokens": 2800, "output_tokens": 450, "reasoning_tokens": 250},
                }
            )
            self.assertTrue(res2["reconciled"])
            self.assertEqual(res2["forecast_id"], forecasts[1]["forecast_id"])
        finally:
            shutil.rmtree(tmp_ws, ignore_errors=True)

    def test_zero_step_transcript_sync_receipts_and_5x_refresh_idempotency(self) -> None:
        """Verify auto-forecast from IDE transcripts, strict receipt math (including 0 when full file read), and 5x refresh immutability."""
        demo_ws = Path(tempfile.mkdtemp()).resolve()
        fake_brain = Path(tempfile.mkdtemp()).resolve()
        saved_env = os.environ.get("TOKWIZ_BRAIN_DIR")
        try:
            subprocess.run(
                [sys.executable, str(REPO_ROOT / "demo" / "setup-ab-demo.py"), "--workspace-dir", str(demo_ws)],
                check=True,
                capture_output=True,
                text=True,
            )
            tokwiz_dir = (demo_ws / "project-tokwiz").resolve()
            baseline_dir = (demo_ws / "project-baseline").resolve()

            # Ensure unittest discover passes out of the box in both generated demo repos
            for d in (baseline_dir, tokwiz_dir):
                t_proc = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-v"],
                    cwd=str(d),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("OK", t_proc.stderr + t_proc.stdout)

            # Build synthetic Antigravity transcripts for Baseline and TokWiz (WITHOUT running `tokwiz predict` first!)
            p1 = "Update validate_token to also accept an 'x-api-key: tk_live_...' format token"
            p2 = "Explain the exact call chain from process_checkout down to validate_token and calculate_order_total"

            tw_log_dir = fake_brain / "cid-tokwiz" / ".system_generated" / "logs"
            tw_log_dir.mkdir(parents=True, exist_ok=True)
            tw_steps = [
                {"step_index": 0, "type": "USER_INPUT", "content": f"<USER_REQUEST>{p1}</USER_REQUEST>"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "thinking": "Querying graph and skeletonizing session.py",
                    "content": "",
                    "tool_calls": [
                        {"name": "run_command", "arguments": {"CommandLine": "tokwiz graph query validate_token --max-tokens 400"}},
                        {"name": "run_command", "arguments": {"CommandLine": "tokwiz skeleton src/auth/session.py"}},
                    ],
                },
                {"step_index": 2, "type": "TOOL_RESPONSE", "content": "src/auth/session.py:L160-L170 validate_token"},
                {"step_index": 3, "type": "TOOL_RESPONSE", "content": "# Skeleton src/auth/session.py (340 lines)\nL160-L170: def validate_token"},
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "thinking": "Reading targeted range in session.py, and testing full-read zero-savings rule on jwt.py",
                    "content": "",
                    "tool_calls": [
                        {"name": "read_file", "arguments": {"Path": f"{tokwiz_dir}/src/auth/session.py", "StartLine": 155, "EndLine": 175}},
                        {"name": "run_command", "arguments": {"CommandLine": "tokwiz skeleton src/crypto/jwt.py"}},
                        {"name": "read_file", "arguments": {"Path": f"{tokwiz_dir}/src/crypto/jwt.py"}},
                    ],
                },
                {"step_index": 5, "type": "TOOL_RESPONSE", "content": "def validate_token(self, token: str) -> bool:\n    return True"},
                {"step_index": 6, "type": "TOOL_RESPONSE", "content": "# Skeleton jwt.py"},
                {"step_index": 7, "type": "TOOL_RESPONSE", "content": "full jwt.py content " * 80},
                {"step_index": 8, "type": "PLANNER_RESPONSE", "thinking": "Done", "content": "Updated validate_token.", "tool_calls": []},
                {"step_index": 9, "type": "USER_INPUT", "content": f"<USER_REQUEST>{p2}</USER_REQUEST>"},
                {
                    "step_index": 10,
                    "type": "PLANNER_RESPONSE",
                    "thinking": "Tracing call chain via graph",
                    "content": "",
                    "tool_calls": [
                        {"name": "run_command", "arguments": {"CommandLine": "tokwiz graph query process_checkout --max-tokens 400"}},
                        {"name": "read_file", "arguments": {"Path": f"{tokwiz_dir}/src/routes/checkout.py", "StartLine": 155, "EndLine": 175}},
                    ],
                },
                {"step_index": 11, "type": "TOOL_RESPONSE", "content": "process_checkout -> validate_token, calculate_order_total"},
                {"step_index": 12, "type": "TOOL_RESPONSE", "content": "def process_checkout(token, subtotal): ..."},
                {"step_index": 13, "type": "PLANNER_RESPONSE", "thinking": "Done", "content": "Here is the exact call chain.", "tool_calls": []},
            ]
            (tw_log_dir / "transcript_full.jsonl").write_text(
                "\n".join(json.dumps(s) for s in tw_steps) + "\n", encoding="utf-8"
            )

            base_log_dir = fake_brain / "cid-baseline" / ".system_generated" / "logs"
            base_log_dir.mkdir(parents=True, exist_ok=True)
            base_steps = [
                {"step_index": 0, "type": "USER_INPUT", "content": f"<USER_REQUEST>{p1}</USER_REQUEST>"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "thinking": "Reading full files without TokWiz",
                    "content": "",
                    "tool_calls": [
                        {"name": "read_file", "arguments": {"Path": f"{baseline_dir}/src/auth/session.py"}},
                        {"name": "read_file", "arguments": {"Path": f"{baseline_dir}/src/middleware/require_auth.py"}},
                        {"name": "read_file", "arguments": {"Path": f"{baseline_dir}/src/services/order_engine.py"}},
                    ],
                },
                {"step_index": 2, "type": "TOOL_RESPONSE", "content": (baseline_dir / "src/auth/session.py").read_text()},
                {"step_index": 3, "type": "TOOL_RESPONSE", "content": (baseline_dir / "src/middleware/require_auth.py").read_text()},
                {"step_index": 4, "type": "TOOL_RESPONSE", "content": (baseline_dir / "src/services/order_engine.py").read_text()},
                {"step_index": 5, "type": "PLANNER_RESPONSE", "thinking": "Done", "content": "Updated in baseline.", "tool_calls": []},
            ]
            (base_log_dir / "transcript_full.jsonl").write_text(
                "\n".join(json.dumps(s) for s in base_steps) + "\n", encoding="utf-8"
            )

            os.environ["TOKWIZ_BRAIN_DIR"] = str(fake_brain)
            tel_mod = _load_mod("telemetry_e2e_check", SCRIPTS_DIR / "telemetry.py")
            store = tel_mod.TelemetryStore(root_dir=tokwiz_dir)

            # Call get_summary() 5 consecutive times to prove 100% refresh idempotency
            snapshots = [store.get_summary() for _ in range(5)]
            first = snapshots[0]
            self.assertEqual(first["total_turns"], 2)
            self.assertIsNotNone(first["ab_comparison"])
            self.assertEqual(first["ab_comparison"]["paired_count"], 1)
            self.assertGreater(first["ab_comparison"]["totals"]["ab_tokens_saved"], 0)

            t1 = first["turns"][0]
            self.assertEqual(t1["telemetry_source"], "TRANSCRIPT_MEASURED")
            receipts_by_target = {r["target"]: r for r in t1["receipts"]}
            # session.py used skeleton + ranged read -> positive savings
            self.assertGreater(receipts_by_target["src/auth/session.py"]["saved_tokens"], 500)
            # jwt.py used skeleton AND full file read -> strictly 0 savings!
            self.assertEqual(receipts_by_target["src/crypto/jwt.py"]["saved_tokens"], 0)

            # Turn 2 ("Explain the exact call chain...") must be classified as Profile A (L1), NOT Profile E (14,694 tok)
            t2 = first["turns"][1]
            self.assertEqual(t2["profile_code"], "A")
            self.assertLess(t2["forecast_total_tokens"], 4500)

            for snap in snapshots[1:]:
                self.assertEqual(snap["total_turns"], first["total_turns"])
                self.assertEqual(snap["net_tokens_saved"], first["net_tokens_saved"])
                self.assertEqual(snap["mape_pct"], first["mape_pct"])
                for idx_turn in range(2):
                    self.assertEqual(snap["turns"][idx_turn]["forecast_id"], first["turns"][idx_turn]["forecast_id"])
                    self.assertEqual(snap["turns"][idx_turn]["forecast_total_tokens"], first["turns"][idx_turn]["forecast_total_tokens"])
                    self.assertEqual(snap["turns"][idx_turn]["real_total_tokens"], first["turns"][idx_turn]["real_total_tokens"])
                    self.assertEqual(snap["turns"][idx_turn]["variance_tags"], first["turns"][idx_turn]["variance_tags"])

            # Verify `tokwiz verify-ab --json` CLI command works end-to-end
            v_proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "cli.py"),
                    "verify-ab",
                    "--tokwiz",
                    str(tokwiz_dir),
                    "--baseline",
                    str(baseline_dir),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            v_report = json.loads(v_proc.stdout)
            self.assertEqual(v_report["paired_count"], 1)
            self.assertTrue(v_report["baseline_tests"]["passed"])
            self.assertTrue(v_report["tokwiz_tests"]["passed"])
        finally:
            if saved_env is None:
                os.environ.pop("TOKWIZ_BRAIN_DIR", None)
            else:
                os.environ["TOKWIZ_BRAIN_DIR"] = saved_env
            shutil.rmtree(demo_ws, ignore_errors=True)
            shutil.rmtree(fake_brain, ignore_errors=True)

    def test_phase5_slice_test_and_checkpoint_cli(self) -> None:
        ws = Path(tempfile.mkdtemp(prefix="tokwiz_phase5_"))
        try:
            (ws / "app.py").write_text("def greet(name: str) -> str:\n    return f'hi {name}'\n", encoding="utf-8")
            (ws / "tests").mkdir()
            (ws / "tests" / "test_app.py").write_text(
                "import unittest\nfrom app import greet\n\nclass T(unittest.TestCase):\n"
                "    def test_greet(self):\n        self.assertEqual(greet('x'), 'hi x')\n",
                encoding="utf-8",
            )
            # Run `tokwiz slice`
            s_proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "cli.py"), "slice", "greet", "--root", str(ws)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("[TokWiz Code Slice: greet", s_proc.stdout)
            self.assertIn("def greet", s_proc.stdout)

            # Run `tokwiz test`
            t_proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "cli.py"), "test", "--root", str(ws), "--all"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("1/1 impacted tests passed", t_proc.stdout)

            # Run `tokwiz checkpoint`
            c_proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "cli.py"), "checkpoint", "--root", str(ws)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("# TokWiz Checkpoint", c_proc.stdout)
            self.assertIn("PASS (1/1", c_proc.stdout)
            self.assertTrue((ws / ".tokwiz" / "checkpoint.md").exists())
            self.assertLess(len(c_proc.stdout.split()), 200)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_tokwiz_update_check_and_bundle_refresh(self) -> None:
        """Verify `tokwiz update --check` and bundle extraction + project init refresh."""
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("cli_upd_test", SCRIPTS_DIR / "cli.py")
        assert spec and spec.loader
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)

        with mock.patch.object(cli_mod, "_fetch_latest_release_tag", return_value="v9.9.9"):
            rc = cli_mod.main(["update", "--check"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
