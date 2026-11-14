#!/usr/bin/env python3
"""Unified CLI entrypoint for TokWiz (`tokwiz`)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import List, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _cmd_level(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz level", description="Set optimization level (1, 2, 3, or auto).")
    parser.add_argument("level", choices=["1", "2", "3", "auto"], help="Target optimization level")
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args(argv)

    cfg_dir = Path(args.root).resolve() / ".tokwiz"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["optimization_level"] = args.level if args.level == "auto" else int(args.level)
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"[TokWiz] Optimization level set to: {args.level}")
    return 0


def _cmd_budget(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz budget", description="Configure prompt cost budget gate.")
    parser.add_argument("--max-prompt-cost", type=float, required=True, help="Max cost in USD per prompt")
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args(argv)

    cfg_dir = Path(args.root).resolve() / ".tokwiz"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["max_prompt_cost_usd"] = args.max_prompt_cost
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"[TokWiz] Max prompt cost gate set to: ${args.max_prompt_cost:.2f}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0] in ("-h", "--help"):
        print(
            "usage: tokwiz <command> [options]\n\n"
            "Commands:\n"
            "  predict <prompt>        Forecast workflow tokens, blast radius, and $ cost\n"
            "  graph <build|query>     Build or query the Tier 2 Tree-Sitter PageRank graph\n"
            "  skeleton <file>         Extract compact AST signature skeleton with line numbers\n"
            "  level <1|2|3|auto>      Configure optimization level\n"
            "  budget --max-prompt-cost <usd>  Configure prompt cost threshold"
        )
        return 0 if args_list else 1

    cmd, rest = args_list[0], args_list[1:]
    if cmd == "predict":
        mod = _load_script_module("token_estimator", "token-estimator.py")
        return int(mod.main(rest))
    if cmd == "graph":
        mod = _load_script_module("project_graph", "project-graph.py")
        return int(mod.main(rest))
    if cmd == "skeleton":
        mod = _load_script_module("code_skeletonizer", "code-skeletonizer.py")
        return int(mod.main(rest))
    if cmd == "level":
        return _cmd_level(rest)
    if cmd == "budget":
        return _cmd_budget(rest)

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
