"""Tier 4 Automated A/B Benchmark Suite Tests."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "evaluations"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bench_mod = _load_module("run_benchmark", EVAL_DIR / "run-benchmark.py")


class TestABBenchmarks(unittest.TestCase):
    def test_ab_benchmark_suite_passes_all_scenarios(self) -> None:
        report = bench_mod.run_ab_benchmarks()
        self.assertTrue(report["all_passed"])
        self.assertLess(int(report["skill_md_tokens"]), 350)
        self.assertGreaterEqual(float(report["overall_net_reduction_pct"]), 50.0)
        self.assertLessEqual(float(report["forecast_vs_real_converged_mape_pct"]), 20.0)
        self.assertEqual(len(report["scenarios"]), 5)
        for sc in report["scenarios"]:
            self.assertTrue(sc["passed"], f"Scenario failed: {sc['name']}")


if __name__ == "__main__":
    unittest.main()
