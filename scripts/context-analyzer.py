#!/usr/bin/env python3
"""Context & Transcript Analyzer for TokWiz (`scripts/context-analyzer.py`).

Audits agent session transcripts (`.jsonl`) for duplicate file reads, redundant
searches, unbounded tool outputs, and KV-cache prefix stability.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4.0))


def analyze_transcript(lines: List[str]) -> Dict[str, object]:
    seen_reads: Set[str] = set()
    duplicate_reads: List[str] = []
    seen_searches: Set[str] = set()
    duplicate_searches: List[str] = []
    large_tool_outputs = 0
    total_estimated_tokens = 0

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        total_estimated_tokens += estimate_tokens(raw)
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        content = str(obj.get("content") or "")
        if estimate_tokens(content) > 1200:
            large_tool_outputs += 1

        tool_calls = obj.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tname = str(tc.get("name") or tc.get("tool_name") or "").lower()
                args = tc.get("arguments") or tc.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    continue

                if "read" in tname:
                    fpath = str(args.get("Path") or args.get("file_path") or args.get("path") or "")
                    rkey = f"{fpath}:{args.get('StartLine', '')}-{args.get('EndLine', '')}"
                    if fpath:
                        if rkey in seen_reads:
                            duplicate_reads.append(rkey)
                        else:
                            seen_reads.add(rkey)
                elif tname in ("grep", "search", "find"):
                    q = str(args.get("query") or args.get("pattern") or "")
                    if q:
                        if q in seen_searches:
                            duplicate_searches.append(q)
                        else:
                            seen_searches.add(q)

    return {
        "total_estimated_tokens": total_estimated_tokens,
        "unique_file_reads": len(seen_reads),
        "duplicate_file_reads": duplicate_reads,
        "duplicate_searches": duplicate_searches,
        "unbounded_tool_outputs": large_tool_outputs,
        "kv_cache_hygiene_ok": len(duplicate_reads) == 0 and large_tool_outputs <= 1,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tokwiz analyze-context", description="Audit session transcript efficiency.")
    parser.add_argument("transcript", help="Path to transcript .jsonl file")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args(argv)

    path = Path(args.transcript)
    if not path.exists():
        print(f"Error: Transcript not found: {path}", file=sys.stderr)
        return 1

    report = analyze_transcript(path.read_text(encoding="utf-8", errors="replace").splitlines())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"[TokWiz Context Audit] Est. Tokens: {report['total_estimated_tokens']:,} | "
            f"Duplicate Reads: {len(report['duplicate_file_reads'])} | "
            f"Unbounded Tool Outputs: {report['unbounded_tool_outputs']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
