import asyncio
import json
import threading
import time

import pytest
from git_helpers import init_repo

from harness.models import Usage
from harness.planner import FakePlannerSession, PlannerTurn, SdkRoutingSession
from harness.planner_log import read_planner_events
from harness.ui.chat import ChatManager
from harness.ui.orchestrator import (
    NO_ROUTING_NEW,
    OPUS_BUSY,
    OPUS_HOLDS,
    ROUTING_BUSY,
    HandoffRefused,
    Orchestrator,
    OrchestratorConflict,
)


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def route(to, plan="project", reason="because", cost=0.02, text="ok"):
    body = json.dumps({"to": to, "plan": plan, "reason": reason})
    return PlannerTurn(text=f"{text}\n\n```json\n{body}\n```", usage=Usage(cost_usd=cost))


class StubOpus:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.state = "asking"
        self.refuse: str | None = None
        self.aborted = 0
        self.detached = 0

    def abort(self):
        self.aborted += 1
        self.state = "interrupted"

    def detach(self):
        self.detached += 1

    def attach(self, session_id):
        self.detached = 0

    def release(self):
        self.state = "interrupted"

    def start(self, text, plan):
        if self.refuse:
            raise HandoffRefused(self.refuse)
        self.started.append((text, plan))
        return "planner-1"

    def message(self, text):
        if self.refuse:
            raise HandoffRefused(self.refuse)
        self.messages.append(text)

    def live(self, session_id):
        return session_id == "planner-1" and self.state in ("busy", "asking", "ready", "staging")

    def waiting(self, session_id):
        return session_id == "planner-1" and self.state in ("asking", "ready")


class Env:
    def __init__(self, repo, chats=None):
        self.repo = repo
        self.chats = chats or ChatManager(repo, fake=True, fake_delay=0)
        self.opus = StubOpus()
        self.steps: list = []
        self.sessions: list[FakePlannerSession] = []
        self.orch = Orchestrator(repo, self.chats, self.opus, session_factory=self.factory)

    def factory(self, repo, progress):
        s = FakePlannerSession([self.steps.pop(0)])
        self.sessions.append(s)
        return s

    def say(self, text, step):
        self.steps.append(step)
        self.orch.message(text)
        wait_for(lambda: self.orch.view().state != "routing")

    def turns(self):
        return [e for e in read_planner_events(self.repo) if e.type == "orchestrator_turn"]

    def failures(self):
        return [e for e in read_planner_events(self.repo) if e.type == "orchestrator_failed"]

    def waiting_chat(self):
        return wait_for(lambda: all(v.state == "waiting" for v in self.chats.views()))


@pytest.fixture
def env(tmp_path):
    init_repo(tmp_path)
    e = Env(tmp_path)
    yield e
    e.orch.close()
    e.chats.close()


def test_new_message_to_haiku(env):
    env.say("  fix typo  ", route("haiku", reason="mechanical"))
    (turn,) = env.turns()
    p = turn.payload
    assert p["action"] == "start" and p["target_kind"] == "chat" and p["to"] == "haiku"
    assert p["routing_ms"] >= 0
    assert p["cost_usd"] == 0.02
    assert p["text"] == "Sent to haiku: mechanical"
    (session,) = env.sessions
    assert "Holder: nobody (new work)." in session.sent[0]
    assert session.sent[0].endswith("Message:\n\nfix typo")
    assert session.closed
    (chat,) = env.chats.views()
    assert chat.transcript[0].text == "fix typo"
    env.waiting_chat()
    view = env.orch.view()
    assert view.holder == "haiku" and view.holder_id == chat.chat_id
    assert [(e.role, e.who) for e in view.transcript] == [("user", "You"), ("agent", "haiku")]


def test_new_message_to_opus_quick(env):
    env.say("big job", route("opus", plan="quick", reason="hard"))
    assert env.opus.started == [("big job", "quick")]
    assert env.turns()[0].payload["text"] == "Sent to opus (quick plan): hard"
    assert env.orch.view().holder == "opus"


