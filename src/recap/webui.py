"""Loopback-only browser interface. Reports exist only in process memory."""

from __future__ import annotations

import json
import secrets
import threading
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import (
    DEFAULT_TEXT,
    Config,
    config_text_with_values,
    config_values,
    load_config,
    parse_config,
    save_config_text,
    save_config_values,
)
from .model import Report
from .report import RecapService, render_report
from .sources import discover_repos

STATIC_DIR = Path(__file__).parent / "web"


class State:
    def __init__(
        self,
        config: Config,
        path: Path,
        day: date,
        *,
        service_factory: Callable[[Config], RecapService] | None = None,
        demo_mode: bool = False,
    ):
        self.config = config
        self.path = path
        self.day = day
        self.service_factory = service_factory or RecapService
        self.demo_mode = demo_mode
        self.report: Report | None = None
        self.run_config = config
        self.run_service = self.service_factory(config)
        self.report_id = ""
        self.posted: set[str] = set()
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()


def make_server(
    config: Config,
    path: Path,
    day: date,
    *,
    service_factory: Callable[[Config], RecapService] | None = None,
    demo_mode: bool = False,
) -> tuple[ThreadingHTTPServer, State]:
    state = State(
        config, path, day, service_factory=service_factory, demo_mode=demo_mode
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return  # Never log work content or request metadata.

        def _headers(self, code: int, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()

        def _json(self, code: int, value: dict) -> None:
            encoded = json.dumps(value, ensure_ascii=False).encode()
            self._headers(code, "application/json; charset=utf-8")
            self.wfile.write(encoded)

        def _read(self) -> dict:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1_000_000:
                raise ValueError("Request is too large")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise TypeError("Expected a JSON object")
            return value

        def _allowed(self) -> bool:
            if self.headers.get("X-Recap-Nonce") != state.csrf:
                return False
            origin = self.headers.get("Origin")
            return not origin or origin == f"http://127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if self.path == "/":
                page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
                page = page.replace("__CSRF_TOKEN__", escape(state.csrf, quote=True))
                page = page.replace(
                    "__RECAP_DEMO__", "true" if state.demo_mode else "false"
                )
                encoded = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(encoded)
                return
            if self.path in (
                "/assets/app.js",
                "/assets/app.css",
                "/assets/theme.js",
                "/assets/favicon.svg",
            ):
                name = self.path.rsplit("/", 1)[1]
                content_type = {
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                }[Path(name).suffix]
                self._headers(200, content_type)
                self.wfile.write((STATIC_DIR / name).read_bytes())
                return
            if not self._allowed():
                self._json(403, {"error": "Invalid local session"})
                return
            try:
                if self.path == "/api/settings":
                    text = (
                        state.path.read_text(encoding="utf-8")
                        if state.path.exists()
                        else DEFAULT_TEXT
                    )
                    service = state.service_factory(state.config)
                    self._json(
                        200,
                        {
                            "text": text,
                            "values": config_values(state.config),
                            "path": str(state.path),
                            "ai_endpoint": service.ai.endpoint_url,
                            "ai_model": state.config.ai_model,
                            "day": state.day.isoformat(),
                        },
                    )
                elif self.path == "/api/models":
                    self._json(
                        200,
                        {
                            "models": state.service_factory(
                                state.config
                            ).ai.list_models()
                        },
                    )
                elif self.path == "/api/repos":
                    warnings: list[str] = []
                    repos = discover_repos(state.config, warnings)
                    self._json(
                        200,
                        {
                            "roots": [str(root) for root in state.config.repo_roots],
                            "repos": [str(repo) for repo in repos],
                            "warnings": warnings,
                        },
                    )
                else:
                    self._json(404, {"error": "Not found"})
            except Exception as exc:  # noqa: BLE001 - return a JSON error instead of dropping the local connection
                self._json(400, {"error": str(exc)})

        def do_POST(self):
            if not self._allowed():
                self._json(403, {"error": "Invalid local session"})
                return
            try:
                data = self._read()
                if self.path == "/api/settings/convert":
                    if isinstance(data.get("values"), dict):
                        current = (
                            state.path.read_text(encoding="utf-8")
                            if state.path.exists()
                            else DEFAULT_TEXT
                        )
                        text = config_text_with_values(data["values"], current)
                        converted = parse_config(text)
                    else:
                        text = str(data.get("text", ""))
                        converted = parse_config(text)
                    self._json(200, {"text": text, "values": config_values(converted)})
                    return
                if self.path == "/api/settings":
                    if isinstance(data.get("values"), dict):
                        save_config_values(data["values"], state.path)
                    else:
                        save_config_text(str(data.get("text", "")), state.path)
                    with state.lock:
                        state.config = load_config(state.path)
                        state.report = None
                        state.report_id = ""
                        service = state.service_factory(state.config)
                        self._json(
                            200,
                            {
                                "saved": True,
                                "text": state.path.read_text(encoding="utf-8"),
                                "values": config_values(state.config),
                                "ai_endpoint": service.ai.endpoint_url,
                                "ai_model": state.config.ai_model,
                            },
                        )
                elif self.path == "/api/report":
                    selected = date.fromisoformat(str(data.get("date", "")))
                    model = str(data.get("model", "")).strip()
                    with state.lock:
                        run_config = replace(
                            state.config, ai_model=model or state.config.ai_model
                        )
                    run_service = state.service_factory(run_config)
                    report = run_service.build(selected)
                    with state.lock:
                        state.report = report
                        state.run_config = run_config
                        state.run_service = run_service
                        state.report_id = uuid.uuid4().hex
                        state.posted.clear()
                        response = {
                            "report": report.to_dict(),
                            "report_id": state.report_id,
                            "posted": [],
                        }
                    self._json(200, response)
                elif self.path == "/api/reassign":
                    with state.lock:
                        if not state.report or data.get("report_id") != state.report_id:
                            raise ValueError("Report expired; generate it again")
                        key = str(data.get("issue_key", ""))
                        include_in_daily_update = data.get(
                            "include_in_daily_update", True
                        )
                        if not isinstance(include_in_daily_update, bool):
                            raise TypeError("include_in_daily_update must be a boolean")
                        if key and not include_in_daily_update:
                            raise ValueError(
                                "An excluded activity cannot be assigned to a Jira issue"
                            )
                        if key and key not in {
                            issue.key for issue in state.report.issues
                        }:
                            raise ValueError(
                                "Issue is not an active assigned candidate"
                            )
                        item = next(
                            (
                                item
                                for item in state.report.items
                                if item.id == data.get("item_id")
                            ),
                            None,
                        )
                        if item is None:
                            raise ValueError("Unknown activity")
                        item.issue_key = key
                        item.include_in_daily_update = include_in_daily_update
                        render_report(state.report, state.run_config)
                        response = {
                            "report": state.report.to_dict(),
                            "report_id": state.report_id,
                            "posted": sorted(state.posted),
                        }
                    self._json(200, response)
                elif self.path == "/api/post":
                    key = str(data.get("issue_key", ""))
                    comment = str(data.get("text", ""))
                    with state.lock:
                        if not state.report or data.get("report_id") != state.report_id:
                            raise ValueError("Report expired; generate it again")
                        if (
                            data.get("approved") is not True
                            or key not in state.report.comments
                            or not comment.strip()
                        ):
                            raise ValueError(
                                "Explicit approval of a nonempty ticket comment is required"
                            )
                        if key in state.posted:
                            raise ValueError("Comment already posted in this run")
                        state.posted.add(key)
                    try:
                        state.run_service.issues.post_comment(key, comment)
                    except (
                        Exception
                    ):  # Always release the in-memory posting guard on failure.
                        with state.lock:
                            state.posted.discard(key)
                        raise
                    response = {"posted": key}
                    if state.demo_mode:
                        response["simulated"] = True
                    self._json(200, response)
                else:
                    self._json(404, {"error": "Not found"})
            except Exception as exc:  # noqa: BLE001 - return a JSON error instead of dropping the local connection
                self._json(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return server, state


def serve(
    config: Config,
    path: Path,
    day: date,
    *,
    service_factory: Callable[[Config], RecapService] | None = None,
    demo_mode: bool = False,
) -> None:
    server, _state = make_server(
        config,
        path,
        day,
        service_factory=service_factory,
        demo_mode=demo_mode,
    )
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Recap UI: {url}\nPress Ctrl+C to stop; this discards the in-memory report.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
