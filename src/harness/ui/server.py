"""The office's HTTP server: loopback only, stdlib only.

GET /api/upload/<id> serves a stored image (also read-only).
Read-only by default (every POST answers 405). Given an `Office` it also serves the planner
POST endpoints, the chat POST endpoints (POST /api/chat/{start,message,close}), the run
POST endpoints (POST /api/run/{resume,stop,launch,options}) and the Orchestrator POST endpoints
(POST /api/orchestrator/{message,new}) and POST /api/upload (raw image bytes), each behind
the Host, Origin and token checks, a JSON-only body of at most 64 KB (the upload: raw image
bytes, at most 8 MB), and a strict per-route body schema. The message routes (planner
start with a description, planner message, chat start, chat message, orchestrator message)
take an optional `images` list of uploaded image ids (400 when malformed or unknown).

Given a `Host` instead (the launcher) the project is not fixed: each request works on the
Host's current project and Office, answers 409 `NO_PROJECT` when none is open, and the
project endpoints (GET /api/project, POST /api/project/{browse,open,init-preview,init,
pull-origin}, POST /api/server/restart) are served. Without a Host nothing changes."""

import json
import math
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from harness.events import EVENTS_FILE
from harness.planner import PROJECT_PLAN_FILE
from harness.report import report_data, report_text, status_text
from harness.state import STATE_FILE, StateError, load_state, run_dir
from harness.ui.chat import CHAT_TIERS, ChatConflict
from harness.ui.git_routes import GIT_POST_ROUTES, BadGitBody, check_git_body, git_get, git_post
from harness.ui.gitops import GitOpError, init_preview, init_project, pull_into_origin
from harness.ui.host import NO_PROJECT, Host, HostConflict
from harness.ui.markdown import parse_markdown
from harness.ui.metrics import build_dashboard, build_overview
from harness.ui.office import (
    MAX_TEXT_CHARS,
    Office,
    OfficeConflict,
    live_run,
    office_view_from_log,
    plan_status,
    status_of,
)
from harness.ui.orchestrator import OrchestratorConflict
from harness.ui.projects import ProjectError, list_dir, load_recent, repo_info
from harness.ui.repofile import read_repo_file
from harness.ui.requests import list_requests, request_thread, request_title_of_run, run_titles
from harness.ui.runs import RunOptions, list_plans, unstaged_stages
from harness.ui.testrun import TestRunConflict, runner_for
from harness.ui.uploads import (
    IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    TEXT_TYPE,
    TOO_LARGE,
    UploadError,
    check_image_ids,
    file_name,
    find_upload_by_name,
    image_path,
    preview_document,
    store_attachment,
)
from harness.ui.view import build_view, list_runs

STATIC_DIR: Path = Path(__file__).parent / "static"
HEARTBEAT_S = 15.0
MAX_BODY_BYTES = 65536
DRAIN_LIMIT = 1 << 20

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
_VIEW_ERRORS = (FileNotFoundError, StateError, OSError, ValueError)

_TOKEN_META = '<meta name="harness-token" content="">'
_MODE_META = '<meta name="harness-mode" content="read-only">'

POST_ROUTES = ("start", "message", "implement", "next-stage", "stage")
CHAT_ROUTES = ("start", "message", "close")
RUN_ROUTES = ("resume", "stop", "launch", "options")
MAX_PLAN_PATH_CHARS = 500
ORCH_ROUTES = ("message", "new", "explain")
PROJECT_ROUTES = ("browse", "open", "init-preview", "init", "pull-origin")
MAX_PATH_CHARS = 4096
ROUTE_CHOICES = ("auto", "quick", "project")


# the tab icon: a starburst in ServiceNow green
# one ray every 30 degrees, from a small hub out to a length of 17-28 (viewBox 0 0 64 64)
_RAY_LENGTHS = (27, 19, 25, 17, 28, 21, 24, 18, 27, 20, 26, 22)


def _favicon_svg() -> str:
    rays = []
    for i, length in enumerate(_RAY_LENGTHS):
        angle = math.radians(i * 30 - 90)
        x1, y1 = 32 + 6 * math.cos(angle), 32 + 6 * math.sin(angle)
        x2, y2 = 32 + length * math.cos(angle), 32 + length * math.sin(angle)
        rays.append(f"M{x1:.1f} {y1:.1f}L{x2:.1f} {y2:.1f}")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<path d="{"".join(rays)}" fill="none" stroke="#63df4e" stroke-width="5.5" '
        'stroke-linecap="round"/></svg>'
    )


