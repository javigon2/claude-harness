"""The office's HTTP server: loopback only, stdlib only.

Read-only by default (every POST answers 405). Given an `Office` it also serves the planner
POST endpoints, the chat POST endpoints (POST /api/chat/{start,message,close}), the run
POST endpoint (POST /api/run/resume) and the Orchestrator POST endpoints
(POST /api/orchestrator/{message,new}), each behind the Host, Origin and token checks, a
JSON-only body of at most 64 KB, and a strict per-route body schema."""

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from harness.events import EVENTS_FILE
from harness.planner import PROJECT_PLAN_FILE
from harness.state import STATE_FILE, StateError, run_dir
from harness.ui.chat import CHAT_TIERS, ChatConflict
from harness.ui.markdown import parse_markdown
from harness.ui.office import (
    MAX_TEXT_CHARS,
    Office,
    OfficeConflict,
    office_view_from_log,
    status_of,
)
from harness.ui.orchestrator import OrchestratorConflict
from harness.ui.view import build_view, list_runs

STATIC_DIR: Path = Path(__file__).parent / "static"
HEARTBEAT_S = 15.0
MAX_BODY_BYTES = 65536
DRAIN_LIMIT = 1 << 20

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
_VIEW_ERRORS = (FileNotFoundError, StateError, OSError, ValueError)

_TOKEN_META = '<meta name="harness-token" content="">'
_MODE_META = '<meta name="harness-mode" content="read-only">'

POST_ROUTES = ("start", "message", "implement", "next-stage")
CHAT_ROUTES = ("start", "message", "close")
RUN_ROUTES = ("resume",)
ORCH_ROUTES = ("message", "new")
ROUTE_CHOICES = ("auto", "quick", "project")


def render_page(token: str, mode: str) -> bytes:
    """index.html with the per-server token and the mode ("interactive" or "read-only")
    filled into its two meta tags."""
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    page = page.replace(_TOKEN_META, f'<meta name="harness-token" content="{token}">')
    page = page.replace(_MODE_META, f'<meta name="harness-mode" content="{mode}">')
    return page.encode("utf-8")


class _BadBody(ValueError):
    """The request body does not fit the route's schema (answered with 400)."""


