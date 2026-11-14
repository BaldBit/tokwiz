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

            # Now simulate Turn 1 finishing — must reconcile forecasts[0], NOT forecasts[2]!
            res1 = post_mod.handle_post_turn({"cwd": str(tmp_ws)})
            self.assertTrue(res1["reconciled"])
            self.assertEqual(res1["forecast_id"], forecasts[0]["forecast_id"])

            # Simulate Turn 2 finishing — must reconcile forecasts[1]!
            res2 = post_mod.handle_post_turn({"cwd": str(tmp_ws)})
            self.assertTrue(res2["reconciled"])
            self.assertEqual(res2["forecast_id"], forecasts[1]["forecast_id"])
        finally:
            shutil.rmtree(tmp_ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