FAVICON_SVG = _favicon_svg()


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
    message {text}; stage {stage} (an int of at least 1); launch {plan_path}; options
    {review, max_parallel, max_cost, fake}; implement, next-stage, resume and stop {}.
    start with a description and message also accept an optional `images`."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route == "message" or (route == "start" and "description" in keys):
        keys.discard("images")
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
    elif route == "stage":
        if keys != {"stage"}:
            raise _BadBody("send exactly: stage")
        stage = body["stage"]
        if isinstance(stage, bool) or not isinstance(stage, int) or stage < 1:
            raise _BadBody("stage must be an integer of at least 1")
    elif route == "launch":
        if keys != {"plan_path"}:
            raise _BadBody("send exactly: plan_path")
        value = body["plan_path"]
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_PLAN_PATH_CHARS:
            raise _BadBody(
                f"plan_path must be a non-empty string of at most {MAX_PLAN_PATH_CHARS} characters"
            )
    elif route == "options":
        if keys != {"review", "max_parallel", "max_cost", "fake"}:
            raise _BadBody("send exactly: review, max_parallel, max_cost, fake")
        try:
            RunOptions.model_validate(body, strict=True)
        except ValidationError as err:
            first = err.errors()[0]
            where = ".".join(str(x) for x in first["loc"])
            raise _BadBody(f"bad run options: {where}: {first['msg']}") from None
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
    close {chat_id}. start and message also accept an optional `images`."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route in ("start", "message"):
        keys.discard("images")
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
    keys: message {text} with an optional `images`; new {}; explain {who} ("orchestrator" or
    "opus")."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route == "message":
        keys.discard("images")
        if keys != {"text"}:
            raise _BadBody("send exactly: text")
        _text_field(body, "text")
    elif route == "explain":
        if keys != {"who"}:
            raise _BadBody("send exactly: who")
        if body["who"] not in ("orchestrator", "opus"):
            raise _BadBody("who must be orchestrator or opus")
    elif keys:
        raise _BadBody("this endpoint takes no fields")
    return body