def _text_field(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _BadBody(f"{key} must be a non-empty string")
    if len(value) > MAX_TEXT_CHARS:
        raise _BadBody(f"{key} must be at most {MAX_TEXT_CHARS} characters")
    return value


def _check_body(route: str, body: object) -> dict:
    """The body of a POST to /api/planner/<route> or /api/run/<route>, or _BadBody. Exactly
    the allowed keys: start {description}, {description, route} or {from_draft: true};
    message {text}; implement, next-stage and resume {}."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route == "start":
        if keys == {"description", "route"}:
            _text_field(body, "description")
            if not isinstance(body["route"], str) or body["route"] not in ROUTE_CHOICES:
                raise _BadBody("route must be auto, quick or project")
        elif keys == {"description"}:
            _text_field(body, "description")
        elif keys == {"from_draft"}:
            if body["from_draft"] is not True:
                raise _BadBody("from_draft must be true")
        else:
            raise _BadBody("send exactly one of description or from_draft")
    elif route == "message":
        if keys != {"text"}:
            raise _BadBody("send exactly: text")
        _text_field(body, "text")
    elif keys:
        raise _BadBody("this endpoint takes no fields")
    return body


def _id_field(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise _BadBody(f"{key} must be a non-empty string")
    return value


def _check_chat_body(route: str, body: object) -> dict:
    """The body of a POST to /api/chat/<route>, or _BadBody. Exactly the allowed keys:
    start {tier, text} or {tier, text, run_id, task_id}; message {chat_id, text};
    close {chat_id}."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route == "start":
        if keys not in ({"tier", "text"}, {"tier", "text", "run_id", "task_id"}):
            raise _BadBody("send exactly: tier, text (and optionally run_id, task_id)")
        if not isinstance(body["tier"], str) or body["tier"] not in CHAT_TIERS:
            raise _BadBody("tier must be haiku or sonnet")
        _text_field(body, "text")
        for key in ("run_id", "task_id"):
            if key in body:
                _id_field(body, key)
    elif route == "message":
        if keys != {"chat_id", "text"}:
            raise _BadBody("send exactly: chat_id, text")
        _id_field(body, "chat_id")
        _text_field(body, "text")
    elif route == "close":
        if keys != {"chat_id"}:
            raise _BadBody("send exactly: chat_id")
        _id_field(body, "chat_id")
    return body


def _check_orch_body(route: str, body: object) -> dict:
    """The body of a POST to /api/orchestrator/<route>, or _BadBody. Exactly the allowed
    keys: message {text}; new {}."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route == "message":
        if keys != {"text"}:
            raise _BadBody("send exactly: text")
        _text_field(body, "text")
    elif keys:
        raise _BadBody("this endpoint takes no fields")
    return body


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with daemon request threads and a `stopping` event that
    shutdown() sets first, so open SSE streams end."""

    daemon_threads = True
    stopping: threading.Event

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self.stopping = threading.Event()
        super().__init__(address, handler)

    def shutdown(self) -> None:
        """Set self.stopping, then ThreadingHTTPServer.shutdown()."""
        self.stopping.set()
        super().shutdown()


def _size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return -1


def _mtime(path: Path) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return -1


def _signature(repo: Path, run_id: str) -> tuple[int, int]:
    d = run_dir(repo, run_id)
    return (_size(d / EVENTS_FILE), _mtime(d / STATE_FILE))


def make_handler(
    repo: Path,
    poll_interval: float = 0.5,
    heartbeat_s: float = HEARTBEAT_S,
    office: Office | None = None,
    token: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    """A request-handler class bound to `repo`. GET routes `/`, `/api/runs`,
    `/api/run/<run_id>`, `/api/stream/<run_id>`, `/api/office`, `/api/plan`. Without an
    `office` every other method answers 405 (read-only). With one, POST
    `/api/planner/{start,message,implement,next-stage}`, POST `/api/chat/{start,message,close}`,
    POST `/api/run/resume` and POST `/api/orchestrator/{message,new}` are served (`token`
    guards them; one is made if not given) and every other method still answers 405. Request
    logging is silenced."""
    interactive = office is not None
    secret = (token or secrets.token_urlsafe(32)) if interactive else ""
    mode = "interactive" if interactive else "read-only"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str = "application/json; charset=utf-8",
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: object, extra: dict[str, str] | None = None) -> None:
            self._send(code, json.dumps(obj).encode(), extra=extra)

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _not_get(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "bad host"})
                return
            if interactive:
                self._json(405, {"error": "method not allowed"}, extra={"Allow": "GET, POST"})
            else:
                self._json(405, {"error": "read-only"}, extra={"Allow": "GET"})

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_get

        def do_POST(self) -> None:
            if office is None:
                self._not_get()
                return
            try:
                self._post(office)
            except _CLIENT_GONE:
                return

        def _reject(self, code: int, message: str) -> None:
            """Answer with an error, then discard the request body (up to DRAIN_LIMIT) so the
            client can finish sending and read the answer."""
            self._json(code, {"error": message})
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length") or 0)
                self.connection.settimeout(2)
                remaining = min(max(length, 0), DRAIN_LIMIT)
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (ValueError, OSError):
                pass

        def _origin_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Origin") in (
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            )

        def _token_ok(self) -> bool:
            sent = self.headers.get("X-Harness-Token") or ""
            return secrets.compare_digest(sent.encode("utf-8"), secret.encode("utf-8"))

        def _post(self, office: Office) -> None:
            """Every safety rule in order; a request failing any of them changes nothing."""
            if not self._host_ok():
                self._reject(403, "bad host")
                return
            path = urlsplit(self.path).path.rstrip("/")
            kind, route = "", ""
            if path.startswith("/api/planner/"):
                kind, route = "planner", path[len("/api/planner/") :]
                known = route in POST_ROUTES
            elif path.startswith("/api/chat/"):
                kind, route = "chat", path[len("/api/chat/") :]
                known = route in CHAT_ROUTES
            elif path.startswith("/api/run/"):
                kind, route = "run", path[len("/api/run/") :]
                known = route in RUN_ROUTES
            elif path.startswith("/api/orchestrator/"):
                kind, route = "orchestrator", path[len("/api/orchestrator/") :]
                known = route in ORCH_ROUTES
            else:
                known = False
            if not known:
                self._reject(404, "not found")
                return
            if not self._origin_ok():
                self._reject(403, "bad origin")
                return
            if not self._token_ok():
                self._reject(403, "bad token")
                return
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._reject(415, "content type must be application/json")
                return
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._reject(411, "content length required")
                return
            try:
                length = int(raw_length)
            except ValueError:
                self._reject(400, "bad content length")
                return
            if length < 0:
                self._reject(400, "bad content length")
                return
            if length > MAX_BODY_BYTES:
                self._reject(413, "body too large")
                return
            self.connection.settimeout(10)
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if kind == "chat":
                    body = _check_chat_body(route, parsed)
                elif kind == "orchestrator":
                    body = _check_orch_body(route, parsed)
                else:
                    body = _check_body(route, parsed)
            except (ValueError, UnicodeDecodeError) as err:
                self._json(400, {"error": str(err) if isinstance(err, _BadBody) else "bad JSON"})
                return
            try:
                if kind == "chat":
                    if route == "start":
                        office.chats.start(
                            body["tier"],
                            body["text"],
                            run_id=body.get("run_id"),
                            task_id=body.get("task_id"),
                        )
                    elif route == "message":
                        office.chats.message(body["chat_id"], body["text"])
                    else:
                        office.chats.close_chat(body["chat_id"])
                elif kind == "run":
                    office.resume_run()
                elif kind == "orchestrator":
                    if route == "message":
                        office.orchestrator.message(body["text"])
                    else:
                        office.orchestrator.new_thread()
                elif route == "start":
                    office.start(
                        description=body.get("description"),
                        from_draft=body.get("from_draft") is True,
                        route=body.get("route", "project"),
                    )
                elif route == "message":
                    office.message(body["text"])
                elif route == "implement":
                    office.implement()
                else:
                    office.next_stage()
            except (OfficeConflict, ChatConflict, OrchestratorConflict) as err:
                self._json(409, {"error": str(err)})
                return
            self._json(200, {"ok": True, "office": office.view().model_dump(mode="json")})

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "bad host"})
                return
            try:
                self._route()
            except _CLIENT_GONE:
                return

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if len(path) > 1:
                path = path.rstrip("/")
            if path == "/":
                try:
                    body = render_page(secret, mode)
                except OSError:
                    self._json(404, {"error": "not found"})
                    return
                self._send(200, body, ctype="text/html; charset=utf-8")
                return
            if path == "/api/office":
                view = office.view() if office is not None else office_view_from_log(repo)
                self._json(200, view.model_dump(mode="json"))
                return
            if path == "/api/plan":
                self._plan()
                return
            if path == "/api/runs":
                try:
                    runs = list_runs(repo)
                except (StateError, OSError, ValueError) as err:
                    self._json(500, {"error": str(err)})
                    return
                self._json(200, {"runs": runs, "latest": runs[0] if runs else None})
                return
            for prefix, stream in (("/api/run/", False), ("/api/stream/", True)):
                if path.startswith(prefix):
                    run_id = unquote(path[len(prefix) :])
                    try:
                        known = run_id in list_runs(repo)
                    except (StateError, OSError, ValueError) as err:
                        self._json(500, {"error": str(err)})
                        return
                    if not known:
                        self._json(404, {"error": "unknown run"})
                        return
                    if stream:
                        self._stream(run_id)
                    else:
                        self._run(run_id)
                    return
            self._json(404, {"error": "not found"})

        def _plan(self) -> None:
            """The quick plan's plan.md while the office has one (kind "quick"), else
            PROJECT_PLAN_FILE (kind "project"). Answers 500 on any other OSError."""
            quick_plan = office.view().quick_plan if office is not None else None
            if quick_plan is not None:
                self._quick_plan(quick_plan)
                return
            try:
                text = (repo / PROJECT_PLAN_FILE).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                self._json(200, {"exists": False, "status": None, "blocks": [], "kind": "project"})
                return
            except OSError as err:
                self._json(500, {"error": str(err)})
                return
            self._json(
                200,
                {
                    "exists": True,
                    "status": status_of(text),
                    "blocks": parse_markdown(text),
                    "kind": "project",
                },
            )

        def _quick_plan(self, quick_plan: str) -> None:
            """plan.md beside the quick plan.json at the repo-relative `quick_plan`."""
            path = repo / PurePosixPath(quick_plan).parent / "plan.md"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                self._json(200, {"exists": False, "status": None, "blocks": [], "kind": "quick"})
                return
            except OSError as err:
                self._json(500, {"error": str(err)})
                return
            self._json(
                200,
                {"exists": True, "status": None, "blocks": parse_markdown(text), "kind": "quick"},
            )

        def _run(self, run_id: str) -> None:
            try:
                body = build_view(repo, run_id).model_dump_json().encode()
            except FileNotFoundError:
                self._json(404, {"error": "unknown run"})
                return
            except (StateError, OSError, ValueError) as err:
                self._json(500, {"error": str(err)})
                return
            self._send(200, body)

        def _snapshot(self, run_id: str, sig: tuple[int, int]) -> bytes:
            try:
                return b"data: " + build_view(repo, run_id).model_dump_json().encode() + b"\n\n"
            except _VIEW_ERRORS as err:
                return (
                    b"event: problem\ndata: " + json.dumps({"error": str(err)}).encode() + b"\n\n"
                )

        def _write(self, data: bytes) -> None:
            self.wfile.write(data)
            self.wfile.flush()

        def _stream(self, run_id: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            stopping = getattr(self.server, "stopping", None)
            sig = _signature(repo, run_id)
            self._write(self._snapshot(run_id, sig))
            last_write = time.monotonic()
            while True:
                time.sleep(poll_interval)
                if stopping is not None and stopping.is_set():
                    return
                new = _signature(repo, run_id)
                if new != sig:
                    sig = new
                    self._write(self._snapshot(run_id, sig))
                    last_write = time.monotonic()
                elif time.monotonic() - last_write >= heartbeat_s:
                    self._write(b": keepalive\n\n")
                    self._write(
                        self._snapshot(run_id, sig)
                    )  # idle_minutes grows with no file change
                    last_write = time.monotonic()

        def handle(self) -> None:
            try:
                super().handle()
            except _CLIENT_GONE:
                return

    return Handler


def make_server(
    repo: Path,
    port: int,
    poll_interval: float = 0.5,
    heartbeat_s: float = HEARTBEAT_S,
    office: Office | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """A server bound to ("127.0.0.1", port); port 0 picks a free port. An OSError from the
    bind propagates. With an `office` the POST endpoints are on (see make_handler). The caller
    runs serve_forever() and later shutdown() + server_close()."""
    return _Server(
        ("127.0.0.1", port), make_handler(repo, poll_interval, heartbeat_s, office, token)
    )
