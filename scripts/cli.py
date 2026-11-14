#!/usr/bin/env python3
"""Unified CLI entrypoint for TokWiz (`tokwiz`)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent


def _ensure_valid_cwd() -> Path:
    """Recover if the user's shell CWD inode was recreated (e.g. after a folder reset)."""
    try:
        return Path(os.getcwd()).resolve()
    except FileNotFoundError:
        pwd_env = os.environ.get("PWD")
        if pwd_env and os.path.isdir(pwd_env):
            os.chdir(pwd_env)
            return Path(pwd_env).resolve()
        os.chdir(str(_REPO_ROOT))
        return _REPO_ROOT


def _resolve_root(root_arg: str) -> Path:
    cwd = _ensure_valid_cwd()
    if not root_arg or root_arg == ".":
        return cwd
    p = Path(root_arg)
    return p.resolve() if p.is_absolute() else (cwd / p).resolve()


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ensure_line_in_file(file_path: Path, line_to_ensure: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path.exists():
        content = file_path.read_text(encoding="utf-8", errors="replace")
        if line_to_ensure in content:
            return
        sep = "" if content.endswith("\n") or not content else "\n"
        file_path.write_text(f"{content}{sep}{line_to_ensure}\n", encoding="utf-8")
    else:
        file_path.write_text(f"{line_to_ensure}\n", encoding="utf-8")


def _merge_hook_list(existing_list: object, new_entry: Dict[str, object], script_marker: str) -> List[Dict[str, object]]:
    """Non-destructively append or update a TokWiz hook entry while preserving existing user hooks."""
    preserved: List[Dict[str, object]] = []
    if isinstance(existing_list, list):
        for item in existing_list:
            if isinstance(item, dict) and script_marker not in json.dumps(item):
                preserved.append(item)
    preserved.append(new_entry)
    return preserved


def _parse_agents(agent_arg: str) -> Set[str]:
    supported = {"claude", "codex", "gemini", "cursor"}
    tokens = {t.strip().lower() for t in agent_arg.split(",") if t.strip()}
    if "all" in tokens:
        return supported
    return tokens & supported


def _cmd_init(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz init",
        description="Initialize TokWiz graph, skills, and lifecycle hooks for AI agents.",
    )
    parser.add_argument(
        "--agent",
        default="all",
        help="Target agents: all or comma-separated (claude,codex,gemini,cursor)",
    )
    parser.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help="Install into user home directory (~/.claude, ~/.agents)",
    )
    parser.add_argument("--root", default=".", help="Target repository root directory")
    parser.add_argument("--home", default=None, help="Override home directory for --global")
    args = parser.parse_args(argv)

    agents = _parse_agents(args.agent)
    if not agents:
        print(f"Error: No valid agents specified in '{args.agent}'", file=sys.stderr)
        return 1

    target_root = _resolve_root(args.root)
    base_dir = _resolve_root(args.home) if (args.is_global and args.home) else (
        Path.home() if args.is_global else target_root
    )

    # 1. Build initial Project Graph & ensure .tokwiz/ in .gitignore (when in a repo)
    if not args.is_global:
        graph_mod = _load_script_module("project_graph", "project-graph.py")
        graph = graph_mod.ProjectGraph(root_dir=target_root)
        graph_stats = graph.build(force=False)
        _ensure_line_in_file(target_root / ".gitignore", ".tokwiz/")
    else:
        graph_stats = {"total_nodes": 0, "total_edges": 0}

    skill_src = _REPO_ROOT / "SKILL.md"
    pre_prompt_cmd = f"python3 {_REPO_ROOT / 'hooks' / 'pre-prompt-predict.py'}"
    pre_tool_cmd = f"python3 {_REPO_ROOT / 'hooks' / 'pre-tool-guard.py'}"
    post_turn_cmd = f"python3 {_REPO_ROOT / 'hooks' / 'post-turn-record.py'}"

    rule_line = (
        "Follow `.agents/skills/tokwiz/SKILL.md` for token optimization: query `tokwiz graph` "
        "before grep/find, skeletonize files >300 lines, and never read lockfiles."
    )

    configured: List[str] = []

    # 2. Claude Code setup
    if "claude" in agents:
        claude_skill = base_dir / ".claude" / "skills" / "tokwiz" / "SKILL.md"
        claude_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, claude_skill)

        settings_path = base_dir / ".claude" / "settings.json"
        settings: Dict[str, object] = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                settings = {}
        hooks_cfg = settings.setdefault("hooks", {})
        if isinstance(hooks_cfg, dict):
            hooks_cfg["UserPromptSubmit"] = _merge_hook_list(
                hooks_cfg.get("UserPromptSubmit"),
                {"matcher": ".*", "hooks": [{"type": "command", "command": pre_prompt_cmd}]},
                "pre-prompt-predict.py",
            )
            hooks_cfg["PreToolUse"] = _merge_hook_list(
                hooks_cfg.get("PreToolUse"),
                {
                    "matcher": "Read|read_file|view_file|Bash|run_command",
                    "hooks": [{"type": "command", "command": pre_tool_cmd}],
                },
                "pre-tool-guard.py",
            )
            hooks_cfg["Stop"] = _merge_hook_list(
                hooks_cfg.get("Stop"),
                {"matcher": ".*", "hooks": [{"type": "command", "command": post_turn_cmd}]},
                "post-turn-record.py",
            )
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        _ensure_line_in_file(
            base_dir / "CLAUDE.md",
            "Follow `.claude/skills/tokwiz/SKILL.md` for token optimization, graph lookups, and ranged reads.",
        )
        configured.append("claude")

    # 3. Gemini CLI / Antigravity setup
    if "gemini" in agents:
        gem_skill = base_dir / ".agents" / "skills" / "tokwiz" / "SKILL.md"
        gem_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, gem_skill)

        gem_rule = base_dir / ".agents" / "rules" / "tokwiz.md"
        gem_rule.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO_ROOT / "adapters" / "gemini" / "GEMINI.md", gem_rule)

        hooks_json_path = base_dir / ".agents" / "hooks.json"
        hooks_payload: Dict[str, object] = {}
        if hooks_json_path.exists():
            try:
                hooks_payload = json.loads(hooks_json_path.read_text(encoding="utf-8"))
            except Exception:
                hooks_payload = {}
        gem_hooks = hooks_payload.setdefault("hooks", {})
        if isinstance(gem_hooks, dict):
            gem_hooks["PreInvocation"] = _merge_hook_list(
                gem_hooks.get("PreInvocation"),
                {"matcher": ".*", "command": pre_prompt_cmd, "timeout": 5},
                "pre-prompt-predict.py",
            )
            gem_hooks["PreToolUse"] = _merge_hook_list(
                gem_hooks.get("PreToolUse"),
                {
                    "matcher": "read_file|view_file|Read|run_command|Bash",
                    "command": pre_tool_cmd,
                    "timeout": 5,
                },
                "pre-tool-guard.py",
            )
            gem_hooks["Stop"] = _merge_hook_list(
                gem_hooks.get("Stop"),
                {"matcher": ".*", "command": post_turn_cmd, "timeout": 5},
                "post-turn-record.py",
            )
        hooks_json_path.write_text(json.dumps(hooks_payload, indent=2) + "\n", encoding="utf-8")
        _ensure_line_in_file(base_dir / "GEMINI.md", rule_line)
        configured.append("gemini")

    # 4. OpenAI Codex CLI setup
    if "codex" in agents:
        codex_skill = base_dir / ".agents" / "skills" / "tokwiz" / "SKILL.md"
        codex_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, codex_skill)
        _ensure_line_in_file(base_dir / "AGENTS.md", rule_line)
        configured.append("codex")

    # 5. Cursor / GitHub Copilot setup
    if "cursor" in agents:
        cursor_skill = base_dir / ".agents" / "skills" / "tokwiz" / "SKILL.md"
        cursor_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, cursor_skill)
        _ensure_line_in_file(base_dir / ".cursorrules", rule_line)
        _ensure_line_in_file(base_dir / ".github" / "copilot-instructions.md", rule_line)
        configured.append("cursor")

    print(
        f"[TokWiz Init] Configured agents ({', '.join(configured)}) in {base_dir} "
        f"| Graph: {graph_stats.get('total_nodes', 0)} nodes, {graph_stats.get('total_edges', 0)} edges"
    )
    return 0


