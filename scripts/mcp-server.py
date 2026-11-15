#!/usr/bin/env python3
"""Native Model Context Protocol (MCP) stdio server for TokWiz (`tokwiz mcp`).

Exposes `tokwiz_graph_query`, `tokwiz_slice`, `tokwiz_skeleton`, `tokwiz_predict`,
and `tokwiz_stats` as in-process JSON-RPC 2.0 tools (<5ms latency, zero shell spawn).
Supports both `Content-Length` framed stdio and newline-delimited JSON-RPC lines.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
PROTOCOL_VERSION = "2024-11-05"


def _load_script(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "tokwiz_graph_query",
        "description": (
            "Query TokWiz's local SQLite + Personalized PageRank codebase graph for exact "
            "file:Lstart-Lend symbol coordinates, callers, and callees before running grep/find."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_text": {"type": "string", "description": "Symbol name or keywords"},
                "max_tokens": {"type": "integer", "default": 400, "description": "Token budget (default 400)"},
                "with_code": {"type": "boolean", "default": False, "description": "Include AST body + caller snippets"},
                "root": {"type": "string", "default": ".", "description": "Workspace root directory"},
            },
            "required": ["query_text"],
        },
    },
    {
        "name": "tokwiz_slice",
        "description": (
            "One-shot code slice bundler: returns the matched symbol's exact AST definition lines "
            "plus direct caller call-site snippets within max_tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_text": {"type": "string", "description": "Symbol name or keywords"},
                "max_tokens": {"type": "integer", "default": 600, "description": "Token budget (default 600)"},
                "root": {"type": "string", "default": ".", "description": "Workspace root directory"},
            },
            "required": ["query_text"],
        },
    },
    {
        "name": "tokwiz_skeleton",
        "description": (
            "Extract a compact AST signature skeleton (or JSON/YAML/TOML/SQL schema skeleton) "
            "with exact line numbers before doing a ranged file read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to file to skeletonize"},
                "max_tokens": {"type": "integer", "description": "Optional max token budget"},
                "root": {"type": "string", "default": ".", "description": "Workspace root directory"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "tokwiz_predict",
        "description": (
            "Forecast multi-step workflow tokens, blast radius, and USD cost before executing a prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Prompt text to forecast"},
                "root": {"type": "string", "default": ".", "description": "Workspace root directory"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "tokwiz_stats",
        "description": "Return TokWiz token savings, Forecast-vs-Real MAPE, and cost telemetry summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "description": "Lookback window in days"},
                "root": {"type": "string", "default": ".", "description": "Workspace root directory"},
            },
        },
    },
]


def _resolve_workspace(args: Dict[str, Any], default_root: Path) -> Path:
    raw = str(args.get("root") or "").strip()
    if not raw or raw == ".":
        return default_root.resolve()
    p = Path(raw)
    return (p if p.is_absolute() else (default_root / p)).resolve()


def _execute_tool(name: str, args: Dict[str, Any], default_root: Path) -> str:
    ws_root = _resolve_workspace(args, default_root)

    if name == "tokwiz_graph_query":
        pg = _load_script("project_graph", "project-graph.py")
        graph = pg.ProjectGraph(root_dir=ws_root)
        query_text = str(args.get("query_text") or "")
        max_tokens = int(args.get("max_tokens") or 400)
        if bool(args.get("with_code", False)):
            res = graph.slice_code(query_text, max_tokens=max_tokens)
        else:
            res = graph.query(query_text, max_tokens=max_tokens)
        return res.formatted_slice

    if name == "tokwiz_slice":
        pg = _load_script("project_graph", "project-graph.py")
        graph = pg.ProjectGraph(root_dir=ws_root)
        res = graph.slice_code(
            str(args.get("query_text") or ""),
            max_tokens=int(args.get("max_tokens") or 600),
        )
        return res.formatted_slice

    if name == "tokwiz_skeleton":
        skel = _load_script("code_skeletonizer", "code-skeletonizer.py")
        raw_fp = str(args.get("file_path") or "")
        p = Path(raw_fp)
        target = p if p.is_absolute() else (ws_root / p).resolve()
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"File not found: {raw_fp}")
        max_tok = args.get("max_tokens")
        return skel.skeletonize_file(
            target,
            root_dir=ws_root,
            max_tokens=int(max_tok) if max_tok is not None else None,
        )

    if name == "tokwiz_predict":
        est = _load_script("token_estimator", "token-estimator.py")
        estimator = est.TokenEstimator(root_dir=ws_root)
        res = estimator.estimate(str(args.get("prompt") or ""))
        return estimator.render_forecast_banner(res)

    if name == "tokwiz_stats":
        tel = _load_script("telemetry", "telemetry.py")
        store = tel.TelemetryStore(ws_root)
        summary = store.get_summary(days=int(args.get("days") or 7))
        return json.dumps(summary, indent=2)

    raise ValueError(f"Unknown tool: {name}")


def handle_jsonrpc_message(
    msg: Dict[str, Any],
    default_root: Path,
) -> Optional[Dict[str, Any]]:
    """Handle a single JSON-RPC 2.0 request/notification and return response dict (or None for notifications)."""
    method = str(msg.get("method") or "")
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # Notifications have no id
    if msg_id is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tokwiz", "version": "1.0.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            text_out = _execute_tool(tool_name, arguments, default_root)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text_out}],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def _read_stdio_message(stream) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read either a Content-Length framed message or a newline-delimited JSON-RPC line."""
    while True:
        line_bytes = stream.readline()
        if not line_bytes:
            return None, "jsonl"
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
            while True:
                hdr = stream.readline()
                if not hdr or hdr.strip() == b"":
                    break
            body = stream.read(length).decode("utf-8", errors="replace")
            return json.loads(body), "lsp"
        return json.loads(line), "jsonl"


def _write_stdio_message(stream, payload: Dict[str, Any], framing: str) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if framing == "lsp":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stream.write(header + encoded)
    else:
        stream.write(encoded + b"\n")
    stream.flush()


def serve_stdio(default_root: Path) -> int:
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer
    while True:
        try:
            msg, framing = _read_stdio_message(stdin_bin)
        except Exception:
            continue
        if msg is None:
            break
        resp = handle_jsonrpc_message(msg, default_root)
        if resp is not None:
            _write_stdio_message(stdout_bin, resp, framing)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz mcp",
        description="Start the TokWiz native MCP stdio server.",
    )
    parser.add_argument("--root", default=".", help="Default workspace root directory")
    args = parser.parse_args(argv)
    return serve_stdio(Path(args.root).resolve())


if __name__ == "__main__":
    sys.exit(main())