def test_opus_without_plan_defaults_to_quick(env):
    body = json.dumps({"to": "opus", "reason": "parallel one-stage work"})
    env.say("one stage", PlannerTurn(text=f"```json\n{body}\n```"))
    assert env.opus.started == [("one stage", "quick")]
    assert env.turns()[0].payload["plan"] == "quick"


def test_default_orchestrator_uses_lightweight_router(tmp_path):
    chats = ChatManager(tmp_path, fake=True, fake_delay=0)
    orch = Orchestrator(tmp_path, chats, StubOpus())
    try:
        session = orch._factory(tmp_path, lambda _info: None)
        assert isinstance(session, SdkRoutingSession)
        assert session.options().max_turns == 1
    finally:
        orch.close()
        chats.close()


def test_follow_up_forwarded_to_holder(env):
    env.say("do it", route("sonnet"))
    env.waiting_chat()
    env.say("and more", route("sonnet", reason="same"))
    p = env.turns()[1].payload
    assert p["action"] == "forward" and p["to"] == "sonnet"
    assert p["text"] == "Passed to sonnet."
    assert "Holder: sonnet." in env.sessions[1].sent[0]
    chat = env.chats.views()[0]
    wait_for(lambda: [e.text for e in env.chats.views()[0].transcript].count("and more") == 1)
    assert len(env.chats.views()) == 1 and chat.tier == "sonnet"


def test_easier_follow_up_switches_down_and_says_so(env):
    env.say("do it", route("sonnet"))
    env.waiting_chat()
    sonnet_id = env.chats.views()[0].chat_id
    env.say("what is 2+2", route("haiku", reason="a quick question"))
    p = env.turns()[1].payload
    assert p["action"] == "switch" and p["to"] == "haiku"
    assert p["text"] == "Switched down to haiku (medium effort): a quick question"
    by_id = {v.chat_id: v for v in env.chats.views()}
    assert by_id[sonnet_id].state == "closed"
    (new,) = [v for v in by_id.values() if v.chat_id != sonnet_id]
    assert new.tier == "haiku" and env.orch.view().holder_id == new.chat_id
    first = new.transcript[0].text
    assert first.startswith("You are taking over this work from sonnet")
    assert first.endswith("The user now says:\nwhat is 2+2")
    live = lambda: {v.chat_id: v.state for v in env.chats.views()}.get(new.chat_id)  # noqa: E731
    wait_for(lambda: live() == "waiting")
    lines = [e.text for e in env.orch.view().transcript if e.role == "orchestrator"]
    assert lines == [
        "Switched down to haiku (medium effort): a quick question"
    ]  # the first hand-off is quiet
    env.say("now the hard part", route("sonnet", reason="real coding"))
    assert env.turns()[2].payload["action"] == "escalate"
    lines = [e.text for e in env.orch.view().transcript if e.role == "orchestrator"]
    assert lines[-1] == "Moved up to sonnet (medium effort): real coding"


def test_escalate_sonnet_to_opus(env):
    env.say("do it", route("sonnet"))
    env.waiting_chat()
    sonnet_id = env.chats.views()[0].chat_id
    env.say("too hard", route("opus", plan="project", reason="beyond"))
    p = env.turns()[1].payload
    assert (
        p["action"] == "escalate"
        and p["text"] == "Moved up to opus (project plan, medium effort): beyond"
    )
    (text, plan) = env.opus.started[0]
    assert text.startswith("You are taking over this work from sonnet in the harness office.")
    assert "sonnet: Done (sonnet demo)" in text
    assert text.endswith("The user now says:\ntoo hard")
    assert plan == "project"
    assert next(v for v in env.chats.views() if v.chat_id == sonnet_id).state == "closed"
    assert env.orch.view().holder == "opus"


def test_no_route_for_new_work(env):
    env.say("hello", PlannerTurn(text="no json", usage=Usage(cost_usd=0.01)))
    p = env.turns()[0].payload
    assert p["to"] == "sonnet" and p["reason"] == NO_ROUTING_NEW
    assert env.chats.views()[0].tier == "sonnet"