def _cmd_level(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz level", description="Set optimization level (1, 2, 3, or auto).")
    parser.add_argument("level", choices=["1", "2", "3", "auto"], help="Target optimization level")
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args(argv)

    cfg_dir = _resolve_root(args.root) / ".tokwiz"
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
    parser.add_argument("--enforce", action="store_true", help="Block prompt execution when threshold is exceeded")
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args(argv)

    cfg_dir = _resolve_root(args.root) / ".tokwiz"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["max_prompt_cost_usd"] = args.max_prompt_cost
    if args.enforce:
        cfg["enforce_budget_gate"] = True
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"[TokWiz] Max prompt cost gate set to: ${args.max_prompt_cost:.2f}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_valid_cwd()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0] in ("-h", "--help"):
        print(
            "usage: tokwiz <command> [options]\n\n"
            "Commands:\n"
            "  init [--agent all|...]  Configure TokWiz skills, hooks, and project graph\n"
            "  predict <prompt>        Forecast workflow tokens, blast radius, and $ cost\n"
            "  graph <build|query>     Build or query the Tier 2 Tree-Sitter PageRank graph\n"
            "  skeleton <file>         Extract compact AST signature skeleton with line numbers\n"
            "  filter [-- <cmd>]       Compress noisy test, build, and git shell outputs\n"
            "  stats [--session|--days N]  Display token savings & Forecast-vs-Real cost report\n"
            "  dashboard [--port 8420] Launch interactive local analytics web dashboard\n"
            "  benchmark [--json]      Run paired A/B token savings & MAPE benchmark suite\n"
            "  level <1|2|3|auto>      Configure optimization level\n"
            "  budget --max-prompt-cost <usd>  Configure prompt cost threshold"
        )
        return 0 if args_list else 1

    cmd, rest = args_list[0], args_list[1:]
    if cmd == "init":
        return _cmd_init(rest)
    if cmd == "predict":
        mod = _load_script_module("token_estimator", "token-estimator.py")
        return int(mod.main(rest))
    if cmd == "graph":
        mod = _load_script_module("project_graph", "project-graph.py")
        return int(mod.main(rest))
    if cmd == "skeleton":
        mod = _load_script_module("code_skeletonizer", "code-skeletonizer.py")
        return int(mod.main(rest))
    if cmd == "filter":
        mod = _load_script_module("output_filter", "output-filter.py")
        return int(mod.main(rest))
    if cmd == "stats":
        mod = _load_script_module("telemetry", "telemetry.py")
        return int(mod.main(rest))
    if cmd == "dashboard":
        spec = importlib.util.spec_from_file_location(
            "dashboard_server", _REPO_ROOT / "dashboard" / "server.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["dashboard_server"] = mod
        spec.loader.exec_module(mod)
        return int(mod.main(rest))
    if cmd == "benchmark":
        spec = importlib.util.spec_from_file_location(
            "run_benchmark", _REPO_ROOT / "evaluations" / "run-benchmark.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["run_benchmark"] = mod
        spec.loader.exec_module(mod)
        return int(mod.main(rest))
    if cmd == "level":
        return _cmd_level(rest)
    if cmd == "budget":
        return _cmd_budget(rest)

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
