"""Tier 1 & Tier 3 tests for Forecast vs. Real Cost Reconciliation, Self-Calibration, and Analytics."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
HOOKS_DIR = REPO_ROOT / "hooks"
DASHBOARD_DIR = REPO_ROOT / "dashboard"
EVAL_DIR = REPO_ROOT / "evaluations"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cal_mod = _load_module("calibration", SCRIPTS_DIR / "calibration.py")
tel_mod = _load_module("telemetry", SCRIPTS_DIR / "telemetry.py")
post_hook_mod = _load_module("post_turn_record", HOOKS_DIR / "post-turn-record.py")
pre_hook_mod = _load_module("pre_prompt_predict", HOOKS_DIR / "pre-prompt-predict.py")
srv_mod = _load_module("dashboard_server", DASHBOARD_DIR / "server.py")
replay_mod = _load_module("replay_transcripts", EVAL_DIR / "replay-transcripts.py")


class TestCostReconciliationAndCalibration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "auth.ts").write_text(
            "export function validateToken(t: string): boolean {\n  return t.length > 10;\n}\n"
            "export function requireAuth(h: string): boolean {\n  return validateToken(h);\n}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_multi_provider_usage_parsing(self) -> None:
        # 1. Claude Code usage payload
        u_claude = cal_mod.parse_usage_payload(
            {
                "agent_host": "claude",
                "model": "claude-sonnet-4",
                "real_steps": 5,
                "usage": {
                    "input_tokens": 600,
                    "cache_read_input_tokens": 2400,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 350,
                    "reasoning_tokens": 200,
                },
            }
        )
        self.assertEqual(u_claude.agent_host, "claude")
        self.assertEqual(u_claude.input_tokens, 3000)
        self.assertEqual(u_claude.cached_input_tokens, 2400)
        self.assertEqual(u_claude.output_tokens, 350)

        # 2. OpenAI Codex CLI usage payload
        u_codex = cal_mod.parse_usage_payload(
            {
                "agent_host": "codex",
                "model": "o4-mini",
                "real_steps": 4,
                "usage": {
                    "prompt_tokens": 2500,
                    "prompt_tokens_details": {"cached_tokens": 2000},
                    "completion_tokens": 600,
                    "completion_tokens_details": {"reasoning_tokens": 250},
                },
            }
        )
        self.assertEqual(u_codex.agent_host, "codex")
        self.assertEqual(u_codex.input_tokens, 2500)
        self.assertEqual(u_codex.cached_input_tokens, 2000)
        self.assertEqual(u_codex.reasoning_tokens, 250)
        self.assertEqual(u_codex.output_tokens, 350)

        # 3. Gemini CLI / Antigravity usageMetadata payload
        u_gemini = cal_mod.parse_usage_payload(
            {
                "agent_host": "gemini",
                "model": "gemini-2.5-pro",
                "real_steps": 3,
                "usageMetadata": {
                    "promptTokenCount": 1800,
                    "cachedContentTokenCount": 1400,
                    "candidatesTokenCount": 300,
                    "thoughtsTokenCount": 150,
                },
            }
        )
        self.assertEqual(u_gemini.agent_host, "gemini")
        self.assertEqual(u_gemini.input_tokens, 1800)
        self.assertEqual(u_gemini.cached_input_tokens, 1400)
        self.assertEqual(u_gemini.output_tokens, 300)
        self.assertEqual(u_gemini.reasoning_tokens, 150)

    def test_variance_root_cause_attribution(self) -> None:
        forecast = {
            "estimated_steps": 3,
            "tool_loop_tokens": 600,
            "reasoning_tokens": 200,
        }
        drifted_usage = cal_mod.ActualUsage(
            agent_host="claude",
            model_id="claude-sonnet-4",
            real_steps=8,              # > 3 * 1.35 -> EXTRA_TOOL_LOOPS
            input_tokens=4000,
            cached_input_tokens=400,   # 10% < 45% -> CACHE_MISS_DRIFT
            tool_output_tokens=1600,   # > 600 * 1.5 -> UNBOUNDED_TOOL_OUTPUT
            reasoning_tokens=600,      # > 200 * 1.5 -> REASONING_SPIKE
            output_tokens=300,
        )
        tags = cal_mod.attribute_variance_causes(forecast, drifted_usage)
        self.assertIn("EXTRA_TOOL_LOOPS", tags)
        self.assertIn("CACHE_MISS_DRIFT", tags)
        self.assertIn("REASONING_SPIKE", tags)
        self.assertIn("UNBOUNDED_TOOL_OUTPUT", tags)

        # High cache-hit turn (e.g. 97% cache_read_input_tokens) must not falsely trigger UNBOUNDED_TOOL_OUTPUT
        cached_usage = cal_mod.parse_usage_payload(
            {
                "agent_host": "claude",
                "real_steps": 3,
                "usage": {
                    "input_tokens": 1500,
                    "cache_read_input_tokens": 48500,
                    "output_tokens": 200,
                },
            }
        )
        self.assertEqual(cached_usage.effective_tokens, 1500 + 4850 + 200)
        self.assertEqual(
            cal_mod.attribute_variance_causes({"total_tokens": 6000, "estimated_steps": 3, "tool_loop_tokens": 1200}, cached_usage),
            ["ON_TARGET"],
        )
        self.assertEqual(
            cal_mod.attribute_variance_causes({"total_tokens": 2000, "estimated_steps": 3, "tool_loop_tokens": 1200}, cached_usage),
            ["CONTEXT_GROWTH"],
        )
        bloated_usage = cal_mod.ActualUsage(
            model_id="m",
            agent_host="claude",
            input_tokens=260_000,
            cached_input_tokens=200_000,
            tool_output_tokens=100,
            reasoning_tokens=100,
            output_tokens=200,
            real_steps=2,
        )
        self.assertIn(
            "CONTEXT_BLOAT",
            cal_mod.attribute_variance_causes({"total_tokens": 2000, "estimated_steps": 2, "tool_loop_tokens": 500}, bloated_usage),
        )

    def test_pre_post_turn_reconciliation_and_telemetry_summary(self) -> None:
        # 1. Trigger PreInvocation hook to create .tokwiz/active-forecast.json
        pre_out = pre_hook_mod.handle_pre_prompt(
            {
                "hook_event_name": "PreInvocation",
                "prompt": "Refactor validateToken and update requireAuth",
                "cwd": str(self.root),
            }
        )
        forecast_id = pre_out["metadata"]["forecast_id"]

        # 2. Trigger PostInvocation hook with actual usage
        post_out = post_hook_mod.handle_post_turn(
            {
                "cwd": str(self.root),
                "agent_host": "claude",
                "model": "claude-sonnet-4",
                "real_steps": 6,
                "usage": {
                    "input_tokens": 900,
                    "cache_read_input_tokens": 3600,
                    "output_tokens": 410,
                    "reasoning_tokens": 260,
                },
            }
        )
        self.assertTrue(post_out["reconciled"])
        self.assertEqual(post_out["forecast_id"], forecast_id)

        # 3. Verify TelemetryStore summary & CLI report
        store = tel_mod.TelemetryStore(root_dir=self.root)
        summary = store.get_summary()
        self.assertEqual(summary["total_turns"], 1)
        self.assertGreater(summary["net_tokens_saved"], 0)
        self.assertAlmostEqual(summary["cache_hit_ratio_pct"], 80.0, places=1)
        self.assertEqual(summary["context_health"]["status"], "HEALTHY")
        self.assertEqual(summary["context_health"]["tokens_per_step"], 4500 // 6)
        report_str = tel_mod.render_cli_stats(summary)
        self.assertIn("TokWiz Optimization & Cost Report", report_str)
        self.assertIn(forecast_id, report_str)

    def test_ema_self_calibration_converges_under_20_pct_mape(self) -> None:
        # Simulate 8 historical turns with consistent repository-specific usage characteristics
        turns = [
            {
                "prompt": f"Refactor validateToken and requireAuth step {i}",
                "agent_host": "claude",
                "model": "claude-sonnet-4",
                "real_steps": 8,
                "usage": {
                    "input_tokens": 1100,
                    "cache_read_input_tokens": 4400,
                    "output_tokens": 520,
                    "reasoning_tokens": 340,
                },
            }
            for i in range(1, 9)
        ]
        res = replay_mod.replay_turns(self.root, turns, ema_alpha=0.45)
        self.assertEqual(res["turns_replayed"], 8)
        self.assertLessEqual(
            res["converged_mape_pct"],
            20.0,
            f"Expected converged MAPE <= 20%, got {res['converged_mape_pct']}%",
        )
        self.assertTrue((self.root / ".tokwiz" / "calibration.json").exists())

    def test_dashboard_http_server_endpoints(self) -> None:
        store = tel_mod.TelemetryStore(root_dir=self.root)
        store.record_turn(
            {
                "forecast_id": "tw_testdash",
                "prompt": "Refactor validateToken",
                "profile_code": "C",
                "optimization_level": 2,
                "forecast_total_tokens": 4000,
                "real_total_tokens": 4100,
                "unoptimized_baseline_tokens": 12000,
                "net_tokens_saved": 7610,
                "forecast_cost_usd": 0.015,
                "real_cost_usd": 0.0155,
                "unoptimized_cost_usd": 0.045,
                "net_cost_saved_usd": 0.0295,
                "cost_delta_usd": 0.0005,
                "ape_pct": 3.2,
                "variance_tags": ["ON_TARGET"],
            }
        )
        handler = srv_mod.create_handler(self.root)
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/stats") as resp:
                self.assertEqual(resp.status, 200)
                api_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(api_data["total_turns"], 1)
                self.assertEqual(api_data["turns"][0]["forecast_id"], "tw_testdash")

            with urlopen(f"http://127.0.0.1:{port}/api/graph") as resp_graph:
                self.assertEqual(resp_graph.status, 200)
                graph_data = json.loads(resp_graph.read().decode("utf-8"))
                self.assertGreaterEqual(graph_data["total_nodes"], 2)
                self.assertIn("nodes", graph_data)
                self.assertIn("edges", graph_data)

            with urlopen(f"http://127.0.0.1:{port}/api/workspaces") as resp_ws:
                self.assertEqual(resp_ws.status, 200)
                ws_data = json.loads(resp_ws.read().decode("utf-8"))
                self.assertEqual(ws_data["active_root"], str(self.root.resolve()))
                self.assertGreaterEqual(len(ws_data["workspaces"]), 1)

            with urlopen(f"http://127.0.0.1:{port}/api/clear") as resp_clr:
                self.assertEqual(resp_clr.status, 200)
                clr_data = json.loads(resp_clr.read().decode("utf-8"))
                self.assertTrue(clr_data["cleared"])

            with urlopen(f"http://127.0.0.1:{port}/") as resp_html:
                self.assertEqual(resp_html.status, 200)
                html = resp_html.read().decode("utf-8")
                self.assertIn("Interactive Project Graph", html)
        finally:
            server.shutdown()
            server.server_close()

    def test_dashboard_js_and_chart_invariants(self) -> None:
        """Catch UI/JS regressions: no sample mode, A/B scoped, every chart has ⓘ, and chart units match."""
        html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")

        # 1. Sample mode and A/B leakage must stay removed from normal dashboard UI
        for forbidden in ("demoBtn", "SAMPLE_DATA", "isDemoMode", "Preview Sample Session", "A/B Verified", "A/B vs Base", "Live Workspace", "Live Data"):
            self.assertNotIn(forbidden, html, f"Found forbidden stale UI string: {forbidden}")
        self.assertRegex(html, r'id="abProofSection"[^>]*style="[^"]*display:\s*none')
        self.assertIn('id="loadingBanner"', html)
        self.assertIn('id="workspaceSelect"', html)
        self.assertIn('id="daysFilterSelect"', html)
        self.assertIn('id="clearHistoryBtn"', html)
        self.assertIn('class="prompt-cell"', html)

        # 2. Every graph-header and chart-header <h2> must include an ⓘ info-tip
        headers = re.findall(r'<div class="(?:graph|chart)-header">.*?<h2>(.*?)</h2>', html, flags=re.DOTALL)
        self.assertGreaterEqual(len(headers), 5)
        for hdr in headers:
            self.assertIn('class="info-tip"', hdr, f"Missing ⓘ info-tip in header: {hdr}")
            self.assertIn('data-tip="', hdr, f"Missing data-tip in header: {hdr}")

        # 3. Extract main inline <script> and verify JS syntax + chart unit consistency via Node
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.DOTALL)
        main_js = max(scripts, key=len)
        node_bin = shutil.which("node")
        if node_bin:
            harness = r"""