def test_opus_holder(env):
    env.say("big", route("opus"))
    env.opus.state = "busy"
    with pytest.raises(OrchestratorConflict, match=OPUS_BUSY):
        env.orch.message("more")
    env.opus.state = "ready"
    env.orch.message("more")
    assert env.opus.messages == ["more"]
    assert len(env.sessions) == 1
    p = env.turns()[-1].payload
    assert p["action"] == "forward" and p["reason"] == OPUS_HOLDS and p["cost_usd"] == 0.0
    assert env.orch.view().state == "idle"
    env.opus.refuse = "opus is busy staging"
    with pytest.raises(OrchestratorConflict, match="opus is busy staging"):
        env.orch.message("again")
    view = env.orch.view()
    assert view.state == "failed" and "opus is busy staging" in (view.error or "")


def test_empty_and_too_long(env):
    with pytest.raises(OrchestratorConflict, match="the message is empty"):
        env.orch.message("   ")
    with pytest.raises(OrchestratorConflict, match="the message is too long"):
        env.orch.message("x" * 20_001)


class GatedSession:
    def __init__(self, gate: threading.Event, turn: PlannerTurn, wait: float = 1.0):
        self.gate, self.turn, self.wait = gate, turn, wait
        self.closed = False

    async def send(self, text):
        await asyncio.to_thread(self.gate.wait, self.wait)
        return self.turn

    async def close(self):
        self.closed = True


def test_second_message_while_routing(tmp_path):
    init_repo(tmp_path)
    chats = ChatManager(tmp_path, fake=True, fake_delay=0)
    gate = threading.Event()
    orch = Orchestrator(
        tmp_path, chats, StubOpus(), session_factory=lambda r, p: GatedSession(gate, route("haiku"))
    )
    try:
        orch.message("one")
        with pytest.raises(OrchestratorConflict, match=ROUTING_BUSY):
            orch.message("two")
        assert orch.view().state == "routing"
        gate.set()
        wait_for(lambda: orch.view().state == "idle")
    finally:
        gate.set()
        orch.close()
        chats.close()


def test_holder_chat_working(tmp_path):
    init_repo(tmp_path)
    gate = threading.Event()

    class Slow:
        async def send(self, text):
            await asyncio.to_thread(gate.wait, 3.0)
            return PlannerTurn(text="done")

        async def close(self):
            pass

    chats = ChatManager(tmp_path, session_factory=lambda r, t, p: Slow())
    e = Env(tmp_path, chats)
    try:
        e.say("go", route("haiku"))
        with pytest.raises(OrchestratorConflict, match="the haiku agent is still working"):
            e.orch.message("more")
    finally:
        gate.set()
        e.orch.close()
        chats.close()


def test_session_error_then_recover(env):
    env.say("x", PlannerTurn(error="boom", usage=Usage(cost_usd=0.03)))
    view = env.orch.view()
    assert view.state == "failed" and view.error == "orchestrator session failed: boom"
    env.say("y", route("haiku"))
    assert env.orch.view().state == "idle"


def test_refused_handoffs(env):
    env.opus.refuse = "no room"
    env.say("x", route("opus"))
    assert env.orch.view().error == "could not hand to opus: no room"
    env.opus.refuse = None
    assert env.failures()[-1].payload["cost_usd"] == 0.02
    other = env.chats.start("haiku", "already")
    env.say("y", route("haiku"))  # another haiku agent is open: this request gets its own
    view = env.orch.view()
    assert view.error is None and view.holder == "haiku" and view.holder_id != other


def test_new_request_detaches_a_working_chat_and_reopen_rejoins_it(tmp_path):
    init_repo(tmp_path)
    gate = threading.Event()

    class Slow(FakePlannerSession):
        async def send(self, text):
            await asyncio.to_thread(gate.wait, 5)
            return await super().send(text)

    chats = ChatManager(tmp_path, session_factory=lambda r, t, p: Slow([PlannerTurn(text="done")]))
    e = Env(tmp_path, chats)
    try:
        e.say("long job", route("haiku"))
        old_thread = e.orch.view().thread_id
        (old,) = chats.views()
        assert old.state == "working" and not old.detached
        e.orch.new_thread()
        (view,) = chats.views()
        assert view.state == "working" and view.detached  # still works, not in the room
        assert e.orch.view().holder is None
        e.say("next job", route("haiku"))  # the busy haiku agent is not reused
        new = next(v for v in chats.views() if v.chat_id != old.chat_id)
        assert e.orch.view().holder_id == new.chat_id and not new.detached
        e.orch.open_thread(old_thread)
        by_id = {v.chat_id: v for v in chats.views()}
        assert not by_id[old.chat_id].detached and by_id[new.chat_id].detached
        assert e.orch.view().holder_id == old.chat_id
    finally:
        gate.set()
        e.orch.close()
        chats.close()


