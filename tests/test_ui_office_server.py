import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from ui_fixtures import write_happy_run

from harness.ui.office import Office
from harness.ui.server import make_server


def _snapshot(root: Path) -> dict:
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


class Site:
    """A running server plus small HTTP helpers that control every header."""

    def __init__(self, repo: Path, office: Office | None) -> None:
        self.repo, self.office = repo, office
        self.server = make_server(repo, 0, poll_interval=0.05, heartbeat_s=0.2, office=office)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        if self.office is not None:
            self.office.close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            conn.close()

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        assert status == 200, data
        return json.loads(data)

    @property
    def token(self) -> str:
        _, _, page = self.request("GET", "/")
        m = re.search(rb'<meta name="harness-token" content="([^"]*)">', page)
        assert m is not None
        return m.group(1).decode()

    def post(self, path, body=None, *, token="ok", origin="ok", ctype="application/json", raw=None):
        headers = {"Content-Type": ctype} if ctype else {}
        if token == "ok":
            token = self.token
        if token is not None:
            headers["X-Harness-Token"] = token
        if origin == "ok":
            origin = self.origin
        if origin is not None:
            headers["Origin"] = origin
        data = raw if raw is not None else json.dumps(body).encode()
        status, hdrs, out = self.request("POST", path, data, headers)
        return status, hdrs, (json.loads(out) if out else None)


@pytest.fixture
def site(tmp_path):
    office = Office(tmp_path, fake=True, fake_delay=0)
    s = Site(tmp_path, office)
    yield s
    s.stop()


@pytest.fixture
def read_only_site(tmp_path):
    write_happy_run(tmp_path)
    s = Site(tmp_path, None)
    yield s
    s.stop()


def wait_for(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def state(site: Site) -> str:
    return site.get_json("/api/office")["state"]


START = "/api/planner/start"


# ---------------------------------------------------------------- safety rules


def test_valid_start_is_200_then_second_start_is_409(site):
    code, _, body = site.post(START, {"description": "a demo"})
    assert code == 200
    assert body["ok"] is True
    assert body["office"]["mode"] == "interactive"
    assert body["office"]["state"] in ("busy", "asking")
    code, _, body = site.post(START, {"description": "again"})
    assert code == 409
    assert "in progress" in body["error"]


def test_message_flow_over_http(site):
    site.post(START, {"description": "a demo"})
    wait_for(lambda: state(site) == "asking")
    code, _, body = site.post("/api/planner/message", {"text": "answers"})
    assert code == 200
    wait_for(lambda: state(site) == "ready")
    code, _, body = site.post("/api/planner/message", {"text": "more"})
    assert code == 200


def test_message_before_start_is_409(site):
    code, _, body = site.post("/api/planner/message", {"text": "hi"})
    assert code == 409
    assert "not waiting" in body["error"]


@pytest.mark.parametrize(
    "kwargs, code, message",
    [
        ({"origin": "http://evil.example"}, 403, "bad origin"),
        ({"origin": None}, 403, "bad origin"),
        ({"origin": "http://127.0.0.1:1"}, 403, "bad origin"),
        ({"token": "wrong"}, 403, "bad token"),
        ({"token": None}, 403, "bad token"),
        ({"token": ""}, 403, "bad token"),
        ({"ctype": "text/plain"}, 415, "application/json"),
        ({"ctype": None}, 415, "application/json"),
        ({"raw": b"x" * 70_000}, 413, "too large"),
        ({"raw": b"not json"}, 400, "bad JSON"),
        ({"raw": b"[1]"}, 400, "JSON object"),
        ({"raw": b"null"}, 400, "JSON object"),
        ({"raw": b"{}"}, 400, "exactly one of"),
        ({"raw": b'{"description": "x", "path": "/etc"}'}, 400, "exactly one of"),
        ({"raw": b'{"description": 5}'}, 400, "non-empty string"),
        ({"raw": b'{"description": "   "}'}, 400, "non-empty string"),
        ({"raw": json.dumps({"description": "x" * 20_001}).encode()}, 400, "at most"),
        ({"raw": b'{"from_draft": false}'}, 400, "must be true"),
        ({"raw": b'{"from_draft": true, "description": "x"}'}, 400, "exactly one of"),
    ],
)
def test_rejected_requests_change_nothing(site, tmp_path, kwargs, code, message):
    before = _snapshot(tmp_path)
    got, _, body = site.post(START, {"description": "x"}, **kwargs)
    assert got == code
    assert message in body["error"]
    assert _snapshot(tmp_path) == before
    assert state(site) == "idle"


def test_schemas_of_the_other_routes(site):
    assert site.post("/api/planner/message", {"text": ""})[0] == 400
    assert site.post("/api/planner/message", {"text": "x", "extra": 1})[0] == 400
    assert site.post("/api/planner/message", {})[0] == 400
    assert site.post("/api/planner/implement", {"force": True})[0] == 400
    assert site.post("/api/planner/next-stage", {"stage": 2})[0] == 400


def test_unknown_route_is_404_and_checked_before_origin(site):
    assert site.post("/api/planner/nope", {}, origin=None)[0] == 404
    assert site.post("/api/anything", {})[0] == 404
    assert site.post("/api/planner/", {})[0] == 404


def test_missing_content_length_is_411(site):
    with socket.create_connection(("127.0.0.1", site.port), timeout=5) as sock:
        sock.sendall(
            (
                f"POST {START} HTTP/1.0\r\nHost: 127.0.0.1:{site.port}\r\n"
                f"Origin: {site.origin}\r\nX-Harness-Token: {site.token}\r\n"
                "Content-Type: application/json\r\n\r\n"
            ).encode()
        )
        reply = sock.recv(4096)
    assert reply.startswith(b"HTTP/1.0 411")


def test_bad_host_is_403_for_post_and_get(site):
    headers = {"Host": "evil.example", "Origin": site.origin, "X-Harness-Token": site.token}
    headers["Content-Type"] = "application/json"
    status, _, body = site.request("POST", START, b'{"description": "x"}', headers)
    assert status == 403 and b"bad host" in body
    status, _, _ = site.request("GET", "/api/office", None, {"Host": "evil.example"})
    assert status == 403


def test_localhost_origin_is_accepted(site):
    origin = f"http://localhost:{site.port}"
    assert site.post(START, {"description": "x"}, origin=origin)[0] == 200


def test_other_methods_are_405_in_interactive_mode(site):
    for method in ("OPTIONS", "PUT", "DELETE", "PATCH"):
        status, headers, _ = site.request(method, START, b"{}" if method != "OPTIONS" else None)
        assert status == 405, method
        assert headers["allow"] == "GET, POST"
    status, headers, _ = site.request(
        "OPTIONS",
        START,
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-harness-token",
        },
    )
    assert status == 405
    assert "access-control-allow-origin" not in headers


