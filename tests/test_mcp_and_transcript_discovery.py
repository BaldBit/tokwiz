#!/usr/bin/env python3
"""Tests for Phase 5A (Native MCP Server) and Phase 5B (Claude Code & Codex transcript auto-discovery)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / "scripts" / "mcp-server.py"
POST_TURN_PATH = REPO_ROOT / "hooks" / "post-turn-record.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp_mod = _load("mcp_server_test", MCP_PATH)
post_mod = _load("post_turn_test", POST_TURN_PATH)


class TestMCPServerAndTranscriptDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(tempfile.mkdtemp(prefix="tokwiz_p5ab_"))
        (self.ws / "auth.py").write_text(
            "def validate_token(token: str) -> bool:\n    return token.startswith('tok_')\n\n"
            "def require_auth(hdr: str) -> bool:\n    return validate_token(hdr)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_mcp_initialize_tools_list_and_in_process_calls(self) -> None:
        init_resp = mcp_mod.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, self.ws)
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "tokwiz")

        list_resp = mcp_mod.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.ws)
        names = {t["name"] for t in list_resp["result"]["tools"]}
        self.assertTrue(
            {"tokwiz_graph_query", "tokwiz_slice", "tokwiz_skeleton", "tokwiz_predict", "tokwiz_stats"}.issubset(names)
        )

        # Call each MCP tool
        gq = mcp_mod.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "tokwiz_graph_query", "arguments": {"query_text": "validate_token"}}},
            self.ws,
        )
        self.assertFalse(gq["result"]["isError"])
        self.assertIn("validate_token", gq["result"]["content"][0]["text"])

        sk = mcp_mod.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "tokwiz_skeleton", "arguments": {"file_path": "auth.py"}}},
            self.ws,
        )
        self.assertFalse(sk["result"]["isError"])
        self.assertIn("def validate_token", sk["result"]["content"][0]["text"])

        pr = mcp_mod.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "tokwiz_predict", "arguments": {"prompt": "Refactor validate_token"}}},
            self.ws,
        )
        self.assertFalse(pr["result"]["isError"])
        self.assertIn("TokWiz Forecast", pr["result"]["content"][0]["text"])

        st = mcp_mod.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "tokwiz_stats", "arguments": {"days": 7}}},
            self.ws,
        )
        self.assertFalse(st["result"]["isError"])
        self.assertIn("total_turns", st["result"]["content"][0]["text"])

        tt = mcp_mod.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "tokwiz_test", "arguments": {}}},
            self.ws,
        )
        self.assertFalse(tt["result"]["isError"])
        self.assertIn("impacted tests", tt["result"]["content"][0]["text"])

        tr = mcp_mod.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "tokwiz_trace", "arguments": {"source": "require_auth", "target": "validate_token"}}},
            self.ws,
        )
        self.assertFalse(tr["result"]["isError"])
        self.assertIn("TokWiz Trace", tr["result"]["content"][0]["text"])
        self.assertIn("validate_token", tr["result"]["content"][0]["text"])

        # Verify `tokwiz init` registers MCP configs non-destructively
        (self.ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"custom": {"command": "echo"}}}), encoding="utf-8")
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "cli.py"), "init", "--agent", "all", "--root", str(self.ws)],
            capture_output=True,
            text=True,
            check=True,
        )
        mcp_cfg = json.loads((self.ws / ".mcp.json").read_text(encoding="utf-8"))
        self.assertIn("custom", mcp_cfg["mcpServers"])
        self.assertIn("tokwiz", mcp_cfg["mcpServers"])
        self.assertTrue((self.ws / ".cursor" / "mcp.json").exists())
        self.assertTrue((self.ws / ".agents" / "mcp.json").exists())

    def test_claude_code_and_codex_transcript_auto_discovery(self) -> None:
        claude_root = Path(tempfile.mkdtemp(prefix="tokwiz_claude_proj_"))
        codex_root = Path(tempfile.mkdtemp(prefix="tokwiz_codex_sess_"))
        saved_c = os.environ.get("TOKWIZ_CLAUDE_PROJECTS_DIR")
        saved_x = os.environ.get("TOKWIZ_CODEX_DIR")
        saved_b = os.environ.get("TOKWIZ_BRAIN_DIR")
        try:
            os.environ["TOKWIZ_CLAUDE_PROJECTS_DIR"] = str(claude_root)
            os.environ["TOKWIZ_CODEX_DIR"] = str(codex_root)
            os.environ["TOKWIZ_BRAIN_DIR"] = str(claude_root / "empty_brain")

            slug_dir = claude_root / post_mod._claude_project_slug(self.ws)
            slug_dir.mkdir(parents=True, exist_ok=True)
            claude_log = slug_dir / "session-1.jsonl"
            claude_log.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "timestamp": "2026-11-15T10:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "Refactor validate_token"}]}}),
                        json.dumps({"type": "user", "isMeta": True, "timestamp": "2026-11-15T10:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "Base directory for this skill: /tmp/.claude/skills/tokwiz # TokWiz Optimization Rules"}]}}),
                        json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 120, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 1000, "output_tokens": 80}, "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": str(self.ws / "auth.py"), "offset": 1, "limit": 3}}]}}),
                        json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "def validate_token..."}]}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            found_claude = post_mod._find_workspace_transcript_turns(self.ws)
            self.assertEqual(len(found_claude), 1)
            self.assertEqual(found_claude[0]["prompt"], "Refactor validate_token")
            self.assertEqual(found_claude[0]["usage_payload"]["agent_host"], "claude")
            self.assertEqual(found_claude[0]["tool_counts"]["ranged_read"], 1)

            # Now test Codex discovery when Claude dir is absent
            shutil.rmtree(slug_dir)
            codex_log = codex_root / "2026" / "11" / "15" / "rollout-1.jsonl"
            codex_log.parent.mkdir(parents=True, exist_ok=True)
            codex_log.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"cwd": str(self.ws)}}),
                        json.dumps({"type": "event_msg", "timestamp": "2026-11-15T10:05:00Z", "payload": {"type": "user_message", "message": "Check auth.py"}}),
                        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 250, "cached_input_tokens": 100, "output_tokens": 45, "reasoning_output_tokens": 15}}}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            found_codex = post_mod._find_workspace_transcript_turns(self.ws)
            self.assertEqual(len(found_codex), 1)
            self.assertEqual(found_codex[0]["prompt"], "Check auth.py")
            self.assertEqual(found_codex[0]["usage_payload"]["agent_host"], "codex")
        finally:
            for k, v in (("TOKWIZ_CLAUDE_PROJECTS_DIR", saved_c), ("TOKWIZ_CODEX_DIR", saved_x), ("TOKWIZ_BRAIN_DIR", saved_b)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(claude_root, ignore_errors=True)
            shutil.rmtree(codex_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
