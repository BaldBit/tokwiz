#!/usr/bin/env python3
"""Historical Transcript Replay & Forecast Calibration Runner (`evaluations/replay-transcripts.py`).

Replays recorded or synthetic multi-turn agent transcripts (`.jsonl`), running
TokWiz's pre-prompt forecaster before each turn and reconciling against actual
billed usage after each turn to verify EMA self-calibration convergence (MAPE <= 20%).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

_EVAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_est_mod = _load_script_module("token_estimator", "token-estimator.py")
_cal_mod = _load_script_module("calibration", "calibration.py")


def replay_turns(
    workspace_root: Path,
    turns: List[Dict[str, object]],
    ema_alpha: float = 0.40,
) -> Dict[str, object]:
    """Replay a sequence of turns, updating calibration weights after each turn."""
    engine = _cal_mod.CalibrationEngine(root_dir=workspace_root, ema_alpha=ema_alpha)
    records: List[Dict[str, object]] = []

    for turn in turns:
        # Reload estimator on each turn so updated calibration.json weights take effect
        estimator = _est_mod.TokenEstimator(root_dir=workspace_root)
        prompt = str(turn.get("prompt", "Refactor validateToken"))
        forecast_res = estimator.estimate(prompt=prompt)
        forecast_dict = asdict(forecast_res)

        rec = engine.reconcile_turn(
            forecast=forecast_dict,
            usage_payload=turn,
        )
        records.append(rec)

    initial_ape = float(records[0]["ape_pct"]) if records else 0.0
    tail_slice = records[-min(3, len(records)) :] if records else []
    converged_mape = (
        sum(float(r["ape_pct"]) for r in tail_slice) / max(1, len(tail_slice))
    )

    return {
        "turns_replayed": len(records),
        "initial_ape_pct": round(initial_ape, 2),
        "converged_mape_pct": round(converged_mape, 2),
        "records": records,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay-transcripts",
        description="Replay historical .jsonl turns to evaluate Forecast vs. Real Cost MAPE convergence.",
    )
    parser.add_argument("transcript_jsonl", help="Path to JSONL file of turns")
    parser.add_argument("--root", default=".", help="Workspace root directory")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args(argv)

    path = Path(args.transcript_jsonl)
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    turns = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    res = replay_turns(Path(args.root).resolve(), turns)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(
            f"[TokWiz Replay] Turns: {res['turns_replayed']} | "
            f"Initial APE: {res['initial_ape_pct']:.1f}% -> "
            f"Converged MAPE: {res['converged_mape_pct']:.1f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
