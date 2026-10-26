"""The chat POST endpoints of the interactive office server: the same safety checks as the
planner routes, plus the chat schema. Runs against the scripted fake chat session only."""

import http.client
import json
import re
import threading
import time
from pathlib import Path

import pytest
from git_helpers import init_repo
from ui_fixtures import write_happy_run

from harness.ui.office import Office
from harness.ui.server import make_server

START = "/api/chat/start"
MESSAGE = "/api/chat/message"
CLOSE = "/api/chat/close"


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
    repo = init_repo(tmp_path / "repo")
    office = Office(repo, fake=True, fake_delay=0)
    s = Site(repo, office)
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


def chats(site: Site) -> list[dict]:
    return site.get_json("/api/office")["chats"]


def chat_state(site: Site, chat_id: str) -> str:
    return next(c["state"] for c in chats(site) if c["chat_id"] == chat_id)


# ---------------------------------------------------------------- the chat lifecycle


def test_start_message_close_over_http(site):
    code, _, body = site.post(START, {"tier": "haiku", "text": "hi"})
    assert code == 200
    assert body["ok"] is True
    assert body["office"]["mode"] == "interactive"
    open_chats = body["office"]["chats"]
    assert len(open_chats) == 1
    assert open_chats[0]["tier"] == "haiku"
    chat_id = open_chats[0]["chat_id"]

    wait_for(lambda: chat_state(site, chat_id) == "waiting")
    code, _, body = site.post(MESSAGE, {"chat_id": chat_id, "text": "more"})
    assert code == 200
    assert body["ok"] is True

    code, _, body = site.post(CLOSE, {"chat_id": chat_id})
    assert code == 200
    assert body["ok"] is True
    wait_for(lambda: chat_state(site, chat_id) == "closed")


def test_second_haiku_start_while_open_is_409(site):
    code, _, _ = site.post(START, {"tier": "haiku", "text": "hi"})
    assert code == 200
    code, _, body = site.post(START, {"tier": "haiku", "text": "again"})
    assert code == 409
    assert "already open" in body["error"]


def test_message_to_unknown_chat_is_409(site):
    code, _, body = site.post(MESSAGE, {"chat_id": "nope", "text": "hi"})
    assert code == 409
    assert "unknown chat" in body["error"]


def test_close_unknown_chat_is_409(site):
    code, _, body = site.post(CLOSE, {"chat_id": "nope"})
    assert code == 409
    assert "unknown chat" in body["error"]


# ---------------------------------------------------------------- body schema


@pytest.mark.parametrize(
    "path, body, message",
    [
        (START, {"tier": "opus", "text": "hi"}, "tier must be haiku or sonnet"),
        (START, {"tier": 1, "text": "hi"}, "tier must be haiku or sonnet"),
        (START, {"tier": "haiku"}, "send exactly"),
        (START, {"tier": "haiku", "text": "hi", "extra": 1}, "send exactly"),
        (START, {"tier": "haiku", "text": "hi", "run_id": "r"}, "send exactly"),
        (START, {"tier": "haiku", "text": "   "}, "text must be a non-empty string"),
        (
            START,
            {"tier": "haiku", "text": "hi", "run_id": "", "task_id": "t"},
            "run_id must be a non-empty string",
        ),
        (MESSAGE, {"chat_id": "", "text": "hi"}, "chat_id must be a non-empty string"),
        (MESSAGE, {"text": "hi"}, "send exactly"),
        (CLOSE, {"chat_id": ""}, "chat_id must be a non-empty string"),
        (CLOSE, {"chat_id": "x", "text": "hi"}, "send exactly"),
        (CLOSE, ["x"], "body must be a JSON object"),
    ],
)
def test_bad_chat_body_is_400(site, path, body, message):
    code, _, out = site.post(path, body)
    assert code == 400
    assert message in out["error"]


def test_bad_chat_json_is_400(site):
    code, _, out = site.post(START, raw=b"{not json")
    assert code == 400
    assert out["error"] == "bad JSON"


# ---------------------------------------------------------------- safety rules


def test_bad_token_is_403(site):
    code, _, body = site.post(START, {"tier": "haiku", "text": "hi"}, token="wrong")
    assert code == 403
    assert body["error"] == "bad token"


def test_bad_origin_is_403(site):
    code, _, body = site.post(START, {"tier": "haiku", "text": "hi"}, origin="http://evil.example")
    assert code == 403
    assert body["error"] == "bad origin"


def test_unknown_chat_route_is_404(site):
    code, _, body = site.post("/api/chat/nope", {"tier": "haiku", "text": "hi"}, token=None)
    assert code == 404
    assert body["error"] == "not found"


def test_bad_host_is_403(site):
    headers = {"Content-Type": "application/json", "Host": "evil.example"}
    status, _, out = site.request("POST", START, b"{}", headers)
    assert status == 403
    assert json.loads(out)["error"] == "bad host"


def test_rejected_chat_start_changes_nothing(site):
    code, _, _ = site.post(START, {"tier": "opus", "text": "hi"})
    assert code == 400
    assert chats(site) == []


# ---------------------------------------------------------------- read-only and planner


def test_read_only_server_answers_405_to_chat_post(read_only_site):
    code, _, body = read_only_site.post(START, {"tier": "haiku", "text": "hi"}, token=None)
    assert code == 405
    assert body == {"error": "read-only"}


def test_planner_start_still_works(site):
    code, _, body = site.post("/api/planner/start", {"description": "a demo"})
    assert code == 200
    assert body["ok"] is True