const vm = require("vm");
const elements = {};
function makeEl(id) {
  return {
    id,
    textContent: "",
    innerHTML: "",
    value: "ALL",
    style: { display: "" },
    className: "",
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {},
    replaceChildren() {},
    appendChild() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getContext() { return { setTransform() {}, clearRect() {} }; },
    getBoundingClientRect() { return { width: 800, height: 400 }; },
  };
}
const chartConfigs = {};
class FakeChart {
  constructor(canvas, cfg) {
    chartConfigs[canvas.id] = cfg;
  }
  destroy() {}
}
FakeChart.defaults = { color: "", font: {} };
const doc = {
  documentElement: { setAttribute() {}, getAttribute() { return "dark"; } },
  createElement(tag) { return makeEl(tag); },
  getElementById(id) {
    if (!elements[id]) elements[id] = makeEl(id);
    return elements[id];
  },
  querySelectorAll() { return []; },
  addEventListener() {},
};
const ctx = {
  console,
  Math,
  Number,
  String,
  Array,
  Object,
  JSON,
  Promise,
  Date,
  Set,
  Map,
  Chart: FakeChart,
  document: doc,
  localStorage: { getItem() { return "dark"; }, setItem() {} },
  window: { devicePixelRatio: 1, addEventListener() {} },
  fetch: async () => ({ json: async () => ({}) }),
  requestAnimationFrame: () => 0,
  setTimeout: (fn) => 0,
};
ctx.window.Chart = FakeChart;
vm.runInNewContext(process.argv[1], ctx);

