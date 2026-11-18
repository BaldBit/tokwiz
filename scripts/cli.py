#!/usr/bin/env python3
"""Unified CLI entrypoint for TokWiz (`tokwiz`)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
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


def _register_mcp_server(config_path: Path, mcp_cmd: List[str]) -> None:
    """Register `tokwiz` stdio MCP server without clobbering existing MCP servers."""
    data: Dict[str, object] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    servers = data.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers["tokwiz"] = {
            "command": mcp_cmd[0],
            "args": mcp_cmd[1:],
        }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _parse_agents(agent_arg: str) -> Set[str]:
    supported = {"claude", "codex", "gemini", "cursor"}
    tokens = {t.strip().lower() for t in agent_arg.split(",") if t.strip()}
    if "all" in tokens:
        return supported
    return tokens & supported


def _workspaces_registry_path() -> Path:
    env_p = os.environ.get("TOKWIZ_WORKSPACES_FILE")
    if env_p:
        return Path(env_p)
    return Path.home() / ".tokwiz" / "workspaces.json"


def _is_temp_test_path(p: Path) -> bool:
    if os.environ.get("TOKWIZ_WORKSPACES_FILE"):
        return False
    s = str(p)
    return s.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/"))


def _register_workspace_in_registry(ws_path: Path) -> None:
    if _is_temp_test_path(ws_path):
        return
    reg_file = _workspaces_registry_path()
    try:
        reg_file.parent.mkdir(parents=True, exist_ok=True)
        items: List[Dict[str, object]] = []
        if reg_file.exists():
            raw = json.loads(reg_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = [x for x in raw if isinstance(x, dict) and str(x.get("path")) != str(ws_path)]
        items.insert(0, {"name": ws_path.name, "path": str(ws_path), "updated_at": time.time()})
        reg_file.write_text(json.dumps(items[:30], indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _unregister_workspace_from_registry(ws_path: Path) -> None:
    reg_file = _workspaces_registry_path()
    if not reg_file.exists():
        return
    try:
        raw = json.loads(reg_file.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            kept = [x for x in raw if isinstance(x, dict) and str(x.get("path")) != str(ws_path)]
            reg_file.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


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
    parser.add_argument("--portable", action="store_true", help="Emit git-committable portable `tokwiz` commands without absolute paths")
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
    if args.portable:
        pre_prompt_cmd = "tokwiz hook pre-prompt-predict.py"
        pre_tool_cmd = "tokwiz hook pre-tool-guard.py"
        post_turn_cmd = "tokwiz hook post-turn-record.py"
        mcp_base_cmd = ["tokwiz", "mcp", "--root", "."]
    else:
        pre_prompt_cmd = f"python3 {_REPO_ROOT / 'hooks' / 'pre-prompt-predict.py'}"
        pre_tool_cmd = f"python3 {_REPO_ROOT / 'hooks' / 'pre-tool-guard.py'}"
        post_turn_cmd = f"python3 {_REPO_ROOT / 'hooks' / 'post-turn-record.py'}"
        mcp_base_cmd = [sys.executable, str(_REPO_ROOT / "scripts" / "mcp-server.py"), "--root", str(base_dir)]

    rule_line = (
        "If `.tokwiz/` or `.agents/skills/tokwiz/SKILL.md` exists, follow `.agents/skills/tokwiz/SKILL.md` "
        "for token optimization: query `tokwiz graph` before grep/find, skeletonize files >150 lines, and never read lockfiles."
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
                    "matcher": "Read|Grep|Glob|read_file|view_file|Bash|run_command",
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
            "TokWiz Protocol (if `mcp__tokwiz__*` or `.tokwiz/` is available; do NOT `cat SKILL.md`): "
            "1) Call `mcp__tokwiz__tokwiz_slice` (supports `symbols: [...]` batching) / `tokwiz_trace` / `tokwiz_graph_query` BEFORE `grep`/`find`/`Read`; "
            "2) Call `mcp__tokwiz__tokwiz_skeleton` before ranged reads on >150L files; "
            "3) Call `mcp__tokwiz__tokwiz_test` instead of full `unittest`/`pytest`; "
            "4) Skip lockfiles/`node_modules/`/`dist/`.",
        )
        _register_mcp_server(base_dir / ".mcp.json", mcp_base_cmd)
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
        _register_mcp_server(base_dir / ".agents" / "mcp.json", mcp_base_cmd)
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
        _register_mcp_server(base_dir / ".cursor" / "mcp.json", mcp_base_cmd)
        configured.append("cursor")

    if not args.is_global:
        _register_workspace_in_registry(target_root)

    # Record initialization timestamp so transcript sync only tracks post-init conversations
    cfg_dir = base_dir / ".tokwiz"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    cfg: Dict[str, object] = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault("installed_at", time.time())
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    print(
        f"[TokWiz Init] Configured agents ({', '.join(configured)}) in {base_dir} "
        f"| Graph: {graph_stats.get('total_nodes', 0)} nodes, {graph_stats.get('total_edges', 0)} edges"
    )
    return 0


def _remove_tokwiz_hooks_from_file(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False

    changed = False
    markers = ("pre-prompt-predict.py", "pre-tool-guard.py", "post-turn-record.py")
    for event_name in list(hooks.keys()):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not any(m in json.dumps(e) for m in markers)]
        if len(kept) != len(entries):
            changed = True
        if kept:
            hooks[event_name] = kept
        else:
            hooks.pop(event_name, None)

    if not changed:
        return False
    if not hooks:
        data.pop("hooks", None)
    if not data:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _unregister_mcp_server(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "tokwiz" not in servers:
        return False
    servers.pop("tokwiz", None)
    if not servers:
        data.pop("mcpServers", None)
    if not data:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _remove_tokwiz_lines(path: Path, needles: Tuple[str, ...]) -> bool:
    if not path.exists():
        return False
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [ln for ln in raw_lines if not any(n in ln for n in needles)]
    if len(kept) == len(raw_lines):
        return False
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        path.unlink(missing_ok=True)
    else:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True


def _cleanup_empty_dirs(dirs: List[Path]) -> None:
    for d in dirs:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass


def _cmd_uninstall(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz uninstall",
        description="Safely remove TokWiz hooks, MCP entries, skills, and data without harming project files.",
    )
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--global", dest="is_global", action="store_true", help="Remove global home-directory integrations")
    parser.add_argument("--home", default=None, help="Optional home directory override (used with --global)")
    parser.add_argument("--keep-data", action="store_true", help="Keep .tokwiz/ telemetry and graph database")
    parser.add_argument("--purge-runtime", action="store_true", help="Also remove global ~/.tokwiz/runtime and ~/.local/bin/tokwiz")
    args = parser.parse_args(argv)

    target_root = _resolve_root(args.root)
    base_dir = _resolve_root(args.home) if (args.is_global and args.home) else (
        Path.home() if args.is_global else target_root
    )

    removed: List[str] = []

    for skill_dir in (base_dir / ".claude" / "skills" / "tokwiz", base_dir / ".agents" / "skills" / "tokwiz"):
        if skill_dir.exists():
            shutil.rmtree(skill_dir, ignore_errors=True)
            removed.append(str(skill_dir.relative_to(base_dir)))

    gem_rule = base_dir / ".agents" / "rules" / "tokwiz.md"
    if gem_rule.exists():
        gem_rule.unlink(missing_ok=True)
        removed.append(str(gem_rule.relative_to(base_dir)))

    for hook_file in (base_dir / ".claude" / "settings.json", base_dir / ".agents" / "hooks.json"):
        if _remove_tokwiz_hooks_from_file(hook_file):
            removed.append(f"hooks:{hook_file.relative_to(base_dir)}")

    for mcp_file in (base_dir / ".mcp.json", base_dir / ".agents" / "mcp.json", base_dir / ".cursor" / "mcp.json"):
        if _unregister_mcp_server(mcp_file):
            removed.append(f"mcp:{mcp_file.relative_to(base_dir)}")

    rule_needles = ("TokWiz Protocol (", ".agents/skills/tokwiz/SKILL.md")
    for md_file in (
        base_dir / "CLAUDE.md",
        base_dir / "GEMINI.md",
        base_dir / "AGENTS.md",
        base_dir / ".cursorrules",
        base_dir / ".github" / "copilot-instructions.md",
    ):
        if _remove_tokwiz_lines(md_file, rule_needles):
            removed.append(f"rules:{md_file.relative_to(base_dir)}")

    if not args.is_global:
        if _remove_tokwiz_lines(target_root / ".gitignore", (".tokwiz/",)):
            removed.append(".gitignore:.tokwiz/")
        tokwiz_dir = target_root / ".tokwiz"
        if not args.keep_data and tokwiz_dir.exists():
            shutil.rmtree(tokwiz_dir, ignore_errors=True)
            removed.append(".tokwiz/")
        _unregister_workspace_from_registry(target_root)

    _cleanup_empty_dirs([
        base_dir / ".claude" / "skills",
        base_dir / ".claude",
        base_dir / ".agents" / "skills",
        base_dir / ".agents" / "rules",
        base_dir / ".agents",
        base_dir / ".cursor",
        base_dir / ".github",
    ])

    if args.purge_runtime:
        runtime_root = Path(os.environ.get("TOKWIZ_HOME") or (Path.home() / ".tokwiz" / "runtime")) / "tokwiz"
        bin_path = Path(os.environ.get("TOKWIZ_BIN_DIR") or (Path.home() / ".local" / "bin")) / "tokwiz"
        if runtime_root.exists():
            shutil.rmtree(runtime_root, ignore_errors=True)
            removed.append(str(runtime_root))
        if bin_path.exists():
            bin_path.unlink(missing_ok=True)
            removed.append(str(bin_path))

    if removed:
        print(f"[TokWiz Uninstall] Cleanly removed from {base_dir}: {', '.join(removed)}")
    else:
        print(f"[TokWiz Uninstall] No TokWiz integrations found in {base_dir}.")
    return 0


def _cmd_hook(argv: List[str]) -> int:
    if not argv:
        print(json.dumps({"decision": "allow"}))
        return 0
    hook_arg = argv[0].strip()
    mapping = {
        "pre-prompt": "pre-prompt-predict.py",
        "pre-prompt-predict": "pre-prompt-predict.py",
        "pre-prompt-predict.py": "pre-prompt-predict.py",
        "pre-tool": "pre-tool-guard.py",
        "pre-tool-guard": "pre-tool-guard.py",
        "pre-tool-guard.py": "pre-tool-guard.py",
        "post-turn": "post-turn-record.py",
        "post-turn-record": "post-turn-record.py",
        "post-turn-record.py": "post-turn-record.py",
    }
    script_name = mapping.get(hook_arg)
    if not script_name:
        print(json.dumps({"decision": "allow"}))
        return 0
    hook_path = _REPO_ROOT / "hooks" / script_name
    spec = importlib.util.spec_from_file_location("tokwiz_hook_exec", hook_path)
    if not spec or not spec.loader:
        print(json.dumps({"decision": "allow"}))
        return 0
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tokwiz_hook_exec"] = mod
    spec.loader.exec_module(mod)
    return int(mod.main())


def _current_version() -> str:
    try:
        init_py = _REPO_ROOT / "scripts" / "__init__.py"
        if init_py.exists():
            m = re.search(r'__version__\s*=\s*"([^"]+)"', init_py.read_text(encoding="utf-8"))
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0.0.0"


def _fetch_latest_release_tag() -> str:
    from urllib.request import Request, urlopen

    req = Request(
        "https://api.github.com/repos/BaldBit/tokwiz/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "tokwiz-cli"},
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("tag_name") or "").strip()


def _cmd_update(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz update",
        description="Update TokWiz runtime from GitHub Releases (or git) and refresh project hooks.",
    )
    parser.add_argument("--check", action="store_true", help="Check latest release version without modifying files")
    parser.add_argument("--version", default="latest", help="Target release tag (e.g. v1.4.1) or 'latest'")
    parser.add_argument("--root", default=".", help="Optional project root to refresh via `tokwiz init`")
    parser.add_argument("--init", action="store_true", help="Force re-running `tokwiz init --agent all` after update")
    args = parser.parse_args(argv)

    cur_ver = _current_version()
    if args.check:
        try:
            latest = _fetch_latest_release_tag()
        except Exception as exc:
            print(f"[TokWiz Update] Failed to check latest release: {exc}", file=sys.stderr)
            return 1
        cur_tag = f"v{cur_ver}" if not cur_ver.startswith("v") else cur_ver
        if latest and latest != cur_tag:
            print(f"[TokWiz Update] Update available: {cur_tag} -> {latest} (run `tokwiz update`)")
        else:
            print(f"[TokWiz Update] Already up to date ({cur_tag}).")
        return 0

    if (_REPO_ROOT / ".git").exists():
        print(f"[TokWiz Update] Source install detected at {_REPO_ROOT}; pulling latest...")
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "pull", "--ff-only"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"[TokWiz Update] git pull failed:\n{proc.stderr.strip()}", file=sys.stderr)
            return proc.returncode
        if proc.stdout.strip():
            print(proc.stdout.strip())
    else:
        import tarfile
        import tempfile
        from urllib.request import Request, urlopen

        ver = args.version
        if ver == "latest":
            bundle_url = "https://github.com/BaldBit/tokwiz/releases/latest/download/tokwiz-bundle.tar.gz"
        else:
            tag = ver if ver.startswith("v") else f"v{ver}"
            bundle_url = f"https://github.com/BaldBit/tokwiz/releases/download/{tag}/tokwiz-bundle.tar.gz"

        print(f"[TokWiz Update] Downloading {bundle_url}...")
        with tempfile.TemporaryDirectory(prefix="tokwiz_upd_") as tmp_dir:
            tar_path = Path(tmp_dir) / "tokwiz-bundle.tar.gz"
            req = Request(bundle_url, headers={"User-Agent": "tokwiz-cli"})
            with urlopen(req, timeout=30) as resp:
                tar_path.write_bytes(resp.read())
            stage_dir = Path(tmp_dir) / "stage"
            stage_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(str(tar_path), "r:gz") as tf:
                for member in tf.getmembers():
                    if os.path.isabs(member.name) or ".." in Path(member.name).parts:
                        raise ValueError(f"Unsafe archive entry: {member.name}")
                tf.extractall(str(stage_dir))
            extracted = stage_dir / "tokwiz"
            if not extracted.is_dir():
                extracted = stage_dir
            shutil.copytree(str(extracted), str(_REPO_ROOT), dirs_exist_ok=True)
        print(f"[TokWiz Update] Updated runtime to {_current_version()} at {_REPO_ROOT}")

    target_root = _resolve_root(args.root)
    if args.init or (target_root / ".tokwiz").exists():
        return _cmd_init(["--agent", "all", "--root", str(target_root)])
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


def _cmd_verify_ab(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz verify-ab",
        description="Verify head-to-head A/B token savings and test equivalence from real IDE session transcripts.",
    )
    parser.add_argument("--tokwiz", default=None, help="Path to TokWiz workspace (default: current dir or ../tokwiz-ab-demo/project-tokwiz)")
    parser.add_argument("--baseline", default=None, help="Path to Baseline workspace (default: sibling project-baseline)")
    parser.add_argument("--no-tests", action="store_true", help="Skip running unittest verification in both workspaces")
    parser.add_argument("--json", action="store_true", help="Output full JSON verification report")
    args = parser.parse_args(argv)

    cwd = _ensure_valid_cwd()
    if args.tokwiz:
        t_root = _resolve_root(args.tokwiz)
    elif (cwd / ".tokwiz").exists() and cwd.name == "project-tokwiz":
        t_root = cwd
    elif (_REPO_ROOT.parent / "tokwiz-ab-demo" / "project-tokwiz").exists():
        t_root = (_REPO_ROOT.parent / "tokwiz-ab-demo" / "project-tokwiz").resolve()
    else:
        t_root = cwd

    if args.baseline:
        b_root = _resolve_root(args.baseline)
    else:
        b_root = (t_root.parent / "project-baseline").resolve()

    spec = importlib.util.spec_from_file_location(
        "post_turn_record", _REPO_ROOT / "hooks" / "post-turn-record.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["post_turn_record"] = mod
    spec.loader.exec_module(mod)

    mod.sync_workspace_transcript_turns(t_root)
    report = mod.build_ab_comparison_report(
        baseline_root=b_root,
        tokwiz_root=t_root,
        run_tests=not args.no_tests,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(mod.render_ab_comparison_cli(report))
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
            "  optimize <prompt>       Pin prompt with exact AST file:Lstart-Lend symbol coordinates\n"
            "  graph <build|query|map> Build or query the Tier 2 Tree-Sitter PageRank graph\n"
            "  map [--max-tokens 220]  Generate <220-token zero-LLM codebase architecture map\n"
            "  slice <symbol>          One-shot AST definition + caller snippet bundler\n"
            "  skeleton <file>         Extract compact AST/data skeleton with line numbers\n"
            "  test [files...]         Run graph-impacted tests with 1-line pass receipt\n"
            "  checkpoint              Generate <200-token working memory snapshot\n"
            "  resume                  Reload live <200-token working memory checkpoint after /clear\n"
            "  mcp [--root DIR]        Start native stdio MCP server for IDE agents\n"
            "  filter [-- <cmd>]       Compress noisy test, build, and git shell outputs\n"
            "  stats [--session|--days N]  Display token savings & Forecast-vs-Real cost report\n"
            "  verify-ab [--json]      Verify head-to-head A/B savings from real IDE transcripts\n"
            "  live-ab [--agent ...]   Run automated headless A/B tests via claude/codex/gemini CLI\n"
            "  dashboard [--port 8420] Launch interactive local analytics web dashboard\n"
            "  benchmark [--json]      Run paired A/B token savings & MAPE benchmark suite\n"
            "  level <1|2|3|auto>      Configure optimization level\n"
            "  budget --max-prompt-cost <usd>  Configure prompt cost threshold\n"
            "  update [--check]        Update TokWiz runtime and refresh project hooks\n"
            "  uninstall [--keep-data] Safely remove TokWiz from a project without harming user files"
        )
        return 0 if args_list else 1

    cmd, rest = args_list[0], args_list[1:]
    if cmd == "init":
        return _cmd_init(rest)
    if cmd == "predict":
        mod = _load_script_module("token_estimator", "token-estimator.py")
        return int(mod.main(rest))
    if cmd == "optimize":
        mod = _load_script_module("workspace_ops", "workspace-ops.py")
        return int(mod.main_optimize(rest))
    if cmd == "hook":
        return _cmd_hook(rest)
    if cmd == "graph":
        mod = _load_script_module("project_graph", "project-graph.py")
        return int(mod.main(rest))
    if cmd == "map":
        mod = _load_script_module("project_graph", "project-graph.py")
        return int(mod.main(["map", *rest]))
    if cmd == "slice":
        mod = _load_script_module("project_graph", "project-graph.py")
        return int(mod.main(["slice", *rest]))
    if cmd == "skeleton":
        mod = _load_script_module("code_skeletonizer", "code-skeletonizer.py")
        return int(mod.main(rest))
    if cmd == "test":
        mod = _load_script_module("workspace_ops", "workspace-ops.py")
        return int(mod.main_test(rest))
    if cmd == "checkpoint":
        mod = _load_script_module("workspace_ops", "workspace-ops.py")
        return int(mod.main_checkpoint(rest))
    if cmd == "resume":
        mod = _load_script_module("workspace_ops", "workspace-ops.py")
        return int(mod.main_resume(rest))
    if cmd == "mcp":
        mod = _load_script_module("mcp_server", "mcp-server.py")
        return int(mod.main(rest))
    if cmd == "filter":
        mod = _load_script_module("output_filter", "output-filter.py")
        return int(mod.main(rest))
    if cmd == "stats":
        mod = _load_script_module("telemetry", "telemetry.py")
        return int(mod.main(rest))
    if cmd == "verify-ab":
        return _cmd_verify_ab(rest)
    if cmd == "live-ab":
        spec = importlib.util.spec_from_file_location(
            "run_live_ab", _REPO_ROOT / "evaluations" / "run-live-ab.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["run_live_ab"] = mod
        spec.loader.exec_module(mod)
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
    if cmd == "update":
        return _cmd_update(rest)
    if cmd == "uninstall":
        return _cmd_uninstall(rest)

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
