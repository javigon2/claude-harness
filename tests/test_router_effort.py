"""The router picks a thinking effort as well as the model (plans/router-effort/PLAN.md):
effort is set when an agent starts, and a low <-> high jump on the same chat agent restarts
it on its own Claude session (retune)."""

import json
import threading
import time

import pytest
from git_helpers import init_repo

from harness.models import Usage
from harness.planner import (
    PlannerTurn,
    Routing,
    SdkPlannerSession,
    parse_routing,
)
from harness.planner_log import PlannerLog, read_planner_events
from harness.ui.chat import ChatConflict, ChatManager, SdkChatSession, read_chat_events
from harness.ui.orchestrator import (
    Orchestrator,
    big_effort_jump,
    orchestrator_view_from_log,
    routing_line,
)
from harness.ui.timing import build_thread_timing


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def route(to, effort=None, plan="project", reason="because"):
    data = {"to": to, "plan": plan, "reason": reason}
    if effort is not None:
        data["effort"] = effort
    return PlannerTurn(text=f"```json\n{json.dumps(data)}\n```", usage=Usage(cost_usd=0.01))


class Rec:
    """A chat session that records the effort and resume id it was started with."""

    def __init__(self, n: int, with_session: bool = True, gate: threading.Event | None = None):
        self.n = n
        self.effort = None
        self.resume_id = None
        self.sent: list[str] = []
        self.closed = False
        self.with_session = with_session
        self.gate = gate

    async def send(self, text):
        self.sent.append(text)
        if self.gate is not None:
            import asyncio

            await asyncio.to_thread(self.gate.wait, 5)
        sid = f"claude-{self.n}" if self.with_session else None
        return PlannerTurn(
            text=f"reply {len(self.sent)}", usage=Usage(cost_usd=0.01), session_id=sid
        )

    async def close(self):
        self.closed = True


def chats_with(repo, with_session=True, gate=None):
    sessions: list[Rec] = []

    def factory(r, tier, progress):
        s = Rec(len(sessions) + 1, with_session, gate)
        sessions.append(s)
        return s

    return ChatManager(repo, session_factory=factory), sessions


def chat_view(mgr, cid):
    return next(v for v in mgr.views() if v.chat_id == cid)


# -- routing schema --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "want"),
    [("low", "low"), ("high", "high"), ("medium", "medium"), (" HIGH ", "high"),
     (None, "medium"), ("max", "medium"), (3, "medium"), ("", "medium")],
)  # fmt: skip
def test_parse_routing_effort_falls_back_to_medium(given, want):
    data = {"to": "sonnet", "reason": "x"}
    if given is not None:
        data["effort"] = given
    routing = parse_routing(f"```json\n{json.dumps(data)}\n```")
    assert routing is not None and routing.effort == want


def test_missing_effort_is_medium():
    assert Routing(to="haiku").effort == "medium"
    assert parse_routing('```json\n{"to": "haiku"}\n```').effort == "medium"


# -- session options -------------------------------------------------------------------


def test_chat_and_planner_sessions_pass_effort(tmp_path):
    chat = SdkChatSession(tmp_path, "haiku")
    assert chat.options().effort is None  # SDK default until the manager sets it
    chat.effort = "low"
    assert chat.options().effort == "low"
    opus = SdkPlannerSession(tmp_path)
    opus.effort = "high"
    assert opus.options().effort == "high"


# -- ChatManager -----------------------------------------------------------------------


