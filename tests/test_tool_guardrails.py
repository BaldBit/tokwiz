"""Tier 1 & Tier 2 unit tests for Anti-Wastage Tool Guardrails and Output Filter."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
HOOKS_DIR = REPO_ROOT / "hooks"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


guard_mod = _load_module("pre_tool_guard", HOOKS_DIR / "pre-tool-guard.py")
filter_mod = _load_module("output_filter", SCRIPTS_DIR / "output-filter.py")


class TestToolGuardrailsAndOutputFilter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_lockfile_and_artifact_blocking(self) -> None:
        blocked_paths = [
            "package-lock.json",
            "frontend/pnpm-lock.yaml",
            "yarn.lock",
            "rust/Cargo.lock",
            "poetry.lock",
            "node_modules/react/index.js",
            "dist/bundle.js",
            "build/output.js",
            ".next/static/chunk.js",
            "static/app.min.js",
            "assets/logo.png",
        ]
        for p in blocked_paths:
            res = guard_mod.handle_pre_tool_use(
                {"tool_name": "read_file", "tool_input": {"Path": p}, "cwd": str(self.root)}
            )
            self.assertEqual(res["decision"], "block", f"Expected {p} to be blocked")
            self.assertIn("[TokWiz Guardrail]", str(res["reason"]))

        # Blocking shell cat on lockfile
        shell_res = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "run_command",
                "tool_input": {"CommandLine": "cat package-lock.json"},
                "cwd": str(self.root),
            }
        )
        self.assertEqual(shell_res["decision"], "block")

    def test_unbounded_300_line_file_interception_and_ranged_allow(self) -> None:
        large_py = self.root / "service.py"
        lines = ["import os\n"]
        for i in range(1, 80):
            lines.append(
                f"def handler_{i}(x: int) -> int:\n"
                f"    val = x + {i}\n"
                f"    return val * 2\n\n"
            )
        large_py.write_text("".join(lines), encoding="utf-8")
        self.assertGreater(len(large_py.read_text(encoding="utf-8").splitlines()), 300)

        # Unbounded read (>300 lines without StartLine/EndLine) -> intercepted with AST skeleton
        res_unbounded = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "read_file",
                "tool_input": {"Path": str(large_py)},
                "cwd": str(self.root),
            }
        )
        self.assertEqual(res_unbounded["decision"], "block")
        self.assertIn("[TokWiz AST Skeleton:", str(res_unbounded["reason"]))
        self.assertIn("def handler_1(x: int) -> int", str(res_unbounded["reason"]))

        # Ranged read (with StartLine & EndLine) -> allowed!
        res_ranged = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "read_file",
                "tool_input": {"Path": str(large_py), "StartLine": 1, "EndLine": 40},
                "cwd": str(self.root),
            }
        )
        self.assertEqual(res_ranged["decision"], "allow")

        # Small file (<=300 lines) -> allowed without range
        small_py = self.root / "small.py"
        small_py.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        res_small = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "read_file",
                "tool_input": {"Path": str(small_py)},
                "cwd": str(self.root),
            }
        )
        self.assertEqual(res_small["decision"], "allow")

    def test_shell_output_filter_compression(self) -> None:
        noisy_log = "\n".join(
            [
                "\x1b[32mDownloading dependencies...\x1b[0m",
                "[====================] 100%",
                *[f"tests/test_suite.py::test_case_{i} PASSED [ {i}%]" for i in range(1, 90)],
                "═══════════════════ FAILURES ═══════════════════",
                "FAILED tests/test_auth.py::test_expired_token - AssertionError: Expected 401 got 200",
                '  File "src/auth.py", line 42, in validate',
                *[f'  File "src/deep_{k}.py", line {k}, in call_{k}' for k in range(1, 20)],
                "1 failed, 89 passed in 2.41s",
            ]
        )
        compressed = filter_mod.filter_output(noisy_log, max_tokens=400, max_stack_frames=5)
        self.assertNotIn("\x1b[", compressed)
        self.assertNotIn("test_case_50 PASSED", compressed)
        self.assertIn("AssertionError: Expected 401 got 200", compressed)
        self.assertIn("1 failed, 89 passed in 2.41s", compressed)
        self.assertIn("[TokWiz Filter: omitted", compressed)
        self.assertLess(len(compressed), len(noisy_log) * 0.35)

    def test_git_diff_lockfile_stripping(self) -> None:
        diff_text = (
            "diff --git a/package-lock.json b/package-lock.json\n"
            "index 111..222 100644\n"
            "+  \"integrity\": \"sha512-xyz\"\n"
            "+  \"resolved\": \"https://registry.npmjs.org/foo\"\n"
            "diff --git a/src/index.ts b/src/index.ts\n"
            "+export const version = 2;\n"
        )
        filtered = filter_mod.filter_output(diff_text)
        self.assertIn("[TokWiz Filter: omitted 3 diff lines for package-lock.json]", filtered)
        self.assertIn("+export const version = 2;", filtered)

    def test_structured_data_150_line_interception_and_session_read_dedup(self) -> None:
        # 1. >150-line JSON file must be intercepted with structured skeleton
        big_json = self.root / "data.json"
        big_json.write_text(
            "{\n" + ",\n".join(f'  "k_{i}": {i}' for i in range(170)) + "\n}\n",
            encoding="utf-8",
        )
        res_json = guard_mod.handle_pre_tool_use(
            {"tool_name": "read_file", "tool_input": {"Path": str(big_json)}, "cwd": str(self.root)},
            default_root=self.root,
        )
        self.assertEqual(res_json["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(">150-line unbounded read limit", res_json["hookSpecificOutput"]["permissionDecisionReason"])

        # 2. Session File-Read Deduplicator: first read allowed, second identical read cached, edit invalidates
        small_py = self.root / "helper.py"
        small_py.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        first = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "read_file",
                "tool_input": {"Path": str(small_py), "StartLine": 1, "EndLine": 2},
                "cwd": str(self.root),
                "session_id": "sess-dedup",
            },
            default_root=self.root,
        )
        self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "allow")

        second = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "read_file",
                "tool_input": {"Path": str(small_py), "StartLine": 1, "EndLine": 2},
                "cwd": str(self.root),
                "session_id": "sess-dedup",
            },
            default_root=self.root,
        )
        self.assertEqual(second["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[TokWiz Cache Hit]", second["hookSpecificOutput"]["permissionDecisionReason"])

        # Modify file -> SHA-1 changes -> fresh read must be allowed
        small_py.write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
        third = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "read_file",
                "tool_input": {"Path": str(small_py), "StartLine": 1, "EndLine": 2},
                "cwd": str(self.root),
                "session_id": "sess-dedup",
            },
            default_root=self.root,
        )
        self.assertEqual(third["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_grep_interception_on_indexed_symbol(self) -> None:
        pg = guard_mod._load_project_graph()
        src = self.root / "billing.py"
        src.write_text("def compute_invoice(amount: float) -> float:\n    return amount * 1.1\n", encoding="utf-8")
        pg.ProjectGraph(root_dir=self.root).build()

        res = guard_mod.handle_pre_tool_use(
            {
                "tool_name": "Grep",
                "tool_input": {"pattern": "compute_invoice"},
                "cwd": str(self.root),
            },
            default_root=self.root,
        )
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Intercepted repo-wide grep", res["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("compute_invoice", res["hookSpecificOutput"]["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
