"""The run viewer's HTTP server: read-only, loopback only, stdlib only."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from harness.events import EVENTS_FILE
from harness.state import STATE_FILE, StateError, run_dir
from harness.ui.view import build_view, list_runs

STATIC_DIR: Path = Path(__file__).parent / "static"
HEARTBEAT_S = 15.0

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
_VIEW_ERRORS = (FileNotFoundError, StateError, OSError, ValueError)


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
    repo: Path, poll_interval: float = 0.5, heartbeat_s: float = HEARTBEAT_S
) -> type[BaseHTTPRequestHandler]:
    """A request-handler class bound to `repo`. GET routes `/`, `/api/runs`,
    `/api/run/<run_id>`, `/api/stream/<run_id>`; every other method answers 405. Request
    logging is silenced."""

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
            self._json(405, {"error": "read-only"}, extra={"Allow": "GET"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_get

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
                    body = (STATIC_DIR / "index.html").read_bytes()
                except OSError:
                    self._json(404, {"error": "not found"})
                    return
                self._send(200, body, ctype="text/html; charset=utf-8")
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
                    last_write = time.monotonic()

        def handle(self) -> None:
            try:
                super().handle()
            except _CLIENT_GONE:
                return

    return Handler


def make_server(
    repo: Path, port: int, poll_interval: float = 0.5, heartbeat_s: float = HEARTBEAT_S
) -> ThreadingHTTPServer:
    """A server bound to ("127.0.0.1", port); port 0 picks a free port. An OSError from the
    bind propagates. The caller runs serve_forever() and later shutdown() + server_close()."""
    return _Server(("127.0.0.1", port), make_handler(repo, poll_interval, heartbeat_s))
