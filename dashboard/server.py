#!/usr/bin/env python3
"""Local Analytics Web Dashboard Server for TokWiz (`dashboard/server.py`).

Serves interactive single-page analytics (`dashboard/index.html`) and JSON
API (`/api/stats`) from `.tokwiz/telemetry.db`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

_DASHBOARD_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DASHBOARD_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _safe_resolve_root(root_arg: Path | str = ".") -> Path:
    """Resolve workspace root safely even if the shell's CWD inode was recreated."""
    try:
        cwd = Path(os.getcwd()).resolve()
    except FileNotFoundError:
        pwd_env = os.environ.get("PWD")
        if pwd_env and os.path.isdir(pwd_env):
            os.chdir(pwd_env)
            cwd = Path(pwd_env).resolve()
        else:
            os.chdir(str(_REPO_ROOT))
            cwd = _REPO_ROOT
    s = str(root_arg)
    if not s or s == ".":
        resolved = cwd
    else:
        p = Path(s)
        resolved = p.resolve() if p.is_absolute() else (cwd / p).resolve()

    # If launched from the parent tokwiz repo root which has no local telemetry yet,
    # and demo/workspace/project-tokwiz has active telemetry/forecasts, use project-tokwiz.
    if resolved == _REPO_ROOT and not (resolved / ".tokwiz" / "forecasts.jsonl").exists():
        demo_tokwiz = _REPO_ROOT / "demo" / "workspace" / "project-tokwiz"
        if (demo_tokwiz / ".tokwiz").exists():
            return demo_tokwiz
    return resolved


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

_TEL_SPEC = importlib.util.spec_from_file_location("telemetry", _SCRIPTS_DIR / "telemetry.py")
assert _TEL_SPEC and _TEL_SPEC.loader
_tel_mod = importlib.util.module_from_spec(_TEL_SPEC)
sys.modules["telemetry"] = _tel_mod
_TEL_SPEC.loader.exec_module(_tel_mod)

_GRAPH_SPEC = importlib.util.spec_from_file_location("project_graph", _SCRIPTS_DIR / "project-graph.py")
assert _GRAPH_SPEC and _GRAPH_SPEC.loader
_graph_mod = importlib.util.module_from_spec(_GRAPH_SPEC)
sys.modules["project_graph"] = _graph_mod
_GRAPH_SPEC.loader.exec_module(_graph_mod)

TelemetryStore = _tel_mod.TelemetryStore
ProjectGraph = _graph_mod.ProjectGraph


def create_handler(workspace_root: Path):
    index_path = _DASHBOARD_DIR / "index.html"
    active_root = _safe_resolve_root(workspace_root)

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/stats":
                qs = parse_qs(parsed.query)
                days_param = qs.get("days", [None])[0]
                days = int(days_param) if days_param and str(days_param).isdigit() else None
                store = TelemetryStore(root_dir=active_root)
                summary = store.get_summary(days=days, limit=100)
                summary["workspace_root"] = str(active_root)
                summary["workspace_name"] = active_root.name
                body = json.dumps(summary).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/graph":
                qs = parse_qs(parsed.query)
                max_n_param = qs.get("max_nodes", ["120"])[0]
                max_nodes = int(max_n_param) if str(max_n_param).isdigit() else 120
                graph = ProjectGraph(root_dir=active_root)
                graph_data = graph.export_graph_data(max_nodes=max_nodes, auto_build=True)
                body = json.dumps(graph_data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path in ("/", "/index.html"):
                html_bytes = index_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    return DashboardRequestHandler


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokwiz dashboard",
        description="Launch local web analytics dashboard for TokWiz.",
    )
    parser.add_argument("--port", type=int, default=8420, help="HTTP port (default: 8420)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args(argv)

    workspace_root = _safe_resolve_root(args.root)
    handler_cls = create_handler(workspace_root)
    try:
        server = ReusableHTTPServer((args.host, args.port), handler_cls)
    except OSError:
        # If an older tokwiz dashboard process is holding the port, terminate it and re-bind
        try:
            out = subprocess.check_output(["lsof", "-ti", f"tcp:{args.port}"], text=True)
            for pid_str in out.splitlines():
                pid = int(pid_str.strip())
                if pid != os.getpid():
                    os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        server = ReusableHTTPServer((args.host, args.port), handler_cls)
    url = f"http://{args.host}:{args.port}"
    print(f"[TokWiz Dashboard] Serving analytics at {url} (workspace: {workspace_root})")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