def test_abort_stops_the_working_holder(tmp_path):
    init_repo(tmp_path)
    gate = threading.Event()

    class Stuck(FakePlannerSession):
        async def send(self, text):
            await asyncio.to_thread(gate.wait, 5)
            return await super().send(text)

    chats = ChatManager(tmp_path, session_factory=lambda r, t, p: Stuck([PlannerTurn(text="late")]))
    e = Env(tmp_path, chats)
    try:
        with pytest.raises(OrchestratorConflict, match="no agent is working"):
            e.orch.abort()
        e.say("hangs", route("sonnet"))
        cid = e.orch.view().holder_id
        e.orch.abort()
        assert e.orch.view().holder is None
        (view,) = chats.views()
        assert view.chat_id == cid and view.state == "closed"
        gate.set()
        time.sleep(0.1)
        (view,) = chats.views()
        assert [t.role for t in view.transcript] == ["user"]  # the late reply is dropped
        e.opus.state = "busy"
        e.say("plan it", route("opus"))
        e.orch.abort()
        assert e.opus.aborted == 1 and e.orch.view().holder is None
    finally:
        gate.set()
        e.orch.close()
        chats.close()


def test_abort_cancels_a_stuck_routing_turn(env):
    gate = threading.Event()

    class Stuck(FakePlannerSession):
        async def send(self, text):
            await asyncio.to_thread(gate.wait, 5)
            return await super().send(text)

    env.factory = lambda repo, progress: Stuck([route("haiku")])
    env.orch._factory = env.factory
    env.orch.message("route me")
    assert env.orch.view().state == "routing"
    env.orch.abort()
    view = env.orch.view()
    assert view.state == "failed" and view.error == "aborted by the user"
    gate.set()
    time.sleep(0.2)
    assert env.chats.views() == []  # the cancelled turn started nothing
    env.say("again", route("haiku"))
    assert env.orch.view().state == "idle" and env.orch.view().holder == "haiku"


def test_close_while_routing(tmp_path):
    init_repo(tmp_path)
    chats = ChatManager(tmp_path, fake=True, fake_delay=0)
    gate = threading.Event()
    orch = Orchestrator(
        tmp_path, chats, StubOpus(), session_factory=lambda r, p: GatedSession(gate, route("haiku"))
    )
    orch.message("one")
    wait_for(lambda: orch._task is not None)
    start = time.monotonic()
    orch.close()
    assert time.monotonic() - start < 5
    failed = [e for e in read_planner_events(tmp_path) if e.type == "orchestrator_failed"]
    assert [e.payload["reason"] for e in failed] == ["interrupted: server stopped"]
    assert len([e for e in read_planner_events(tmp_path) if e.type == "orchestrator_turn"]) == 0
    orch.close()
    chats.close()


def test_view_cost_sums(env):
    env.say("a", route("haiku", cost=0.02))
    env.waiting_chat()
    env.say("b", route("haiku", cost=0.05))
    assert env.orch.view().cost_usd == pytest.approx(0.07)


def test_titler_names_a_thread_once_on_its_first_message(tmp_path):
    init_repo(tmp_path)
    named: list[tuple[str, str]] = []
    env = Env(tmp_path)
    env.orch._titler = lambda repo, thread_id, text: named.append((thread_id, text))
    try:
        env.say(" fix typo ", route("haiku"))
        env.waiting_chat()
        env.say("and the other one", route("haiku"))
        wait_for(lambda: named)
        (thread_id, text) = named[0]
        assert len(named) == 1 and text == "fix typo"
        assert thread_id == env.orch.view().thread_id
    finally:
        env.orch.close()
        env.chats.close()
