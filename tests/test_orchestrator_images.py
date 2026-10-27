import json
import time

import pytest
from git_helpers import init_repo

from harness.models import Usage
from harness.planner import FakePlannerSession, PlannerTurn
from harness.planner_log import read_planner_events
from harness.ui.chat import ChatManager, read_chat_events
from harness.ui.orchestrator import Orchestrator, orchestrator_view_from_log
from harness.ui.uploads import attachment_note, store_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def route(to, plan="project", reason="because", cost=0.02):
    body = json.dumps({"to": to, "plan": plan, "reason": reason})
    return PlannerTurn(text=f"ok\n\n```json\n{body}\n```", usage=Usage(cost_usd=cost))


class ImageOpus:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, tuple]] = []
        self.messages: list[tuple[str, tuple]] = []
        self.state = "asking"

    def start(self, text, plan, images=()):
        self.started.append((text, plan, tuple(images)))
        return "planner-1"

    def message(self, text, images=()):
        self.messages.append((text, tuple(images)))

    def live(self, session_id):
        return session_id == "planner-1" and self.state in ("busy", "asking", "ready", "staging")

    def waiting(self, session_id):
        return session_id == "planner-1" and self.state in ("asking", "ready")


class OldOpus:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.messages: list[str] = []

    def start(self, text, plan):
        self.started.append((text, plan))
        return "planner-1"

    def message(self, text):
        self.messages.append(text)

    def live(self, session_id):
        return session_id == "planner-1"

    def waiting(self, session_id):
        return session_id == "planner-1"


class Env:
    def __init__(self, repo, opus=None):
        self.repo = repo
        self.chats = ChatManager(repo, fake=True, fake_delay=0)
        self.opus = opus or ImageOpus()
        self.steps: list = []
        self.sessions: list[FakePlannerSession] = []
        self.orch = Orchestrator(repo, self.chats, self.opus, session_factory=self.factory)

    def factory(self, repo, progress):
        s = FakePlannerSession([self.steps.pop(0)])
        self.sessions.append(s)
        return s

    def say(self, text, step, images=()):
        self.steps.append(step)
        self.orch.message(text, images)
        wait_for(lambda: self.orch.view().state != "routing")

    def chat_users(self):
        return [e for e in read_chat_events(self.repo) if e.type == "chat_user"]

    def waiting_chat(self):
        return wait_for(lambda: all(v.state == "waiting" for v in self.chats.views()))


@pytest.fixture
def env(tmp_path):
    init_repo(tmp_path)
    e = Env(tmp_path)
    yield e
    e.orch.close()
    e.chats.close()


def test_new_request_with_image(env):
    img = store_image(env.repo, PNG)
    env.say("look", route("haiku"), [img])
    assert env.sessions[0].sent[0].endswith("look" + attachment_note(env.repo, [img]))
    (user,) = env.chat_users()
    assert user.payload["images"] == [img]
    (ou,) = [e for e in read_planner_events(env.repo) if e.type == "orchestrator_user"]
    assert ou.payload["images"] == [img]
    entry = orchestrator_view_from_log(env.repo).transcript[0]
    assert entry.role == "user" and entry.images == [img]


def test_no_images_key_without_images(env):
    env.say("plain", route("haiku"))
    (ou,) = [e for e in read_planner_events(env.repo) if e.type == "orchestrator_user"]
    assert "images" not in ou.payload
    assert "images" not in env.chat_users()[0].payload


def test_forward_and_escalate(env):
    a = store_image(env.repo, PNG)
    b = store_image(env.repo, JPEG)
    env.say("start", route("haiku"))
    env.waiting_chat()
    env.say("more", route("haiku"), [a])
    wait_for(lambda: len(env.chat_users()) == 2)
    assert env.chat_users()[1].payload["images"] == [a]
    env.waiting_chat()
    env.say("harder", route("sonnet"), [a, b])
    wait_for(lambda: len(env.chat_users()) == 3)
    assert env.chat_users()[2].payload["images"] == [a, b]


def test_route_to_opus_and_opus_holder(env):
    a = store_image(env.repo, PNG)
    env.say("big", route("opus"), [a])
    assert env.opus.started[0][2] == (a,)
    env.say_opus = env.orch.message("more", [a])
    assert env.opus.messages == [("more", (a,))]


def test_old_opus_signature(tmp_path):
    init_repo(tmp_path)
    e = Env(tmp_path, OldOpus())
    try:
        e.say("big", route("opus"))
        assert len(e.opus.started) == 1
        e.orch.message("more")
        assert e.opus.messages == ["more"]
    finally:
        e.orch.close()
        e.chats.close()


def test_demo_triage_with_image(tmp_path):
    init_repo(tmp_path)
    chats = ChatManager(tmp_path, fake=True, fake_delay=0)
    orch = Orchestrator(tmp_path, chats, ImageOpus(), fake=True, fake_delay=0)
    try:
        img = store_image(tmp_path, PNG)
        orch.message("hello", [img])
        wait_for(lambda: orch.view().state != "routing")
        assert orch.view().state == "idle"
    finally:
        orch.close()
        chats.close()