def test_get_pages_carry_no_cors_headers(site):
    _, headers, _ = site.request("GET", "/api/office", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in headers


# ---------------------------------------------------------------- read-only mode


@pytest.mark.parametrize("path", [START, "/api/planner/message", "/api/planner/implement", "/"])
def test_read_only_server_answers_405_to_every_post(read_only_site, tmp_path, path):
    before = _snapshot(tmp_path)
    s = read_only_site
    headers = {
        "Origin": s.origin,
        "X-Harness-Token": "anything",
        "Content-Type": "application/json",
    }
    status, hdrs, body = s.request("POST", path, b'{"description": "x"}', headers)
    assert status == 405
    assert hdrs["allow"] == "GET"
    assert json.loads(body) == {"error": "read-only"}
    assert _snapshot(tmp_path) == before


def test_read_only_page_has_an_empty_token_and_the_read_only_mode(read_only_site):
    _, _, page = read_only_site.request("GET", "/")
    assert b'<meta name="harness-token" content="">' in page
    assert b'<meta name="harness-mode" content="read-only">' in page


def test_read_only_office_view_comes_from_the_log(read_only_site):
    view = read_only_site.get_json("/api/office")
    assert view["mode"] == "read-only"
    assert view["state"] == "idle"
    assert view["can_click"] is False


# ---------------------------------------------------------------- pages and plan


def test_interactive_page_carries_the_token_and_mode(site):
    _, _, page = site.request("GET", "/")
    m = re.search(rb'<meta name="harness-token" content="([A-Za-z0-9_-]{32,})">', page)
    assert m is not None
    assert b'<meta name="harness-mode" content="interactive">' in page
    _, _, again = site.request("GET", "/")
    assert again == page  # one token per server


def test_two_servers_make_different_tokens(tmp_path):
    a = Site(tmp_path, Office(tmp_path, fake=True, fake_delay=0))
    b = Site(tmp_path, Office(tmp_path, fake=True, fake_delay=0))
    try:
        assert a.token != b.token
    finally:
        a.stop()
        b.stop()


def test_plan_endpoint_renders_the_draft(site):
    assert site.get_json("/api/plan") == {
        "exists": False,
        "status": None,
        "blocks": [],
        "kind": "project",
    }
    site.post(START, {"description": "a demo"})
    wait_for(lambda: state(site) == "asking")
    site.post("/api/planner/message", {"text": "answers"})
    wait_for(lambda: state(site) == "ready")
    plan = site.get_json("/api/plan")
    assert plan["exists"] is True
    assert plan["status"] == "draft"
    kinds = {b["type"] for b in plan["blocks"]}
    assert {"heading", "list", "table", "code"} <= kinds
    office = site.get_json("/api/office")
    assert office["plan_exists"] is True
    assert office["plan_status"] == "draft"
    assert office["reply_blocks"]
