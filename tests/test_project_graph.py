"""Tier 1 unit tests for Project Graph, Personalized PageRank, and Code Skeletonizer."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import time
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


skel_mod = _load_module("code_skeletonizer", "code-skeletonizer.py")
graph_mod = _load_module("project_graph", "project-graph.py")


class TestProjectGraphAndSkeletonizer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self._create_polyglot_fixtures()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _create_polyglot_fixtures(self) -> None:
        # 1. TypeScript auth + callers + callees
        (self.root / "src" / "crypto").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "auth").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "middleware").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "routes").mkdir(parents=True, exist_ok=True)
        (self.root / "pkg" / "py").mkdir(parents=True, exist_ok=True)
        (self.root / "pkg" / "go").mkdir(parents=True, exist_ok=True)
        (self.root / "pkg" / "rs").mkdir(parents=True, exist_ok=True)

        (self.root / "src" / "crypto" / "jwt.ts").write_text(
            "export interface TokenPayload {\n"
            "  sub: string;\n"
            "  exp: number;\n"
            "}\n\n"
            "export function verifySignature(raw: string): boolean {\n"
            "  return raw.length > 10;\n"
            "}\n",
            encoding="utf-8",
        )

        (self.root / "src" / "auth" / "session.ts").write_text(
            'import { verifySignature, TokenPayload } from "../crypto/jwt";\n\n'
            "export class SessionManager {\n"
            "  public validateToken(token: string): boolean {\n"
            "    if (!verifySignature(token)) {\n"
            "      return false;\n"
            "    }\n"
            "    return true;\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )

        (self.root / "src" / "middleware" / "requireAuth.ts").write_text(
            'import { SessionManager } from "../auth/session";\n\n'
            "export function requireAuth(header: string): boolean {\n"
            "  const mgr = new SessionManager();\n"
            "  return mgr.validateToken(header);\n"
            "}\n",
            encoding="utf-8",
        )

        (self.root / "src" / "routes" / "login.ts").write_text(
            'import { SessionManager } from "../auth/session";\n\n'
            "export async function handleLogin(tok: string): Promise<boolean> {\n"
            "  const s = new SessionManager();\n"
            "  return s.validateToken(tok);\n"
            "}\n",
            encoding="utf-8",
        )

        # 2. Python fixture
        (self.root / "pkg" / "py" / "billing.py").write_text(
            "import math\n\n"
            "class BaseCalculator:\n"
            "    def compute_tax(self, amount: float) -> float:\n"
            "        return round(amount * 0.15, 2)\n\n"
            "class InvoiceService(BaseCalculator):\n"
            "    def generate_invoice(self, subtotal: float) -> float:\n"
            "        tax = self.compute_tax(subtotal)\n"
            "        return subtotal + tax\n",
            encoding="utf-8",
        )

        # 3. Go fixture
        (self.root / "pkg" / "go" / "worker.go").write_text(
            "package worker\n\n"
            'import "fmt"\n\n'
            "type JobRunner struct {\n"
            "    Workers int\n"
            "}\n\n"
            "func DispatchBatch(count int) int {\n"
            "    return ProcessItem(count)\n"
            "}\n\n"
            "func ProcessItem(v int) int {\n"
            "    fmt.Println(v)\n"
            "    return v * 2\n"
            "}\n",
            encoding="utf-8",
        )

        # 4. Rust fixture
        (self.root / "pkg" / "rs" / "engine.rs").write_text(
            "use std::collections::HashMap;\n\n"
            "pub struct TokenCache {\n"
            "    pub capacity: usize,\n"
            "}\n\n"
            "pub fn hash_key(input: &str) -> usize {\n"
            "    input.len()\n"
            "}\n\n"
            "pub fn lookup_entry(key: &str) -> usize {\n"
            "    hash_key(key)\n"
            "}\n",
            encoding="utf-8",
        )

    def test_skill_md_under_350_tokens(self) -> None:
        skill_path = REPO_ROOT / "SKILL.md"
        self.assertTrue(skill_path.exists())
        tokens = skel_mod.estimate_tokens(skill_path.read_text(encoding="utf-8"))
        self.assertLess(tokens, 350, f"SKILL.md must be <350 tokens, got {tokens}")

    def test_multilang_ast_parsing_and_line_numbers(self) -> None:
        # Python
        py_res = skel_mod.parse_source_file(self.root / "pkg" / "py" / "billing.py", self.root)
        py_names = {s.name: s for s in py_res.symbols}
        self.assertIn("BaseCalculator", py_names)
        self.assertIn("InvoiceService", py_names)
        self.assertIn("generate_invoice", py_names)
        self.assertEqual(py_names["InvoiceService"].bases, ["BaseCalculator"])
        self.assertIn("compute_tax", py_names["generate_invoice"].calls)
        self.assertEqual(py_names["BaseCalculator"].start_line, 3)
        self.assertEqual(py_names["BaseCalculator"].end_line, 5)

        # TypeScript
        ts_res = skel_mod.parse_source_file(self.root / "src" / "auth" / "session.ts", self.root)
        ts_names = {s.name: s for s in ts_res.symbols}
        self.assertIn("SessionManager", ts_names)
        self.assertIn("validateToken", ts_names)
        self.assertIn("verifySignature", ts_names["validateToken"].calls)
        self.assertEqual(ts_names["validateToken"].start_line, 4)
        self.assertEqual(ts_names["validateToken"].end_line, 9)

        # Go
        go_res = skel_mod.parse_source_file(self.root / "pkg" / "go" / "worker.go", self.root)
        go_names = {s.name: s for s in go_res.symbols}
        self.assertIn("JobRunner", go_names)
        self.assertIn("DispatchBatch", go_names)
        self.assertIn("ProcessItem", go_names["DispatchBatch"].calls)

        # Rust
        rs_res = skel_mod.parse_source_file(self.root / "pkg" / "rs" / "engine.rs", self.root)
        rs_names = {s.name: s for s in rs_res.symbols}
        self.assertIn("TokenCache", rs_names)
        self.assertIn("lookup_entry", rs_names)
        self.assertIn("hash_key", rs_names["lookup_entry"].calls)

    def test_code_skeletonizer_compression_and_budget(self) -> None:
        skeleton = skel_mod.skeletonize_file(self.root / "pkg" / "py" / "billing.py", self.root)
        self.assertIn("[TokWiz AST Skeleton: pkg/py/billing.py", skeleton)
        self.assertIn("L3-L5: class BaseCalculator", skeleton)
        self.assertIn("L8-L10: def generate_invoice", skeleton)

        capped = skel_mod.skeletonize_file(
            self.root / "pkg" / "py" / "billing.py", self.root, max_tokens=25
        )
        self.assertLessEqual(skel_mod.estimate_tokens(capped), 40)

    def test_pagerank_query_and_caller_callee_resolution(self) -> None:
        graph = graph_mod.ProjectGraph(self.root)
        build_stats = graph.build()
        self.assertEqual(build_stats["indexed_files"], 7)

        res = graph.query("validateToken", max_tokens=400)
        self.assertTrue(len(res.matched_symbols) >= 1)
        self.assertEqual(res.matched_symbols[0]["name"], "validateToken")

        caller_names = {c["name"] for c in res.callers}
        callee_names = {c["name"] for c in res.callees}
        self.assertIn("requireAuth", caller_names)
        self.assertIn("handleLogin", caller_names)
        self.assertIn("verifySignature", callee_names)
        self.assertIn("DEF src/auth/session.ts:L4-L9", res.formatted_slice)
        self.assertIn("CALLERS (2):", res.formatted_slice)
        self.assertIn("CALLEES (1):", res.formatted_slice)
        self.assertLessEqual(res.token_count, 400)

        # Deterministic PageRank order across repeated queries
        res2 = graph.query("validateToken", max_tokens=400)
        self.assertEqual(res.formatted_slice, res2.formatted_slice)

    def test_max_tokens_budget_enforcement(self) -> None:
        graph = graph_mod.ProjectGraph(self.root)
        graph.build()
        res = graph.query("validateToken", max_tokens=45)
        self.assertLessEqual(res.token_count, 45)

    def test_incremental_indexing_and_latency(self) -> None:
        graph = graph_mod.ProjectGraph(self.root)
        first = graph.build()
        self.assertEqual(first["indexed_files"], 7)

        # Unchanged incremental build must skip all 7 files and run in <40ms
        t0 = time.perf_counter()
        second = graph.build()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertEqual(second["indexed_files"], 0)
        self.assertEqual(second["unchanged_files"], 7)
        self.assertLess(elapsed_ms, 40.0, f"Incremental no-op took {elapsed_ms:.2f}ms (>40ms SLA)")

        # Modify a single file -> only 1 file re-indexed
        login_file = self.root / "src" / "routes" / "login.ts"
        login_file.write_text(
            login_file.read_text(encoding="utf-8") + "\nexport function logout(): void {}\n",
            encoding="utf-8",
        )
        third = graph.build()
        self.assertEqual(third["indexed_files"], 1)
        self.assertEqual(third["unchanged_files"], 6)

        # Delete a file -> removed from SQLite
        login_file.unlink()
        fourth = graph.build()
        self.assertEqual(fourth["deleted_files"], 1)
        self.assertEqual(fourth["total_files"], 6)

    def test_one_shot_slice_bundler_and_structured_skeletons(self) -> None:
        import json

        graph = graph_mod.ProjectGraph(self.root)
        graph.build()
        sliced = graph.slice_code("validateToken", max_tokens=500)
        self.assertIn("[TokWiz Code Slice: validateToken", sliced.formatted_slice)
        self.assertIn("--- DEF", sliced.formatted_slice)
        self.assertLessEqual(sliced.token_count, 500)

        # Structured JSON/YAML/TOML/SQL skeletonization
        json_file = self.root / "config.json"
        json_file.write_text(
            json.dumps({"service": "api", "items": [{"id": i, "name": f"item_{i}"} for i in range(60)]}, indent=2),
            encoding="utf-8",
        )
        json_skel = skel_mod.skeletonize_file(json_file, root_dir=self.root)
        self.assertIn("Structured Skeleton: config.json", json_skel)
        self.assertIn("similar entries omitted", json_skel)

        sql_file = self.root / "schema.sql"
        sql_file.write_text(
            "CREATE TABLE users (\n  id INT PRIMARY KEY,\n  email TEXT NOT NULL\n);\n"
            + "\n".join(f"INSERT INTO users VALUES ({i}, 'u{i}@example.com');" for i in range(40)),
            encoding="utf-8",
        )
        sql_skel = skel_mod.skeletonize_file(sql_file, root_dir=self.root)
        self.assertIn("CREATE TABLE users", sql_skel)
        self.assertIn("similar INSERT rows omitted", sql_skel)

    def test_hierarchical_graph_export_and_expand(self) -> None:
        graph = graph_mod.ProjectGraph(self.root)
        graph.build()
        base = graph.export_graph_data(max_nodes=4, auto_build=False)
        self.assertLessEqual(len(base["nodes"]), 4)
        self.assertIsNone(base["expanded_node_id"])
        # File hubs should be prioritized in top-hierarchy export
        self.assertTrue(any(n["kind"] == "file" for n in base["nodes"]))

        file_node = next(n for n in base["nodes"] if n["kind"] == "file")
        expanded = graph.export_graph_data(max_nodes=4, auto_build=False, expand_id=str(file_node["id"]))
        self.assertEqual(expanded["expanded_node_id"], file_node["id"])
        self.assertGreaterEqual(len(expanded["nodes"]), len(base["nodes"]))


if __name__ == "__main__":
    unittest.main()
