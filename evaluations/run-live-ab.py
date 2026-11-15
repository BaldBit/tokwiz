#!/usr/bin/env python3
"""Headless Live A/B Test Runner (`evaluations/run-live-ab.py`).

Automates paired head-to-head A/B evaluation between an unoptimized baseline
workspace (`project-baseline`) and a TokWiz-enabled workspace (`project-tokwiz`)
using headless AI coding agent CLIs (`claude`, `codex`, `gemini`).
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SETUP_DEMO = _REPO_ROOT / "demo" / "setup-ab-demo.py"

DEFAULT_LIVE_TASKS = [
    {
        "id": "auth_refactor",
        "name": "Multi-File Auth Refactor",
        "prompt": (
            "Refactor validate_token to accept an optional api_key: str = '' parameter, "
            "update require_auth if needed, and ensure all unit tests pass."
        ),
    },
    {
        "id": "tax_edit",
        "name": "Regional Tax Rule Edit",
        "prompt": (
            "In order_engine.py, add support for region 'JP' with a 10% tax rate in "
            "apply_regional_tax and add a unit test verifying it."
        ),
    },
    {
        "id": "call_chain_trace",
        "name": "Cross-Module Call-Chain Trace",
        "prompt": (
            "Trace how process_checkout calls validate_token and reply with the exact "
            "file paths and line numbers."
        ),
    },
]


@dataclass
class AgentRunResult:
    agent: str
    workspace: str
    task_id: str
    returncode: int
    elapsed_ms: float
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    uncached_tokens: int
    total_tokens: int
    cost_usd: Optional[float]
    num_turns: int = 0
    raw_error: str = ""


def build_agent_command(
    agent: str,
    prompt: str,
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """Build the headless CLI command for the requested coding agent."""
    extra = list(extra_args or [])
    low = agent.lower().strip()
    if low == "claude":
        return [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "project,local",
            "--strict-mcp-config",
            "--no-session-persistence",
            *extra,
        ]
    if low == "codex":
        return ["codex", "exec", prompt, "--json", "--full-auto", *extra]
    if low == "gemini":
        return ["gemini", "-p", prompt, "--output-format", "json", "--yolo", *extra]
    raise ValueError(f"Unsupported agent '{agent}'. Choose from: claude, codex, gemini")


def _extract_json_objects(raw_text: str) -> List[Dict[str, object]]:
    """Parse either a single JSON document or NDJSON lines."""
    text = (raw_text or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except Exception:
        pass

    objs: List[Dict[str, object]] = []
    for line in text.splitlines():
        ln = line.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                objs.append(obj)
        except Exception:
            continue
    return objs


def parse_agent_usage_json(stdout_text: str, agent: str) -> Tuple[int, int, int, int, Optional[float]]:
    """Extract (input_tokens, cached_tokens, output_tokens, total_tokens, cost_usd)."""
    objs = _extract_json_objects(stdout_text)
    inp = 0
    cached = 0
    out = 0
    cost: Optional[float] = None

    for obj in objs:
        # 1. Claude Code `--output-format json`
        if "total_cost_usd" in obj and isinstance(obj["total_cost_usd"], (int, float)):
            cost = float(obj["total_cost_usd"])
        usage = obj.get("usage")
        if isinstance(usage, dict):
            inp += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) + int(
                usage.get("cache_creation_input_tokens") or 0
            )
            cached += int(
                usage.get("cache_read_input_tokens")
                or usage.get("cached_tokens")
                or usage.get("cached_input_tokens")
                or 0
            )
            out += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            continue

        # 2. Gemini CLI `--output-format json` stats.models.<model>.tokens
        stats = obj.get("stats")
        if isinstance(stats, dict):
            models = stats.get("models")
            if isinstance(models, dict):
                for m_data in models.values():
                    if not isinstance(m_data, dict):
                        continue
                    toks = m_data.get("tokens")
                    if isinstance(toks, dict):
                        inp += int(toks.get("prompt") or 0)
                        cached += int(toks.get("cached") or 0)
                        out += int(toks.get("candidates") or 0) + int(toks.get("thoughts") or 0)

        # 3. Gemini usageMetadata fallback
        meta = obj.get("usageMetadata")
        if isinstance(meta, dict):
            inp += int(meta.get("promptTokenCount") or 0)
            cached += int(meta.get("cachedContentTokenCount") or 0)
            out += int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)

    total = inp + cached + out
    return inp, cached, out, total, cost


def _run_workspace_tests(ws_dir: Path) -> Dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(ws_dir),
        capture_output=True,
        text=True,
    )
    return {
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "summary": (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""],
    }


def _default_subprocess_runner(
    cmd: List[str],
    cwd: Path,
    timeout_sec: int,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


def run_live_ab(
    agent: str = "claude",
    tasks: Optional[List[Dict[str, str]]] = None,
    workspace_dir: Optional[Path] = None,
    extra_args: Optional[List[str]] = None,
    timeout_sec: int = 180,
    dry_run: bool = False,
    runner: Optional[Callable[[List[str], Path, int], subprocess.CompletedProcess]] = None,
) -> Dict[str, object]:
    """Execute paired live A/B runs across project-baseline and project-tokwiz."""
    task_list = tasks or list(DEFAULT_LIVE_TASKS)
    exec_fn = runner or _default_subprocess_runner

    if not dry_run and runner is None:
        exe = build_agent_command(agent, "ping", extra_args)[0]
        if shutil.which(exe) is None:
            raise FileNotFoundError(
                f"Headless CLI '{exe}' not found in PATH. Install it or run with --dry-run."
            )

    cleanup = False
    if workspace_dir is None:
        ws_root = Path(tempfile.mkdtemp(prefix="tokwiz_live_ab_"))
        cleanup = True
    else:
        ws_root = Path(workspace_dir).resolve()
        ws_root.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [sys.executable, str(_SETUP_DEMO), "--workspace-dir", str(ws_root)],
            capture_output=True,
            text=True,
            check=True,
        )
        baseline_dir = ws_root / "project-baseline"
        tokwiz_dir = ws_root / "project-tokwiz"

        paired_results: List[Dict[str, object]] = []
        base_sum = 0
        tok_sum = 0
        base_uncached_sum = 0
        tok_uncached_sum = 0

        for task in task_list:
            tid = str(task.get("id") or "task")
            tname = str(task.get("name") or tid)
            prompt = str(task["prompt"])
            cmd = build_agent_command(agent, prompt, extra_args)

            runs: Dict[str, AgentRunResult] = {}
            for label, target_dir in (("baseline", baseline_dir), ("tokwiz", tokwiz_dir)):
                if dry_run:
                    runs[label] = AgentRunResult(
                        agent=agent,
                        workspace=label,
                        task_id=tid,
                        returncode=0,
                        elapsed_ms=0.0,
                        input_tokens=0,
                        cached_tokens=0,
                        output_tokens=0,
                        uncached_tokens=0,
                        total_tokens=0,
                        cost_usd=None,
                    )
                    continue

                t0 = time.perf_counter()
                proc = exec_fn(cmd, target_dir, timeout_sec)
                elapsed = round((time.perf_counter() - t0) * 1000.0, 1)
                inp, cached, out, total, cost = parse_agent_usage_json(proc.stdout or "", agent)
                uncached = inp + out
                turns = 0
                for obj in _extract_json_objects(proc.stdout or ""):
                    if isinstance(obj.get("num_turns"), int):
                        turns = int(obj["num_turns"])
                runs[label] = AgentRunResult(
                    agent=agent,
                    workspace=label,
                    task_id=tid,
                    returncode=proc.returncode,
                    elapsed_ms=elapsed,
                    input_tokens=inp,
                    cached_tokens=cached,
                    output_tokens=out,
                    uncached_tokens=uncached,
                    total_tokens=total,
                    cost_usd=cost,
                    num_turns=turns,
                    raw_error=(proc.stderr or "").strip()[:300] if proc.returncode != 0 else "",
                )

            b_res = runs["baseline"]
            t_res = runs["tokwiz"]
            saved = b_res.total_tokens - t_res.total_tokens
            pct = round((saved / b_res.total_tokens) * 100.0, 1) if b_res.total_tokens > 0 else 0.0
            saved_uncached = b_res.uncached_tokens - t_res.uncached_tokens
            pct_uncached = (
                round((saved_uncached / b_res.uncached_tokens) * 100.0, 1)
                if b_res.uncached_tokens > 0
                else 0.0
            )
            base_sum += b_res.total_tokens
            tok_sum += t_res.total_tokens
            base_uncached_sum += b_res.uncached_tokens
            tok_uncached_sum += t_res.uncached_tokens

            paired_results.append(
                {
                    "task_id": tid,
                    "task_name": tname,
                    "command_preview": " ".join(shlex.quote(c) for c in cmd[:4]) + " ...",
                    "baseline": asdict(b_res),
                    "tokwiz": asdict(t_res),
                    "net_saved_uncached_tokens": saved_uncached,
                    "net_uncached_reduction_pct": pct_uncached,
                    "net_saved_tokens": saved,
                    "net_reduction_pct": pct,
                }
            )

        base_tests = _run_workspace_tests(baseline_dir)
        tok_tests = _run_workspace_tests(tokwiz_dir)
        total_saved = base_sum - tok_sum
        total_pct = round((total_saved / base_sum) * 100.0, 1) if base_sum > 0 else 0.0
        total_uncached_saved = base_uncached_sum - tok_uncached_sum
        total_uncached_pct = (
            round((total_uncached_saved / base_uncached_sum) * 100.0, 1)
            if base_uncached_sum > 0
            else 0.0
        )

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "dry_run": dry_run,
            "workspace_dir": str(ws_root),
            "tasks": paired_results,
            "baseline_uncached_tokens": base_uncached_sum,
            "tokwiz_uncached_tokens": tok_uncached_sum,
            "net_saved_uncached_tokens": total_uncached_saved,
            "net_uncached_reduction_pct": total_uncached_pct,
            "baseline_total_tokens": base_sum,
            "tokwiz_total_tokens": tok_sum,
            "net_saved_tokens": total_saved,
            "net_reduction_pct": total_pct,
            "baseline_tests": base_tests,
            "tokwiz_tests": tok_tests,
        }
    finally:
        if cleanup:
            shutil.rmtree(ws_root, ignore_errors=True)


def render_live_ab_markdown(report: Dict[str, object]) -> str:
    lines = [
        f"# TokWiz Live A/B Report (`{report['agent']}`)",
        "",
        "| Task | Baseline New Tok | TokWiz New Tok | New Saved | New Red. % | Baseline $ | TokWiz $ |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    b_cost_sum = 0.0
    t_cost_sum = 0.0
    has_cost = False
    for item in report.get("tasks", []):
        b_new = item["baseline"].get("uncached_tokens", item["baseline"]["total_tokens"])
        t_new = item["tokwiz"].get("uncached_tokens", item["tokwiz"]["total_tokens"])
        saved_new = item.get("net_saved_uncached_tokens", item["net_saved_tokens"])
        pct_new = item.get("net_uncached_reduction_pct", item["net_reduction_pct"])
        b_cost = item["baseline"].get("cost_usd")
        t_cost = item["tokwiz"].get("cost_usd")
        if isinstance(b_cost, (int, float)) and isinstance(t_cost, (int, float)):
            has_cost = True
            b_cost_sum += float(b_cost)
            t_cost_sum += float(t_cost)
            cost_cols = f" ${b_cost:.3f} | ${t_cost:.3f} |"
        else:
            cost_cols = " — | — |"
        lines.append(
            f"| {item['task_name']} | {b_new:,} | {t_new:,} | "
            f"{saved_new:+,} | {pct_new:.1f}% |{cost_cols}"
        )
    total_cost_cols = f" **${b_cost_sum:.3f}** | **${t_cost_sum:.3f}** |" if has_cost else " — | — |"
    b_new_tot = report.get("baseline_uncached_tokens", report["baseline_total_tokens"])
    t_new_tot = report.get("tokwiz_uncached_tokens", report["tokwiz_total_tokens"])
    saved_new_tot = report.get("net_saved_uncached_tokens", report["net_saved_tokens"])
    pct_new_tot = report.get("net_uncached_reduction_pct", report["net_reduction_pct"])
    lines.append(
        f"| **Total** | **{b_new_tot:,}** | **{t_new_tot:,}** | "
        f"**{saved_new_tot:+,}** | **{pct_new_tot:.1f}%** |{total_cost_cols}"
    )
    lines.append("")
    if has_cost and b_cost_sum > 0:
        cost_saved = b_cost_sum - t_cost_sum
        cost_pct = (cost_saved / b_cost_sum) * 100.0
        lines.append(f"- **Net Cost Saved:** `${cost_saved:+.3f}` ({cost_pct:+.1f}%)")
    lines.append(
        f"- Total Tokens (incl. 0.1x cache reads): Baseline `{report['baseline_total_tokens']:,}` vs. "
        f"TokWiz `{report['tokwiz_total_tokens']:,}`"
    )
    lines.append(
        f"- Baseline Tests Passed: `{report['baseline_tests']['passed']}` | "
        f"TokWiz Tests Passed: `{report['tokwiz_tests']['passed']}`"
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz live-ab",
        description="Run automated headless A/B tests using Claude, Codex, or Gemini CLI.",
    )
    parser.add_argument("--agent", default="claude", choices=["claude", "codex", "gemini"])
    parser.add_argument("--prompt", default="", help="Optional single custom prompt to test")
    parser.add_argument("--tasks-file", default="", help="Optional JSON file of tasks ([{id, name, prompt}])")
    parser.add_argument("--workspace-dir", default="", help="Optional persistent workspace directory")
    parser.add_argument("--timeout", type=int, default=180, help="Per-task timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Setup workspaces & verify commands without calling LLM")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument("--out", default="", help="Optional path to save JSON report")
    args = parser.parse_args(argv)

    tasks = None
    if args.prompt:
        tasks = [{"id": "custom", "name": "Custom Prompt", "prompt": args.prompt}]
    elif args.tasks_file:
        loaded = json.loads(Path(args.tasks_file).read_text(encoding="utf-8"))
        tasks = loaded if isinstance(loaded, list) else loaded.get("tasks", [])

    try:
        report = run_live_ab(
            agent=args.agent,
            tasks=tasks,
            workspace_dir=Path(args.workspace_dir) if args.workspace_dir else None,
            timeout_sec=args.timeout,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_live_ab_markdown(report))
    return 0 if (report["baseline_tests"]["passed"] and report["tokwiz_tests"]["passed"]) else 1


if __name__ == "__main__":
    sys.exit(main())
