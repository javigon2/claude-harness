"""The `images` field of the five message routes: ids of uploaded images are checked (400
when malformed or unknown) and reach the office, the chats and the Orchestrator."""

import http.client
import json
import re
import threading
import time
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.office import Office
from harness.ui.server import make_server
from harness.ui.uploads import store_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
BAD_IMAGES = "images must be a list of 1 to 4 image ids"


class Site:
    """A running server plus small HTTP helpers."""

    def __init__(self, repo: Path, office: Office) -> None:
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
        self.office.close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def get_json(self, path):
        status, data = self.request("GET", path)
        assert status == 200, data
        return json.loads(data)

    @property
    def token(self) -> str:
        _, page = self.request("GET", "/")
        m = re.search(rb'<meta name="harness-token" content="([^"]*)">', page)
        assert m is not None
        return m.group(1).decode()

    def post(self, path, body):
        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": self.token,
            "Origin": self.origin,
        }
        status, out = self.request("POST", path, json.dumps(body).encode(), headers)
        return status, (json.loads(out) if out else None)

    def office_view(self) -> dict:
        return self.get_json("/api/office")


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    office = Office(repo, fake=True, fake_delay=0)
    s = Site(repo, office)
    yield s
    s.stop()


@pytest.fixture
def image_id(site):
    return store_image(site.repo, PNG)


def wait_for(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def user_images(entries) -> list[list[str]]:
    return [e["images"] for e in entries if e["role"] == "user"]


def chat_id_of(site) -> str:
    return site.office_view()["chats"][0]["chat_id"]


def chat_view(site, chat_id) -> dict:
    return next(c for c in site.office_view()["chats"] if c["chat_id"] == chat_id)


def test_orchestrator_message_with_images(site, image_id):
    code, body = site.post("/api/orchestrator/message", {"text": "hi", "images": [image_id]})
    assert code == 200
    assert body["ok"] is True
    wait_for(lambda: [image_id] in user_images(site.office_view()["orchestrator"]["transcript"]))


def test_chat_start_with_images(site, image_id):
    code, _ = site.post("/api/chat/start", {"tier": "haiku", "text": "hi", "images": [image_id]})
    assert code == 200
    wait_for(lambda: [image_id] in user_images(site.office_view()["chats"][0]["transcript"]))


def test_planner_start_with_images(site, image_id):
    code, _ = site.post("/api/planner/start", {"description": "a demo", "images": [image_id]})
    assert code == 200
    wait_for(lambda: [image_id] in user_images(site.office_view()["transcript"]))


def test_chat_message_with_and_without_images(site, image_id):
    site.post("/api/chat/start", {"tier": "haiku", "text": "hi"})
    cid = chat_id_of(site)
    wait_for(lambda: chat_view(site, cid)["state"] == "waiting")
    code, _ = site.post("/api/chat/message", {"chat_id": cid, "text": "pic", "images": [image_id]})
    assert code == 200
    wait_for(lambda: [image_id] in user_images(chat_view(site, cid)["transcript"]))
    wait_for(lambda: chat_view(site, cid)["state"] == "waiting")
    code, _ = site.post("/api/chat/message", {"chat_id": cid, "text": "plain"})
    assert code == 200
    wait_for(lambda: user_images(chat_view(site, cid)["transcript"])[-1] == [])


def test_planner_message_with_and_without_images(site, image_id):
    site.post("/api/planner/start", {"description": "a demo"})
    wait_for(lambda: site.office_view()["state"] == "asking")
    code, _ = site.post("/api/planner/message", {"text": "pic", "images": [image_id]})
    assert code == 200
    wait_for(lambda: [image_id] in user_images(site.office_view()["transcript"]))
    wait_for(lambda: site.office_view()["state"] == "ready")
    code, _ = site.post("/api/planner/message", {"text": "plain"})
    assert code == 200


def test_bad_images_are_400(site, image_id):
    unknown = "0" * 64 + ".png"
    cases = [
        ([unknown], f"unknown image id: {unknown}"),
        (["../../etc/passwd"], "unknown image id: ../../etc/passwd"),
        ([unknown] * 5, BAD_IMAGES),
        (image_id, BAD_IMAGES),
    ]
    for images, message in cases:
        code, body = site.post("/api/orchestrator/message", {"text": "hi", "images": images})
        assert (code, body["error"]) == (400, message)
    assert site.office_view()["chats"] == []


def test_five_ids_are_400(site):
    ids = [f"{i}" * 64 + ".png" for i in range(5)]
    code, body = site.post("/api/chat/start", {"tier": "haiku", "text": "hi", "images": ids})
    assert (code, body["error"]) == (400, BAD_IMAGES)


def test_from_draft_with_images_keeps_old_error(site, image_id):
    code, body = site.post("/api/planner/start", {"from_draft": True, "images": [image_id]})
    assert code == 400
    assert body["error"] == "send exactly one of description or from_draft"
