#!/usr/bin/env python3
"""Shell Output Filter & Compressor for TokWiz (`scripts/output-filter.py`).

Strips ANSI escape sequences, progress bars, passing test noise, and lockfile
diffs while preserving failing assertions, relevant stack frames, and summaries.
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from typing import List, Optional

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
PROGRESS_LINE_RE = re.compile(
    r"^\s*(?:\d+%|\[\s*[=#>-]+\s*\]|Downloading|Installing|Resolving|Fetching|Building\s+\[|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏])\b",
    re.IGNORECASE,
)
PASS_LINE_RE = re.compile(
    r"^\s*(?:PASS|PASSED|✓|✔|ok\s+\d+|test\s+.*\.\.\.\s+ok\b|.*\s+PASSED\s*(?:\[\s*\d+%\])?$)"
)
FAIL_OR_ERR_RE = re.compile(
    r"(?:FAIL|FAILED|ERROR|Exception|Traceback|AssertionError|panic:|error\[E\d+\]|✕|✖|═══+.*FAIL|───+)",
    re.IGNORECASE,
)
SUMMARY_LINE_RE = re.compile(
    r"(?:\b\d+\s+(?:passed|failed|skipped|error|warnings?)\b|Test Suites:|Tests:|Ran\s+\d+\s+tests|test result:)",
    re.IGNORECASE,
)
LOCKFILE_DIFF_HEADER_RE = re.compile(
    r"^diff --git a/.*?((?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|Cargo\.lock|poetry\.lock|uv\.lock))\b"
)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4.0))


def strip_ansi(text: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", text)
    # Collapse carriage-return overwrites (keep final segment per line)
    out_lines: List[str] = []
    for raw_line in cleaned.splitlines():
        if "\r" in raw_line:
            segments = [s for s in raw_line.split("\r") if s.strip()]
            out_lines.append(segments[-1] if segments else "")
        else:
            out_lines.append(raw_line)
    return "\n".join(out_lines)


def _strip_lockfile_diffs(lines: List[str]) -> List[str]:
    result: List[str] = []
    skipping_lockfile = False
    skipped_name = ""
    skipped_lines = 0

    for line in lines:
        if line.startswith("diff --git "):
            if skipping_lockfile:
                result.append(f"[TokWiz Filter: omitted {skipped_lines} diff lines for {skipped_name}]")
            m = LOCKFILE_DIFF_HEADER_RE.match(line)
            if m:
                skipping_lockfile = True
                skipped_name = m.group(1)
                skipped_lines = 0
                continue
            skipping_lockfile = False
        if skipping_lockfile:
            skipped_lines += 1
            continue
        result.append(line)

    if skipping_lockfile:
        result.append(f"[TokWiz Filter: omitted {skipped_lines} diff lines for {skipped_name}]")
    return result


def filter_output(
    raw_text: str,
    max_tokens: int = 800,
    max_stack_frames: int = 8,
) -> str:
    """Compress verbose shell/test/build/git output while keeping high-signal lines."""
    if not raw_text:
        return ""

    cleaned = strip_ansi(raw_text)
    lines = _strip_lockfile_diffs(cleaned.splitlines())

    kept: List[str] = []
    omitted_pass_count = 0
    omitted_progress_count = 0
    consecutive_blanks = 0
    in_stack_trace = False
    stack_frame_count = 0
    omitted_frames = 0
    prev_line: Optional[str] = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            consecutive_blanks += 1
            if consecutive_blanks <= 1 and kept:
                kept.append("")
            continue
        consecutive_blanks = 0

        # Deduplicate consecutive identical lines
        if line == prev_line:
            continue
        prev_line = line

        # Always keep summary lines
        if SUMMARY_LINE_RE.search(stripped):
            in_stack_trace = False
            kept.append(line)
            continue

        # Omit progress bars / spinner noise
        if PROGRESS_LINE_RE.match(stripped) and not FAIL_OR_ERR_RE.search(stripped):
            omitted_progress_count += 1
            continue

        # Omit passing test lines unless they also contain error keywords
        if PASS_LINE_RE.match(stripped) and not FAIL_OR_ERR_RE.search(stripped):
            omitted_pass_count += 1
            continue

        # Stack frame capping inside tracebacks
        is_frame = stripped.startswith(("File \"", "at ", "--> "))
        if FAIL_OR_ERR_RE.search(stripped):
            in_stack_trace = True
            stack_frame_count = 0
            kept.append(line)
            continue

        if in_stack_trace and is_frame:
            stack_frame_count += 1
            if stack_frame_count > max_stack_frames:
                omitted_frames += 1
                continue

        kept.append(line)

    footer_notes: List[str] = []
    if omitted_pass_count > 0:
        footer_notes.append(f"{omitted_pass_count} passing test lines")
    if omitted_progress_count > 0:
        footer_notes.append(f"{omitted_progress_count} progress lines")
    if omitted_frames > 0:
        footer_notes.append(f"{omitted_frames} deep stack frames")

    if footer_notes:
        kept.append(f"[TokWiz Filter: omitted {', '.join(footer_notes)}]")

    # Enforce token budget keeping head + tail (errors & summaries are often at the end)
    output_text = "\n".join(kept).strip()
    if max_tokens > 0 and estimate_tokens(output_text) > max_tokens:
        char_budget = max(120, max_tokens * 4)
        head_budget = int(char_budget * 0.45)
        tail_budget = int(char_budget * 0.45)
        head = output_text[:head_budget].rstrip()
        tail = output_text[-tail_budget:].lstrip()
        output_text = f"{head}\n... [TokWiz Filter: truncated middle to fit {max_tokens} token budget] ...\n{tail}"

    return output_text


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz filter",
        description="Compress noisy test/build/git shell outputs.",
    )
    parser.add_argument("--max-tokens", type=int, default=800, help="Maximum output tokens (default: 800)")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Optional command after '--' to execute and filter")
    args = parser.parse_args(argv)

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if cmd:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        print(filter_output(combined, max_tokens=args.max_tokens))
        return proc.returncode

    raw = sys.stdin.read()
    print(filter_output(raw, max_tokens=args.max_tokens))
    return 0


if __name__ == "__main__":
    sys.exit(main())