def test_start_effort_reaches_the_session_and_the_log(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr, sessions = chats_with(repo)
    try:
        cid = mgr.start("haiku", "hi", effort="low")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert sessions[0].effort == "low"
        started = next(e for e in read_chat_events(repo) if e.type == "chat_started")
        assert started.payload["effort"] == "low"
        assert chat_view(mgr, cid).effort == "low"
        default = mgr.start("sonnet", "hello")
        wait_for(lambda: chat_view(mgr, default).state == "waiting")
        assert sessions[1].effort == "medium"
    finally:
        mgr.close()


def test_retune_restarts_on_the_same_chat_and_claude_session(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr, sessions = chats_with(repo)
    try:
        cid = mgr.start("sonnet", "hi", effort="low")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert mgr.retune(cid, "high", "now debug it") is True
        wait_for(lambda: len(sessions) == 2 and sessions[1].sent)
        new = sessions[1]
        assert new.effort == "high" and new.resume_id == "claude-1"
        assert new.sent == ["now debug it"]
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert sessions[0].closed  # the old session thread stopped
        v = chat_view(mgr, cid)
        assert v.effort == "high" and v.chat_id == cid
        assert [t.text for t in v.transcript] == ["hi", "reply 1", "now debug it", "reply 1"]
        assert v.cost_usd == pytest.approx(0.02)  # earlier session's cost is kept
        retuned = [e for e in read_chat_events(repo) if e.type == "chat_retuned"]
        assert [e.payload for e in retuned] == [
            {"from": "low", "to": "high", "session_id": "claude-1"}
        ]
        assert not any(e.type == "chat_closed" for e in read_chat_events(repo))
    finally:
        mgr.close()


def test_retune_refused_while_working(tmp_path):
    repo = init_repo(tmp_path / "r")
    gate = threading.Event()
    mgr, sessions = chats_with(repo, gate=gate)
    try:
        cid = mgr.start("sonnet", "hi", effort="low")
        with pytest.raises(ChatConflict, match="still working"):
            mgr.retune(cid, "high", "more")
        wait_for(lambda: sessions)
        assert len(sessions) == 1
    finally:
        gate.set()
        mgr.close()


def test_retune_without_session_id_notes_and_keeps_effort(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr, sessions = chats_with(repo, with_session=False)
    try:
        cid = mgr.start("sonnet", "hi", effort="low")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert mgr.retune(cid, "high", "more") is False
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert len(sessions) == 1 and sessions[0].sent == ["hi", "more"]
        notes = [e.payload["text"] for e in read_chat_events(repo) if e.type == "chat_note"]
        assert notes == ["no saved session to retune; kept low effort"]
        assert chat_view(mgr, cid).effort == "low"
    finally:
        mgr.close()


def test_resume_keeps_the_logged_effort(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr, sessions = chats_with(repo)
    try:
        cid = mgr.start("haiku", "hi", effort="high")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        mgr.close_chat(cid)
        mgr.resume(cid, "again")
        wait_for(lambda: len(sessions) == 2 and sessions[1].sent)
        assert sessions[1].effort == "high" and sessions[1].resume_id == "claude-1"
    finally:
        mgr.close()


# -- Orchestrator ----------------------------------------------------------------------


class StubOpus:
    def __init__(self):
        self.started = []

    def start(self, text, plan, images=(), effort=None):
        self.started.append((plan, effort))
        return "planner-1"

    def message(self, text, images=()):
        pass

    def release(self):
        pass

    def detach(self):
        pass

    def live(self, session_id):
        return session_id == "planner-1"

    def waiting(self, session_id):
        return session_id == "planner-1"


class Env:
    def __init__(self, repo, with_session=True):
        self.repo = repo
        self.chats, self.chat_sessions = chats_with(repo, with_session)
        self.opus = StubOpus()
        self.steps: list = []
        self.router_prompts: list[str] = []
        self.orch = Orchestrator(repo, self.chats, self.opus, session_factory=self.factory)

    def factory(self, repo, progress):
        from harness.planner import FakePlannerSession

        env = self

        class Recording(FakePlannerSession):
            async def send(self, text):
                env.router_prompts.append(text)
                return await super().send(text)

        return Recording([self.steps.pop(0)])

    def say(self, text, step):
        self.steps.append(step)
        self.orch.message(text)
        wait_for(lambda: self.orch.view().state != "routing")
        wait_for(lambda: not self.chats.working())

    def turns(self):
        return [e.payload for e in read_planner_events(self.repo) if e.type == "orchestrator_turn"]

    def lines(self):
        return [e.text for e in self.orch.view().transcript if e.role == "orchestrator"]


@pytest.fixture
def env(tmp_path):
    init_repo(tmp_path)
    e = Env(tmp_path)
    yield e
    e.orch.close()
    e.chats.close()


def test_new_request_starts_the_agent_at_the_routed_effort(env):
    env.say("hello", route("haiku", "low"))
    (turn,) = env.turns()
    assert turn["action"] == "start" and turn["effort"] == "low"
    assert env.chat_sessions[0].effort == "low"
    assert "Holder's effort: none (new work)." in env.router_prompts[0]


def test_opus_start_gets_the_effort(env):
    env.say("plan it", route("opus", "high", plan="quick"))
    assert env.opus.started == [("quick", "high")]
    assert env.turns()[0]["effort"] == "high"


def test_one_step_change_forwards_without_retune(env):
    env.say("hello", route("sonnet", "low"))
    env.say("now code", route("sonnet", "medium"))
    p = env.turns()[1]
    assert p["action"] == "forward" and p["effort"] == "low"
    assert len(env.chat_sessions) == 1
    assert "Holder: sonnet." in env.router_prompts[1]
    assert "Holder's effort: low." in env.router_prompts[1]


def test_two_step_change_retunes_and_says_so(env):
    env.say("hello", route("sonnet", "low"))
    cid = env.orch.view().holder_id
    env.say("debug this race", route("sonnet", "high", reason="hard debugging"))
    p = env.turns()[1]
    assert p["action"] == "retune" and p["effort"] == "high" and p["target_id"] == cid
    assert p["text"] == "Effort raised to high: hard debugging"
    assert len(env.chat_sessions) == 2 and env.chat_sessions[1].effort == "high"
    assert env.chat_sessions[1].resume_id == "claude-1"
    assert env.lines() == ["Effort raised to high: hard debugging"]
    assert env.orch.view().holder_id == cid
    env.say("thanks", route("sonnet", "low", reason="small talk"))
    assert env.turns()[2]["text"] == "Effort lowered to low: small talk"
    assert env.chat_sessions[2].effort == "low"


def test_no_session_id_forwards_and_keeps_effort(tmp_path):
    init_repo(tmp_path)
    e = Env(tmp_path, with_session=False)
    try:
        e.say("hello", route("sonnet", "low"))
        e.say("debug", route("sonnet", "high"))
        p = e.turns()[1]
        assert p["action"] == "forward" and p["effort"] == "low"
        assert len(e.chat_sessions) == 1 and e.chat_sessions[0].sent[-1] == "debug"
    finally:
        e.orch.close()
        e.chats.close()


def test_switch_and_escalate_pass_and_name_the_effort(env):
    env.say("code", route("sonnet", "medium"))
    env.say("what is 2+2", route("haiku", "low", reason="quick question"))
    assert env.turns()[1]["text"] == "Switched down to haiku (low effort): quick question"
    assert env.chat_sessions[-1].effort == "low"
    env.say("design it", route("sonnet", "high", reason="design"))
    assert env.turns()[2]["text"] == "Moved up to sonnet (high effort): design"
    assert env.chat_sessions[-1].effort == "high"


def test_effort_is_restored_after_a_restart(env):
    env.say("hello", route("sonnet", "low"))
    again = Orchestrator(env.repo, env.chats, env.opus, session_factory=env.factory)
    try:
        again.restore()
        env.steps.append(route("sonnet", "high"))
        again.message("debug it")
        wait_for(lambda: again.view().state != "routing")
        assert env.turns()[-1]["action"] == "retune"
        assert "Holder's effort: low." in env.router_prompts[-1]
    finally:
        again.close()


def test_rules_and_lines():
    assert big_effort_jump("low", "high") and big_effort_jump("high", "low")
    assert not big_effort_jump("low", "medium") and not big_effort_jump("high", "high")
    assert routing_line("retune", "sonnet", "project", "r", "low", "high") == (
        "Effort lowered to low: r"
    )
    assert routing_line("escalate", "opus", "project", "r", "high") == (
        "Moved up to opus (project plan, high effort): r"
    )
    assert routing_line("start", "opus", "quick", "r", "high") == "Sent to opus (quick plan): r"
    assert routing_line("forward", "sonnet", "project", "r", "high") == "Passed to sonnet."


# -- Office (Opus) and Timing ----------------------------------------------------------


def test_office_opus_start_sets_effort_and_logs_it(tmp_path):
    from harness.ui.office import Office, _OfficeOpusDesk

    repo = init_repo(tmp_path)
    made: list[SdkPlannerSession] = []

    class Opusish:
        def __init__(self):
            self.effort = None
            self.resume_id = None

        async def send(self, text):
            return PlannerTurn(text="which one?", usage=Usage(cost_usd=0.01))

        async def close(self):
            pass

    def factory(r, p):
        s = Opusish()
        made.append(s)
        return s

    office = Office(repo, fake=True, fake_delay=0, session_factory=factory)
    try:
        _OfficeOpusDesk(office).start("plan a thing", "project", effort="high")
        wait_for(lambda: made)
        assert made[0].effort == "high"
        started = [e for e in read_planner_events(repo) if e.type == "planner_started"]
        assert started[-1].payload["effort"] == "high"
    finally:
        office.close()


def test_timing_exchange_has_the_effort(tmp_path):
    log = PlannerLog(tmp_path, "orch-1")
    log.emit("orchestrator_started", {"pid": 1})
    log.emit("orchestrator_user", {"text": "hi"})
    log.emit(
        "orchestrator_turn",
        {"action": "retune", "to": "sonnet", "effort": "high", "target_kind": "chat",
         "target_id": "chat-x", "routing_ms": 5, "text": "Effort raised to high: r"},
    )  # fmt: skip
    timing = build_thread_timing(tmp_path, "orch-1")
    assert timing is not None
    (ex,) = timing.exchanges
    assert (ex.to, ex.effort, ex.action) == ("sonnet", "high", "retune")
    view = orchestrator_view_from_log(tmp_path)
    assert [e.text for e in view.transcript if e.role == "orchestrator"] == [
        "Effort raised to high: r"
    ]
