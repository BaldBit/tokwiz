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
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
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
    # and the external A/B demo workspace has active telemetry/forecasts, use project-tokwiz.
    if resolved == _REPO_ROOT and not (resolved / ".tokwiz" / "forecasts.jsonl").exists():
        for candidate in (
            _REPO_ROOT.parent / "tokwiz-ab-demo" / "project-tokwiz",
            _REPO_ROOT / "demo" / "workspace" / "project-tokwiz",
        ):
            if (candidate / ".tokwiz").exists():
                return candidate
    return resolved


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

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


def _list_registered_workspaces(active_root: Path) -> List[ dict ]:
    reg_env = os.environ.get("TOKWIZ_WORKSPACES_FILE")
    reg_file = Path(reg_env) if reg_env else (Path.home() / ".tokwiz" / "workspaces.json")
    seen = {str(active_root)}
    result = [{"name": active_root.name, "path": str(active_root)}]
    if reg_file.exists():
        try:
            raw = json.loads(reg_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("path"):
                        p = Path(str(item["path"]))
                        if str(p) not in seen and p.is_dir():
                            seen.add(str(p))
                            result.append({"name": str(item.get("name") or p.name), "path": str(p)})
        except Exception:
            pass
    return result


def _resolve_requested_workspace(qs: dict, active_root: Path) -> Path:
    ws_param = qs.get("workspace", [None])[0]
    if not ws_param:
        return active_root
    allowed = {w["path"] for w in _list_registered_workspaces(active_root)}
    cand = str(Path(str(ws_param)).resolve())
    if cand in allowed or (Path(cand) / ".tokwiz").is_dir():
        return Path(cand)
    return active_root


def _clear_workspace_telemetry(ws_root: Path) -> None:
    import time

    tw = ws_root / ".tokwiz"
    if not tw.is_dir():
        return
    for fname in (
        "telemetry.db",
        "telemetry.db-wal",
        "telemetry.db-shm",
        "forecasts.jsonl",
        "active-forecast.json",
        "guardrail-events.jsonl",
        "session-reads.json",
    ):
        (tw / fname).unlink(missing_ok=True)
    cfg_file = tw / "config.json"
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["installed_at"] = time.time()
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def create_handler(workspace_root: Path):
    index_path = _DASHBOARD_DIR / "index.html"
    active_root = _safe_resolve_root(workspace_root)

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/clear":
                qs = parse_qs(parsed.query)
                target_ws = _resolve_requested_workspace(qs, active_root)
                _clear_workspace_telemetry(target_ws)
                self._send_json({"cleared": True, "workspace_root": str(target_ws), "workspace_name": target_ws.name})
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/workspaces":
                self._send_json({
                    "active_root": str(active_root),
                    "active_name": active_root.name,
                    "workspaces": _list_registered_workspaces(active_root),
                })
                return

            if parsed.path == "/api/clear":
                qs = parse_qs(parsed.query)
                target_ws = _resolve_requested_workspace(qs, active_root)
                _clear_workspace_telemetry(target_ws)
                self._send_json({"cleared": True, "workspace_root": str(target_ws), "workspace_name": target_ws.name})
                return

            if parsed.path == "/api/stats":
                qs = parse_qs(parsed.query)
                target_ws = _resolve_requested_workspace(qs, active_root)
                days_param = qs.get("days", [None])[0]
                days = int(days_param) if days_param and str(days_param).isdigit() else None
                store = TelemetryStore(root_dir=target_ws)
                summary = store.get_summary(days=days, limit=100)
                summary["workspace_root"] = str(target_ws)
                summary["workspace_name"] = target_ws.name
                self._send_json(summary)
                return

            if parsed.path == "/api/graph":
                qs = parse_qs(parsed.query)
                target_ws = _resolve_requested_workspace(qs, active_root)
                max_n_param = qs.get("max_nodes", ["120"])[0]
                max_nodes = int(max_n_param) if str(max_n_param).isdigit() else 120
                expand_id = qs.get("expand", [None])[0]
                graph = ProjectGraph(root_dir=target_ws)
                graph_data = graph.export_graph_data(
                    max_nodes=max_nodes,
                    auto_build=(expand_id is None),
                    expand_id=expand_id,
                )
                self._send_json(graph_data)
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