def _check_project_body(route: str, body: object) -> dict:
    """The body of a POST to /api/project/<route> or /api/server/restart, or _BadBody.
    Exactly the allowed keys: browse {path, hidden}; open {path}; init-preview, init,
    pull-origin and restart {}. `path` is at most MAX_PATH_CHARS characters and, after `~`
    is expanded, absolute."""
    if not isinstance(body, dict):
        raise _BadBody("body must be a JSON object")
    keys = set(body)
    if route in ("browse", "open"):
        want = {"path", "hidden"} if route == "browse" else {"path"}
        if keys != want:
            raise _BadBody(f"send exactly: {', '.join(sorted(want, reverse=True))}")
        value = body["path"]
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_PATH_CHARS:
            raise _BadBody(
                f"path must be a non-empty string of at most {MAX_PATH_CHARS} characters"
            )
        if "\0" in value or not Path(value).expanduser().is_absolute():
            raise _BadBody("path must be an absolute folder path")
        if route == "browse" and not isinstance(body["hidden"], bool):
            raise _BadBody("hidden must be true or false")
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
    host: Host | None = None,
) -> type[BaseHTTPRequestHandler]:
    """A request-handler class bound to `repo`. GET routes `/`, `/api/runs`,
    `/api/run/<run_id>`, `/api/stream/<run_id>`, `/api/office`, `/api/plan`, `/api/plans`,
    `/api/report/<run_id>`. Without an `office` every other method answers 405 (read-only).
    With one, POST `/api/planner/{start,message,implement,next-stage,stage}`, POST
    `/api/chat/{start,message,close}`, POST `/api/run/{resume,stop,launch,options}` and POST
    `/api/orchestrator/{message,new}` and `/api/upload` are served (GET `/api/upload/<id>`
    always; `token` guards the POSTs; one is made if not
    given) and every other method still answers 405. Request logging is silenced."""
    fixed_repo, fixed_office = repo, office
    interactive = office is not None or host is not None
    secret = (token or secrets.token_urlsafe(32)) if interactive else ""
    mode = "interactive" if interactive else "read-only"

    class Handler(BaseHTTPRequestHandler):
        _repo: Path
        _office: Office | None
        _no_project: bool = False

        def _bind(self) -> None:
            """Pick the project this request works on: the fixed one, or the Host's current
            one (`_no_project` when none is open)."""
            if host is None:
                self._repo, self._office, self._no_project = fixed_repo, fixed_office, False
                return
            project, current = host.current()
            self._repo, self._office = (project or host.cwd), current
            self._no_project = project is None

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str = "application/json; charset=utf-8",
            extra: dict[str, str] | None = None,
            cache: str = "no-store",
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
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
            if not interactive:
                self._not_get()
                return
            self._bind()
            try:
                self._post()
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

        def _upload(self) -> None:
            """Store the raw request body: an image (its magic bytes decide) or, with an X-Filename
            header, a text or code file."""
            if self._no_project:
                self._reject(409, NO_PROJECT)
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
            if length > MAX_IMAGE_BYTES:
                self._reject(413, TOO_LARGE)
                return
            self.connection.settimeout(30)
            data = self.rfile.read(length)
            try:
                sent = unquote(self.headers.get("X-Filename") or "")[:300]
                image_id = store_attachment(self._repo, data, sent or None)
            except UploadError as err:
                self._json(err.code, {"error": str(err)})
                return
            except OSError as err:
                self._json(500, {"error": f"could not store the upload: {err}"})
                return
            ext = image_id.rsplit(".", 1)[1]
            reply = {"id": image_id, "type": IMAGE_TYPES.get(ext, TEXT_TYPE), "bytes": len(data)}
            if ext not in IMAGE_TYPES:
                reply["name"] = file_name(self._repo, image_id) or image_id
            self._json(200, reply)

        def _image(self, image_id: str) -> None:
            p = image_path(self._repo, image_id)
            data = None
            if p is not None:
                try:
                    data = p.read_bytes()
                except OSError:
                    data = None
            if p is None or data is None:
                self._json(404, {"error": "unknown image"})
                return
            self._send(
                200,
                data,
                ctype=IMAGE_TYPES.get(p.name.rsplit(".", 1)[1], TEXT_TYPE),
                extra={"X-Content-Type-Options": "nosniff"},
                cache="private",
            )

        def _document(self, image_id: str) -> None:
            doc = preview_document(self._repo, image_id)
            if doc is None:
                self._json(404, {"error": "unknown document"})
                return
            self._json(200, doc, extra={"X-Content-Type-Options": "nosniff"})

        def _post(self) -> None:
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
            elif host is not None and path.startswith("/api/project/"):
                kind, route = "project", path[len("/api/project/") :]
                known = route in PROJECT_ROUTES
            elif host is not None and path == "/api/server/restart":
                kind, route, known = "server", "restart", True
            elif path == "/api/upload":
                kind, route, known = "upload", "", True
            elif path == "/api/tests/run":
                kind, route, known = "tests", "run", True
            elif path.startswith("/api/git/"):
                kind, route = "git", path[len("/api/git/") :]
                known = route in GIT_POST_ROUTES
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
            if kind == "upload":
                self._upload()
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
                elif kind in ("project", "server"):
                    body = _check_project_body(route, parsed)
                elif kind == "git":
                    body = check_git_body(route, parsed)
                else:
                    body = _check_body(route, parsed)
            except (ValueError, UnicodeDecodeError) as err:
                bad = isinstance(err, (_BadBody, BadGitBody))
                self._json(400, {"error": str(err) if bad else "bad JSON"})
                return
            if kind in ("project", "server"):
                assert host is not None
                self._project_post(host, route, body)
                return
            if kind == "tests":
                if self._no_project:
                    self._json(409, {"error": NO_PROJECT})
                    return
                try:
                    started = runner_for(self._repo).start()
                except TestRunConflict as err:
                    self._json(409, {"error": str(err)})
                    return
                self._json(200, {"ok": True, "tests": started})
                return
            if kind == "git":
                if self._no_project:
                    self._json(409, {"error": NO_PROJECT})
                    return
                code, reply = git_post(self._repo, route, body, self._git_busy())
                self._json(code, reply)
                return
            office = self._office
            if office is None:
                self._json(409, {"error": NO_PROJECT})
                return
            images: list[str] = []
            if "images" in body:
                try:
                    images = check_image_ids(self._repo, body["images"])
                except ValueError as err:
                    self._json(400, {"error": str(err)})
                    return
            try:
                if kind == "chat":
                    if route == "start":
                        office.chats.start(
                            body["tier"],
                            body["text"],
                            run_id=body.get("run_id"),
                            task_id=body.get("task_id"),
                            images=images,
                        )
                    elif route == "message":
                        office.chats.message(body["chat_id"], body["text"], images=images)
                    else:
                        office.chats.close_chat(body["chat_id"])
                elif kind == "run":
                    if route == "resume":
                        office.resume_run()
                    elif route == "stop":
                        office.stop_run()
                    elif route == "launch":
                        office.launch_plan(body["plan_path"])
                    else:
                        office.set_run_options(RunOptions.model_validate(body, strict=True))
                elif kind == "orchestrator":
                    if route == "explain":
                        self._json(200, office.explain_error(body["who"]))
                        return
                    if route == "message":
                        office.orchestrator.message(body["text"], images=images)
                    else:
                        office.orchestrator.new_thread()
                elif route == "start":
                    office.start(
                        description=body.get("description"),
                        from_draft=body.get("from_draft") is True,
                        route=body.get("route", "project"),
                        images=images,
                    )
                elif route == "message":
                    office.message(body["text"], images=images)
                elif route == "implement":
                    office.implement()
                elif route == "stage":
                    office.plan_stage(body["stage"])
                else:
                    office.next_stage()
            except (OfficeConflict, ChatConflict, OrchestratorConflict) as err:
                self._json(409, {"error": str(err)})
                return
            self._json(200, {"ok": True, "office": office.view().model_dump(mode="json")})

        def _git_busy(self) -> str | None:
            """Why git writes are refused now, or None: the Host's busy reason, else a run
            still going in the project."""
            reason = host.busy() if host is not None else None
            if reason is None and (live := live_run(self._repo)) is not None:
                reason = f"a run is active ({live}); wait for it to finish"
            return reason

        def _project_post(self, host: Host, route: str, body: dict) -> None:
            """The /api/project/* and /api/server/restart routes. ProjectError answers 400;
            HostConflict and GitOpError answer 409."""
            try:
                if route == "browse":
                    listing = list_dir(Path(body["path"]).expanduser(), body["hidden"])
                    self._json(200, listing.model_dump(mode="json"))
                elif route == "open":
                    host.open_project(Path(body["path"]).expanduser())
                    project, _ = host.current()
                    self._json(
                        200,
                        {"ok": True, "project": repo_info(project).model_dump(mode="json")}
                        if project is not None
                        else {"ok": True, "project": None},
                    )
                elif route == "restart":
                    host.request_restart()
                    self._json(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self._project_git(host, route)
            except ProjectError as err:
                self._json(400, {"error": str(err)})
            except (HostConflict, GitOpError) as err:
                self._json(409, {"error": str(err)})

        def _project_git(self, host: Host, route: str) -> None:
            """init-preview, init and pull-origin act on the open project."""
            project, office = host.current()
            if project is None or office is None:
                raise HostConflict(NO_PROJECT)
            if route == "init-preview":
                files, truncated = init_preview(project)
                self._json(200, {"files": files, "truncated": truncated})
                return
            if route == "init":
                reason = host.busy() or (
                    f"a run is active ({live}); wait for it to finish"
                    if (live := live_run(project)) is not None
                    else None
                )
                if reason is not None:
                    raise HostConflict(reason)
                sha = init_project(project)
                self._json(
                    200,
                    {"ok": True, "sha": sha, "project": repo_info(project).model_dump(mode="json")},
                )
                return
            self._json(200, {"ok": True, "output": pull_into_origin(project)})

        def _project_info(self) -> None:
            """GET /api/project: the open project and what the browser needs around it."""
            assert host is not None
            project, _ = host.current()
            self._json(
                200,
                {
                    "project": repo_info(project).model_dump(mode="json") if project else None,
                    "recent": load_recent(host.recent_path),
                    "cwd": str(host.cwd),
                    "home": str(Path.home()),
                    "started": host.started,
                    "busy": host.busy(),
                },
            )

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "bad host"})
                return
            self._bind()
            try:
                self._route()
            except _CLIENT_GONE:
                return

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if len(path) > 1:
                path = path.rstrip("/")
            if path == "/favicon.svg":
                self._send(200, FAVICON_SVG.encode(), ctype="image/svg+xml")
                return
            if path == "/":
                try:
                    body = render_page(secret, mode)
                except OSError:
                    self._json(404, {"error": "not found"})
                    return
                self._send(200, body, ctype="text/html; charset=utf-8")
                return
            if host is not None and path == "/api/project":
                self._project_info()
                return
            if self._no_project:
                self._json(409, {"error": NO_PROJECT})
                return
            repo, office = self._repo, self._office
            if path.startswith("/api/git/"):
                query = parse_qs(urlsplit(self.path).query)
                code, body = git_get(repo, unquote(path[len("/api/git/") :]), query)
                self._json(code, body)
                return
            if path == "/api/file":
                wanted = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
                doc = read_repo_file(repo, wanted)
                if doc is None:  # a file the user attached, mentioned by its own name
                    found = find_upload_by_name(repo, wanted)
                    doc = preview_document(repo, found) if found else None
                if doc is None:
                    self._json(404, {"error": "unknown file"})
                else:
                    self._json(200, doc, extra={"X-Content-Type-Options": "nosniff"})
                return
            if path.startswith("/api/upload/"):
                upload_id = unquote(path[len("/api/upload/") :])
                if parse_qs(urlsplit(self.path).query).get("view") == ["doc"]:
                    self._document(upload_id)
                else:
                    self._image(upload_id)
                return
            if path == "/api/tests":
                self._json(200, runner_for(repo).view())
                return
            if path == "/api/office":
                view = office.view() if office is not None else office_view_from_log(repo)
                self._json(200, view.model_dump(mode="json"))
                return
            if path == "/api/plan":
                self._plan()
                return
            if path == "/api/plans":
                self._json(
                    200,
                    {
                        "plans": [e.model_dump(mode="json") for e in list_plans(repo)],
                        "unstaged": unstaged_stages(repo),
                        "plan_status": plan_status(repo),
                    },
                )
                return
            if path.startswith("/api/report/"):
                self._report(unquote(path[len("/api/report/") :]))
                return
            if path == "/api/overview":
                self._overview()
                return
            if path.startswith("/api/dashboard/"):
                self._dashboard(unquote(path[len("/api/dashboard/") :]))
                return
            if path == "/api/runs":
                try:
                    runs = list_runs(repo)
                except (StateError, OSError, ValueError) as err:
                    self._json(500, {"error": str(err)})
                    return
                requests = list_requests(repo)
                self._json(
                    200,
                    {
                        "runs": runs,
                        "latest": runs[0] if runs else None,
                        "titles": run_titles(repo, runs, requests),
                        "requests": [r.model_dump(mode="json") for r in requests],
                    },
                )
                return
            if path.startswith("/api/orchestrator/thread/"):
                thread = request_thread(repo, unquote(path[len("/api/orchestrator/thread/") :]))
                if thread is None:
                    self._json(404, {"error": "unknown request"})
                    return
                self._json(200, thread.model_dump(mode="json"))
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
            repo, office = self._repo, self._office
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
            path = self._repo / PurePosixPath(quick_plan).parent / "plan.md"
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

        def _report(self, run_id: str) -> None:
            """The status and cost report texts of a run, as `harness status` and
            `harness report` print them."""
            repo = self._repo
            try:
                if run_id not in list_runs(repo):
                    self._json(404, {"error": "unknown run"})
                    return
                state = load_state(run_dir(repo, run_id))
            except (StateError, OSError, ValueError) as err:
                self._json(500, {"error": str(err)})
                return
            self._json(
                200,
                {
                    "run_id": run_id,
                    "status": status_text(state),
                    "report": report_text(state),
                    "data": report_data(state),
                    "title": request_title_of_run(repo, run_id),
                },
            )

        def _overview(self) -> None:
            """The cost and time of every remembered project, plus the open one."""
            recent = load_recent(host.recent_path) if host is not None else []
            repos = [self._repo, *(Path(p) for p in recent)]
            try:
                body = build_overview(repos).model_dump_json().encode()
            except (StateError, OSError, ValueError) as err:
                self._json(500, {"error": str(err)})
                return
            self._send(200, body)

        def _dashboard(self, run_id: str) -> None:
            """The cost, time and latency numbers of a run next to the average of the others."""
            try:
                body = build_dashboard(self._repo, run_id).model_dump_json().encode()
            except FileNotFoundError:
                self._json(404, {"error": "unknown run"})
                return
            except (StateError, OSError, ValueError) as err:
                self._json(500, {"error": str(err)})
                return
            self._send(200, body)

        def _run(self, run_id: str) -> None:
            try:
                body = build_view(self._repo, run_id).model_dump_json().encode()
            except FileNotFoundError:
                self._json(404, {"error": "unknown run"})
                return
            except (StateError, OSError, ValueError) as err:
                self._json(500, {"error": str(err)})
                return
            self._send(200, body)

        def _snapshot(self, run_id: str, sig: tuple[int, int]) -> bytes:
            try:
                return (
                    b"data: " + build_view(self._repo, run_id).model_dump_json().encode() + b"\n\n"
                )
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
            repo = self._repo
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
    host: Host | None = None,
) -> ThreadingHTTPServer:
    """A server bound to ("127.0.0.1", port); port 0 picks a free port. An OSError from the
    bind propagates. With an `office` the POST endpoints are on (see make_handler). The caller
    runs serve_forever() and later shutdown() + server_close()."""
    return _Server(
        ("127.0.0.1", port), make_handler(repo, poll_interval, heartbeat_s, office, token, host)
    )