// Render a heavily cached turn where net_tokens_saved = 0
const payload = {
  workspace_name: "test-ws",
  total_turns: 1,
  total_forecasts: 1,
  total_real_tokens: 102000,
  total_effective_tokens: 21000,
  total_unoptimized_tokens: 21000,
  net_tokens_saved: 0,
  avg_reduction_pct: 0.0,
  total_forecast_cost_usd: 0.06,
  total_real_cost_usd: 0.06,
  net_cost_saved_usd: 0.0,
  total_cost_delta_usd: 0.0,
  mape_pct: 0.0,
  recent_mape_pct: 0.0,
  cache_hit_ratio_pct: 90.0,
  phase_breakdown: { context_input: 10000, graph_skill: 0, tool_output: 0, reasoning: 0, output: 2000 },
  model_breakdown: { "claude-sonnet-4": { turns: 1, forecast_cost_usd: 0.06, real_cost_usd: 0.06, saved_cost_usd: 0.0 } },
  turns: [{
    forecast_id: "tw_cache1",
    prompt: "Cached turn",
    profile_code: "C",
    telemetry_source: "TRANSCRIPT",
    real_input_tokens: 100000,
    real_cached_input_tokens: 90000,
    real_reasoning_tokens: 0,
    real_output_tokens: 2000,
    forecast_total_tokens: 21000,
    real_total_tokens: 102000,
    unoptimized_baseline_tokens: 21000,
    net_tokens_saved: 0,
    forecast_cost_usd: 0.06,
    real_cost_usd: 0.06,
    cost_delta_usd: 0.0,
    ape_pct: 0.0,
    variance_tags: ["ON_TARGET"],
    receipts: []
  }],
  forecasts: [],
  ab_comparison: null
};
ctx.renderDashboard(payload);
const tl = chartConfigs.tokensTimelineCanvas;
if (!tl) throw new Error("tokensTimelineCanvas not rendered");
const baselineBar = tl.data.datasets[0].data[0];
const tokwizBar = tl.data.datasets[1].data[0];
if (baselineBar !== 21000 || tokwizBar !== 21000) {
  throw new Error(`Chart unit mismatch: baseline=${baselineBar}, tokwiz=${tokwizBar} (expected both 21000)`);
}
if (doc.getElementById("abProofSection").style.display !== "none") {
  throw new Error("abProofSection must stay hidden when ab_comparison is null");
}
if (doc.getElementById("kpiTurnsSub").textContent.includes("A/B")) {
  throw new Error("kpiTurnsSub leaked A/B text in normal mode");
}
ctx.renderDashboard({...payload, context_health: { status: "BLOATED", tokens_per_step: 169100, current_context_tokens: 169100, avg_tokens_per_step: 124600, context_window_limit: 1000000, context_pct: 16.9, recommendation: "Bloated (>100k ctx) — Run /clear + tokwiz_resume" }});
if (!doc.getElementById("kpiContextHealth").textContent.includes("169.1k / 1.0M (17%)")) {
  throw new Error("kpiContextHealth did not render exact /context parity: " + doc.getElementById("kpiContextHealth").textContent);
}
if (!doc.getElementById("kpiContextSub").textContent.includes("Avg 124.6k/step")) {
  throw new Error("kpiContextSub did not render avg/step comparison: " + doc.getElementById("kpiContextSub").textContent);
}
"""
            proc = subprocess.run([node_bin, "-e", harness, main_js], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"Dashboard JS invariant failed:\n{proc.stderr}\n{proc.stdout}")

    def test_exact_last_step_context_parity_with_claude_context(self) -> None:
        transcript = self.root / "session.jsonl"
        # Step 1: 80k ctx, Step 2: 125k ctx, Step 3 (final): 169.1k ctx -> avg ~124.7k, exact final = 169.1k
        lines = [
            json.dumps({
                "type": "user",
                "timestamp": "2026-11-20T06:00:00Z",
                "message": {"content": "merged pr. continue with next stories"},
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 5000, "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 70000, "output_tokens": 500},
                    "content": [{"type": "text", "text": "step 1"}],
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 5000, "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 115000, "output_tokens": 600},
                    "content": [{"type": "text", "text": "step 2"}],
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 4100, "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 160000, "output_tokens": 700},
                    "content": [{"type": "text", "text": "done"}],
                },
            }),
        ]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

        parsed = post_hook_mod._parse_claude_transcript_turns(transcript, self.root)
        self.assertEqual(len(parsed), 1)
        payload = parsed[0]["usage_payload"]
        self.assertEqual(payload["last_step_context_tokens"], 169_100)

        engine = cal_mod.CalibrationEngine(root_dir=self.root)
        engine.reconcile_turn(
            forecast={
                "forecast_id": "tw_ctx_parity",
                "prompt": parsed[0]["prompt"],
                "profile_code": "C",
                "optimization_level": 2,
                "estimated_steps": 3,
                "total_tokens": 300_000,
                "costs": {"claude-opus-5-5": {"cached_cost_usd": 1.0, "uncached_cost_usd": 2.0, "unoptimized_cost_usd": 3.0}},
            },
            usage_payload=payload,
        )
        summary = tel_mod.TelemetryStore(root_dir=self.root).get_summary()
        ch = summary["context_health"]
        self.assertEqual(ch["current_context_tokens"], 169_100)
        self.assertEqual(ch["context_window_limit"], 1_000_000)
        self.assertEqual(ch["context_pct"], 16.9)
        self.assertEqual(ch["measurement_mode"], "EXACT_LAST_STEP")
        self.assertLess(ch["avg_tokens_per_step"], 169_100)
        self.assertEqual(ch["status"], "BLOATED")


if __name__ == "__main__":
    unittest.main()
