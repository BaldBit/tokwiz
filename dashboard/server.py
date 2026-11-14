#!/usr/bin/env python3
"""Local Analytics Web Dashboard Server for TokWiz (`dashboard/server.py`).

Serves interactive single-page analytics (`dashboard/index.html`) and JSON
API (`/api/stats`) from `.tokwiz/telemetry.db`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

_DASHBOARD_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DASHBOARD_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

_TEL_SPEC = importlib.util.spec_from_file_location("telemetry", _SCRIPTS_DIR / "telemetry.py")
assert _TEL_SPEC and _TEL_SPEC.loader
_tel_mod = importlib.util.module_from_spec(_TEL_SPEC)
sys.modules["telemetry"] = _tel_mod
_TEL_SPEC.loader.exec_module(_tel_mod)

TelemetryStore = _tel_mod.TelemetryStore


def create_handler(workspace_root: Path):
    index_path = _DASHBOARD_DIR / "index.html"

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/stats":
                qs = parse_qs(parsed.query)
                days_param = qs.get("days", [None])[0]
                days = int(days_param) if days_param and str(days_param).isdigit() else None
                store = TelemetryStore(root_dir=workspace_root)
                summary = store.get_summary(days=days, limit=100)
                body = json.dumps(summary).encode("utf-8")
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

    workspace_root = Path(args.root).resolve()
    handler_cls = create_handler(workspace_root)
    server = HTTPServer((args.host, args.port), handler_cls)
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
