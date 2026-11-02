import json
import time

import pytest
from git_helpers import init_repo

from harness.models import Usage
from harness.planner import PROJECT_PLAN_FILE, FakePlannerSession, PlannerTurn
from harness.planner_log import read_planner_events
from harness.ui.chat import RESUME_WINDOW_S, ChatConflict, ChatManager, chats_from_log
from harness.ui.orchestrator import Orchestrator, OrchestratorConflict, new_thread_id


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


class Remembering:
    """A chat session that reports a Claude session id and keeps what it was resumed from."""

    resume_id: str | None = None

    def __init__(self, n: int) -> None:
        self.n = n
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)
        return PlannerTurn(text="ok", usage=Usage(cost_usd=0.01), session_id=f"claude-{self.n}")

    async def close(self):
        pass


class StubOpus:
    def start(self, text, plan):
        return "planner-1"

    def message(self, text):
        pass

    def release(self):
        pass

    def live(self, session_id):
        return False

    def waiting(self, session_id):
        return False


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path)


@pytest.fixture
def stack(repo):
    sessions: list[Remembering] = []
    clock = [time.time()]

    def factory(r, tier, progress):
        s = Remembering(len(sessions) + 1)
        sessions.append(s)
        return s

    chats = ChatManager(repo, session_factory=factory, clock=lambda: clock[0])
    yield chats, sessions, clock
    chats.close()


def settled(chats, chat_id):
    return wait_for(
        lambda: next(
            (v for v in chats.views() if v.chat_id == chat_id and v.state == "waiting"), None
        )
    )


def test_resume_continues_the_same_claude_session(repo, stack):
    chats, sessions, _ = stack
    cid = chats.start("haiku", "first")
    settled(chats, cid)
    assert chats_from_log(repo)[0].session_id == "claude-1"
    chats.close_chat(cid)
    chats.resume(cid, "second")
    wait_for(lambda: len(sessions) == 2)
    view = settled(chats, cid)
    assert sessions[1].resume_id == "claude-1"
    assert [e.text for e in view.transcript if e.role == "user"] == ["first", "second"]


def test_resume_refused_after_two_days(repo, stack):
    chats, _, clock = stack
    cid = chats.start("haiku", "first")
    settled(chats, cid)
    chats.close_chat(cid)
    clock[0] += RESUME_WINDOW_S + 60
    with pytest.raises(ChatConflict, match="older than 2 days"):
        chats.resume(cid, "late")


def test_new_chat_is_not_blocked_by_a_released_one(repo, stack):
    chats, sessions, _ = stack
    steps = [PlannerTurn(text='```json\n{"to":"haiku","plan":"project","reason":"r"}\n```')] * 2
    orch = Orchestrator(
        repo, chats, StubOpus(), session_factory=lambda r, p: FakePlannerSession([steps.pop()])
    )
    try:
        orch.message("one")
        wait_for(lambda: orch.view().holder == "haiku" and orch.view().state == "idle")
        first = orch.view().holder_id
        settled(chats, first)
        orch.new_thread()
        orch.message("two")
        wait_for(lambda: orch.view().state == "idle" and orch.view().holder_id)
        assert orch.view().holder_id != first
        wait_for(lambda: len(sessions) == 2 and sessions[1].sent == ["two"])
    finally:
        orch.close()


def test_open_thread_resumes_its_agent_with_its_history(repo, stack):
    chats, sessions, _ = stack
    steps = [PlannerTurn(text='```json\n{"to":"haiku","plan":"project","reason":"r"}\n```')] * 2
    orch = Orchestrator(
        repo, chats, StubOpus(), session_factory=lambda r, p: FakePlannerSession([steps.pop()])
    )
    try:
        orch.message("one")
        wait_for(lambda: orch.view().holder == "haiku" and orch.view().state == "idle")
        old_thread, old_chat = orch.view().thread_id, orch.view().holder_id
        settled(chats, old_chat)
        orch.new_thread()
        wait_for(lambda: new_thread_id() != old_thread, timeout=3.0)  # ids change per second
        orch.message("other work")
        wait_for(lambda: orch.view().holder_id not in (None, old_chat))
        settled(chats, orch.view().holder_id)
        with pytest.raises(OrchestratorConflict, match="unknown request"):
            orch.open_thread("orch-nope")
        orch.open_thread(old_thread)
        assert orch.view().thread_id == old_thread
        assert [e.text for e in orch.view().transcript][0] == "one"
        orch.message("back again")
        wait_for(lambda: orch.view().holder_id == old_chat)
        wait_for(lambda: len(sessions) == 3)
        assert sessions[2].resume_id == "claude-1"
        turns = [
            e
            for e in read_planner_events(repo)
            if e.run_id == old_thread and e.type == "orchestrator_turn"
        ]
        assert turns[-1].payload["target_id"] == old_chat
        assert json.dumps(turns[-1].payload)
    finally:
        orch.close()


class Opusish:
    """A planner session that asks a question and reports a Claude session id."""

    resume_id: str | None = None

    def __init__(self, n: int) -> None:
        self.n = n
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)
        return PlannerTurn(
            text="which stack?", usage=Usage(cost_usd=0.1), session_id=f"opus-{self.n}"
        )

    async def close(self):
        pass


def test_opus_is_released_and_resumed_with_its_history(tmp_path):
    from harness.ui.office import Office

    init_repo(tmp_path)
    opus: list[Opusish] = []
    routes = [
        PlannerTurn(text='```json\n{"to":"opus","plan":"project","reason":"r"}\n```'),
        PlannerTurn(text='```json\n{"to":"haiku","plan":"project","reason":"r"}\n```'),
        PlannerTurn(text='```json\n{"to":"haiku","plan":"project","reason":"r"}\n```'),
    ]

    def opus_factory(r, p):
        s = Opusish(len(opus) + 1)
        opus.append(s)
        return s

    office = Office(
        tmp_path,
        fake=True,
        fake_delay=0,
        session_factory=opus_factory,
        orchestrator_session_factory=lambda r, p: FakePlannerSession([routes.pop(0)]),
    )
    orch = office.orchestrator
    try:
        orch.message("plan a big thing")
        wait_for(lambda: office.view().state == "asking")
        # what the real Opus does while it plans
        plan = tmp_path / PROJECT_PLAN_FILE
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("Status: draft\n", encoding="utf-8")
        old_thread, old_session = orch.view().thread_id, office.view().session_id
        wait_for(lambda: orch.view().state == "idle")
        wait_for(lambda: new_thread_id() != old_thread, timeout=3.0)

        orch.new_thread()  # releases the waiting Opus, so a new request is not blocked by it
        assert office.view().state == "interrupted"
        orch.message("small fix")
        wait_for(lambda: orch.view().state == "idle" and orch.view().holder == "haiku")
        wait_for(lambda: any(c.state == "waiting" for c in office.view().chats))

        orch.open_thread(old_thread)
        orch.message("back to the big thing")
        wait_for(lambda: len(opus) == 2)
        assert opus[1].resume_id == "opus-1"
        wait_for(lambda: opus[1].sent)
        assert "back to the big thing" in opus[1].sent[0]
        assert opus[1].sent[0].startswith("Continue the planning you were doing")
        assert office.view().session_id not in (None, old_session)
        turns = [
            e
            for e in read_planner_events(tmp_path)
            if e.run_id == old_thread and e.type == "orchestrator_turn"
        ]
        assert turns[-1].payload["target_kind"] == "opus"
        assert turns[-1].payload["target_id"] == office.view().session_id
    finally:
        office.close()
