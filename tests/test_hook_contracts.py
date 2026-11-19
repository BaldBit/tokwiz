"""Tier 2 Hook Contract, Zero-Token Injection, and Fail-Open Resilience Tests."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cli_mod = _load_module("tokwiz_cli", SCRIPTS_DIR / "cli.py")


class TestHookContractsAndResilience(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "auth.py").write_text(
            "def validate_token(tok: str) -> bool:\n    return len(tok) > 8\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _run_hook(self, script_name: str, stdin_text: str) -> Tuple[dict, float]:
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / script_name)],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=True,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return json.loads(proc.stdout), elapsed_ms

    def test_pre_invocation_zero_token_contract_and_latency(self) -> None:
        # Pre-build graph so hook runs against warm SQLite index
        cli_mod.main(["graph", "--root", str(self.root), "build"])

        payload = {
            "hook_event_name": "PreInvocation",
            "prompt": "Refactor validate_token to support API keys",
            "cwd": str(self.root),
        }
        out, elapsed_ms = self._run_hook("pre-prompt-predict.py", json.dumps(payload))

        # Zero-Token Injection check: systemMessage MUST be empty string ""
        self.assertEqual(out.get("systemMessage"), "")
        self.assertEqual(out.get("decision"), "allow")
        meta = out.get("metadata", {})
        self.assertIn("⚡ TokWiz Forecast", meta.get("user_markdown", ""))
        self.assertTrue(str(meta.get("forecast_id", "")).startswith("tw_"))
        self.assertLess(elapsed_ms, 200.0, f"PreInvocation hook took {elapsed_ms:.1f}ms (>200ms SLA)")

        # Verify Claude UserPromptSubmit contract
        claude_payload = {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Refactor validate_token",
            "cwd": str(self.root),
        }
        c_out, _ = self._run_hook("pre-prompt-predict.py", json.dumps(claude_payload))
        self.assertEqual(c_out.get("decision"), "allow")
        self.assertEqual(
            c_out.get("hookSpecificOutput", {}).get("hookEventName"),
            "UserPromptSubmit",
        )

    def test_fail_open_on_corrupted_sqlite_and_malformed_stdin(self) -> None:
        # 1. Malformed JSON stdin -> must fail open with decision=allow in <200ms
        out_bad_json, ms1 = self._run_hook("pre-prompt-predict.py", "{not valid json!!!")
        self.assertEqual(out_bad_json.get("decision"), "allow")
        self.assertEqual(out_bad_json.get("systemMessage"), "")
        self.assertLess(ms1, 200.0)

        out_guard_bad, ms2 = self._run_hook("pre-tool-guard.py", "<<<corrupted stdin>>>")
        self.assertEqual(out_guard_bad.get("decision"), "allow")
        self.assertLess(ms2, 200.0)

        # 2. Corrupted .tokwiz/graph.db -> must fail open without crashing
        tokwiz_dir = self.root / ".tokwiz"
        tokwiz_dir.mkdir(parents=True, exist_ok=True)
        (tokwiz_dir / "graph.db").write_bytes(b"CORRUPTED_SQLITE_HEADER_BYTES_12345")

        payload = {
            "hook_event_name": "PreInvocation",
            "prompt": "Fix validate_token bug",
            "cwd": str(self.root),
        }
        out_corrupt_db, ms3 = self._run_hook("pre-prompt-predict.py", json.dumps(payload))
        self.assertEqual(out_corrupt_db.get("decision"), "allow")
        self.assertLess(ms3, 200.0)

    def test_tokwiz_init_wires_all_agents(self) -> None:
        # Pre-populate existing project instructions, skills, and hooks to verify non-destructive merge
        (self.root / "AGENTS.md").write_text("# My Project Custom Rules\n- Always run linter.\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("# Existing Claude Instructions\n", encoding="utf-8")
        custom_skill = self.root / ".claude" / "skills" / "my-existing-skill" / "SKILL.md"
        custom_skill.parent.mkdir(parents=True, exist_ok=True)
        custom_skill.write_text("# Custom Skill\n", encoding="utf-8")
        (self.root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"matcher": ".*", "hooks": [{"type": "command", "command": "echo custom-stop"}]}]}}),
            encoding="utf-8",
        )

        rc = cli_mod.main(["init", "--agent", "all", "--root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / ".tokwiz" / "graph.db").exists())
        self.assertTrue((self.root / ".claude" / "skills" / "tokwiz" / "SKILL.md").exists())
        self.assertTrue(custom_skill.exists())
        self.assertTrue((self.root / ".claude" / "settings.json").exists())
        self.assertTrue((self.root / ".agents" / "skills" / "tokwiz" / "SKILL.md").exists())
        self.assertTrue((self.root / ".agents" / "hooks.json").exists())
        self.assertIn("# My Project Custom Rules", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("tokwiz", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("# Existing Claude Instructions", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("echo custom-stop", (self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "GEMINI.md").exists())
        self.assertTrue((self.root / ".cursorrules").exists())
        self.assertIn(".tokwiz/", (self.root / ".gitignore").read_text(encoding="utf-8"))

    def test_ide_wrapper_tags_ignored_and_stripped(self) -> None:
        # Pure IDE / background task notification tags must NOT create a forecast
        for noisy_xml in (
            "<ide_opened_file>The user opened the file /tmp/CLAUDE.md in the IDE.</ide_opened_file>",
            "<task-notification><task-id>bd2l9nsz1</task-id><tool-use-id>toolu_01</tool-use-id></task-notification>",
            "<future-agent-event>internal metadata</future-agent-event>",
        ):
            ide_only = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": noisy_xml,
                "cwd": str(self.root),
            }
            out_only, _ = self._run_hook("pre-prompt-predict.py", json.dumps(ide_only))
            self.assertEqual(out_only.get("decision"), "allow")
            self.assertFalse((self.root / ".tokwiz" / "forecasts.jsonl").exists())

        # Mixed IDE tag + real user prompt must strip the tag and keep only the real prompt
        mixed = {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "<ide_opened_file>Opened CLAUDE.md</ide_opened_file>\nRefactor validate_token",
            "cwd": str(self.root),
        }
        out_mixed, _ = self._run_hook("pre-prompt-predict.py", json.dumps(mixed))
        self.assertEqual(out_mixed.get("decision"), "allow")
        lines = (self.root / ".tokwiz" / "forecasts.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["prompt"], "Refactor validate_token")

    def test_tokwiz_uninstall_preserves_project_files(self) -> None:
        custom_skill = self.root / ".claude" / "skills" / "custom" / "SKILL.md"
        custom_skill.parent.mkdir(parents=True, exist_ok=True)
        custom_skill.write_text("# Keep custom skill\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# My Project Custom Rules\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("# Existing Claude Instructions\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        (self.root / ".mcp.json").write_text(json.dumps({"mcpServers": {"custom": {"command": "echo"}}}), encoding="utf-8")
        (self.root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"matcher": ".*", "hooks": [{"type": "command", "command": "echo custom-stop"}]}]}}),
            encoding="utf-8",
        )

        self.assertEqual(cli_mod.main(["init", "--agent", "all", "--root", str(self.root)]), 0)
        self.assertEqual(cli_mod.main(["uninstall", "--root", str(self.root)]), 0)

        # TokWiz artifacts removed
        self.assertFalse((self.root / ".tokwiz").exists())
        self.assertFalse((self.root / ".claude" / "skills" / "tokwiz").exists())
        self.assertFalse((self.root / ".agents" / "skills" / "tokwiz").exists())
        self.assertFalse((self.root / ".agents" / "rules" / "tokwiz.md").exists())
        self.assertFalse((self.root / "GEMINI.md").exists())
        self.assertFalse((self.root / ".cursorrules").exists())

        # User/project files preserved intact
        self.assertTrue(custom_skill.exists())
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), "# My Project Custom Rules\n")
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), "# Existing Claude Instructions\n")
        self.assertEqual((self.root / ".gitignore").read_text(encoding="utf-8"), "node_modules/\n")
        mcp_cfg = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))
        self.assertIn("custom", mcp_cfg["mcpServers"])
        self.assertNotIn("tokwiz", mcp_cfg["mcpServers"])
        claude_cfg = json.loads((self.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("echo custom-stop", json.dumps(claude_cfg))
        self.assertNotIn("post-turn-record.py", json.dumps(claude_cfg))

        # Verify `tokwiz init --portable` emits zero machine-specific absolute paths
        self.assertEqual(cli_mod.main(["init", "--agent", "claude", "--portable", "--root", str(self.root)]), 0)
        portable_settings = (self.root / ".claude" / "settings.json").read_text(encoding="utf-8")
        portable_mcp = (self.root / ".mcp.json").read_text(encoding="utf-8")
        self.assertIn("tokwiz hook pre-tool-guard.py", portable_settings)
        self.assertIn("tokwiz hook session-start-resume.py", portable_settings)
        self.assertNotIn(str(REPO_ROOT), portable_settings)
        self.assertNotIn(str(self.root), portable_mcp)
        self.assertEqual(cli_mod.main(["hook", "pre-tool-guard.py"]), 0)

        # Verify SessionStart auto-resume hook injects checkpoint on clear/compact and stays silent on startup
        (self.root / ".tokwiz" / "checkpoint.md").write_text("# TokWiz Checkpoint\n- Branch: main\n", encoding="utf-8")
        out_clear, _ = self._run_hook(
            "session-start-resume.py",
            json.dumps({"cwd": str(self.root), "source": "clear", "hook_event_name": "SessionStart"}),
        )
        self.assertIn("TokWiz Checkpoint", out_clear["hookSpecificOutput"]["additionalContext"])
        out_startup, _ = self._run_hook(
            "session-start-resume.py",
            json.dumps({"cwd": str(self.root), "source": "startup", "hook_event_name": "SessionStart"}),
        )
        self.assertEqual(out_startup, {})

        # Re-running `tokwiz init` WITHOUT `--portable` (as `tokwiz update` does) must preserve portable mode!
        (self.root / "CLAUDE.md").write_text("# Custom\nTokWiz Protocol (old rule line)\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Custom\nIf `.tokwiz/` or `.agents/skills/tokwiz/SKILL.md` old line\n", encoding="utf-8")
        self.assertEqual(cli_mod.main(["init", "--agent", "all", "--root", str(self.root)]), 0)
        after_reinit_settings = (self.root / ".claude" / "settings.json").read_text(encoding="utf-8")
        after_reinit_mcp = (self.root / ".mcp.json").read_text(encoding="utf-8")
        self.assertIn("tokwiz hook pre-tool-guard.py", after_reinit_settings)
        self.assertNotIn(str(REPO_ROOT), after_reinit_settings)
        self.assertNotIn(str(self.root), after_reinit_mcp)

        for rule_file in (
            self.root / "CLAUDE.md",
            self.root / "AGENTS.md",
            self.root / "GEMINI.md",
            self.root / ".cursorrules",
            self.root / ".agents" / "rules" / "tokwiz.md",
        ):
            rule_txt = rule_file.read_text(encoding="utf-8")
            self.assertIn("tokwiz_map", rule_txt)
            self.assertIn("tokwiz_diff_slice", rule_txt)
            self.assertIn("tokwiz_resume", rule_txt)
            self.assertNotIn("old rule line", rule_txt)
            self.assertNotIn("old line", rule_txt)


if __name__ == "__main__":
    unittest.main()
