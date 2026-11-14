"""Tier 1 unit tests for Token Estimator, Complexity Classifier, and Pricing Matrix."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


est_mod = _load_module("token_estimator", "token-estimator.py")


class TestTokenEstimator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "jwt.ts").write_text(
            "export function verifySignature(token: string): boolean {\n  return token.length > 5;\n}\n",
            encoding="utf-8",
        )
        (self.root / "src" / "session.ts").write_text(
            'import { verifySignature } from "./jwt";\n'
            "export function validateToken(token: string): boolean {\n"
            "  return verifySignature(token);\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "src" / "middleware.ts").write_text(
            'import { validateToken } from "./session";\n'
            "export function requireAuth(req: string): boolean {\n"
            "  return validateToken(req);\n"
            "}\n",
            encoding="utf-8",
        )
        self.estimator = est_mod.TokenEstimator(root_dir=self.root)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_complexity_profiles_a_through_e(self) -> None:
        # Profile A: Direct Q&A / Explanation
        res_a = self.estimator.estimate("What is the purpose of JWT expiration?")
        self.assertEqual(res_a.profile_code, "A")
        self.assertEqual(res_a.optimization_level, 1)

        # Profile B: Targeted Single-File Edit
        res_b = self.estimator.estimate("Edit verifySignature to check prefix")
        self.assertIn(res_b.profile_code, ("B", "C"))

        # Profile C: Multi-File Feature / Refactor
        res_c = self.estimator.estimate(
            "Refactor validateToken to support API key headers and update callers"
        )
        self.assertEqual(res_c.profile_code, "C")
        self.assertGreaterEqual(res_c.optimization_level, 2)
        self.assertGreaterEqual(res_c.blast_radius_files, 3)

        # Profile D: Deep Debugging / Test Triage
        res_d = self.estimator.estimate(
            "Fix failing pytest test traceback and debug authentication error"
        )
        self.assertEqual(res_d.profile_code, "D")

        # Profile E: Repo-Wide Exploration / Architecture
        res_e = self.estimator.estimate(
            "Audit the entire codebase architecture and migrate all repository modules"
        )
        self.assertEqual(res_e.profile_code, "E")
        self.assertEqual(res_e.optimization_level, 3)

    def test_pricing_matrix_and_savings_calculation(self) -> None:
        res = self.estimator.estimate(
            "Refactor validateToken to support API key headers and update tests"
        )
        self.assertTrue(res.forecast_id.startswith("tw_"))
        self.assertGreater(res.unoptimized_tokens, res.total_tokens)
        self.assertGreater(res.savings_pct, 30.0)

        # Verify Claude, Codex, and Gemini models are priced with cache discounts
        for model_id in ("claude-sonnet-4", "o4-mini", "gemini-2.5-pro"):
            self.assertIn(model_id, res.costs)
            mc = res.costs[model_id]
            self.assertGreater(mc.uncached_cost_usd, 0.0)
            self.assertLess(mc.cached_cost_usd, mc.uncached_cost_usd)
            self.assertGreater(mc.unoptimized_cost_usd, mc.uncached_cost_usd)

        # Verify forecast banner formatting
        self.assertIn("⚡ TokWiz Forecast", res.banner)
        self.assertIn("Claude Sonnet:", res.banner)
        self.assertIn("Codex (`o4-mini`):", res.banner)
        self.assertIn("Gemini Pro:", res.banner)

    def test_calibration_multiplier_integration(self) -> None:
        cal_dir = self.root / ".tokwiz"
        cal_dir.mkdir(parents=True, exist_ok=True)
        (cal_dir / "calibration.json").write_text(
            json.dumps({"profile_step_multipliers": {"C": 1.5}}),
            encoding="utf-8",
        )
        calibrated_est = est_mod.TokenEstimator(root_dir=self.root)
        res = calibrated_est.estimate("Refactor validateToken across callers")
        self.assertEqual(res.profile_code, "C")
        self.assertEqual(res.estimated_steps, 9)  # 6 * 1.5 = 9

    def test_cli_entrypoints(self) -> None:
        cli_path = SCRIPTS_DIR / "cli.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(cli_path),
                "predict",
                "Refactor validateToken",
                "--root",
                str(self.root),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        self.assertEqual(data["profile_code"], "C")
        self.assertIn("validateToken", data["matched_symbols"])


if __name__ == "__main__":
    unittest.main()
