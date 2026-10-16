import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from ui_fixtures import write_running_run

from harness.models import Event
from harness.planner import PROJECT_PLAN_FILE, PlannerTurn
from harness.planner_log import PLANNER_EVENTS, PlannerLog, read_planner_events
from harness.ui.demo import DemoPlannerSession
from harness.ui.office import (
    LOCK_FILE,
    Office,
    OfficeConflict,
    OfficeError,
    office_view_from_log,
)


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def state_is(office: Office, state: str):
    return lambda: office.view().state == state


class GateSession:
    """A planner session whose turn blocks until `gate` is set, then asks a question."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.closed = False
        self.sent: list[str] = []

    async def send(self, text: str) -> PlannerTurn:
        self.sent.append(text)
        await asyncio.to_thread(self.gate.wait, 5)
        return PlannerTurn(text="a question?")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def make_office(tmp_path):
    made: list[Office] = []

    def make(**kwargs) -> Office:
        kwargs.setdefault("fake", True)
        kwargs.setdefault("fake_delay", 0)
        office = Office(tmp_path, **kwargs)
        made.append(office)
        return office

    yield make
    for office in made:
        office.close()


def _types(repo: Path) -> list[str]:
    return [e.type for e in read_planner_events(repo)]


def test_start_ask_message_ready(tmp_path, make_office):
    office = make_office()
    assert office.view().state == "idle"
    assert office.view().can_click
    office.start("a demo project")
    wait_for(state_is(office, "asking"))
    v = office.view()
    assert v.mode == "interactive"
    assert "Two questions" in (v.reply or "")
    assert v.reply_blocks[0]["type"] == "paragraph"
    assert v.stage == 1
    assert v.session_id and v.session_id.startswith("planner-")
    assert v.progress == ["Glob: **/*.py", "Read: README.md", "Read: pyproject.toml"]
    assert not v.plan_exists
    office.message("my answers")
    wait_for(state_is(office, "ready"))
    v = office.view()
    assert "Draft written" in (v.reply or "")
    assert v.plan_exists and v.plan_status == "draft"
    assert v.cost_usd == pytest.approx(0.02)
    types = _types(tmp_path)
    assert types[0] == "planner_started"
    assert types.count("plan_ready") == 1
    started = read_planner_events(tmp_path)[0].payload
    assert started["source"] == "office"
    assert started["pid"] == os.getpid()
    assert started["description"] == "a demo project"
    office.message("tweak it")
    wait_for(lambda: _types(tmp_path).count("plan_ready") == 2)
    wait_for(state_is(office, "ready"))


def test_start_and_message_while_busy_conflict(make_office):
    gate = threading.Event()
    session = GateSession(gate)
    office = make_office(session_factory=lambda repo, progress: session)
    office.start("x")
    wait_for(lambda: session.sent)
    assert office.view().state == "busy"
    with pytest.raises(OfficeConflict, match="in progress"):
        office.start("y")
    with pytest.raises(OfficeConflict, match="not waiting"):
        office.message("hello")
    gate.set()
    wait_for(state_is(office, "asking"))


def test_start_validates_input(make_office):
    office = make_office()
    with pytest.raises(OfficeConflict, match="empty"):
        office.start("   ")
    with pytest.raises(OfficeConflict, match="no draft"):
        office.start(from_draft=True)
    with pytest.raises(OfficeConflict, match="too long"):
        office.start("x" * 20_001)
    assert office.view().state == "idle"


def test_idle_timeout_interrupts_the_session(tmp_path, make_office):
    sessions: list[DemoPlannerSession] = []

    def factory(repo, progress):
        s = DemoPlannerSession(repo, progress=progress, delay_s=0)
        sessions.append(s)
        return s

    office = make_office(idle_timeout_s=0.2, session_factory=factory)
    office.start("x")
    wait_for(state_is(office, "interrupted"))
    v = office.view()
    assert "Session closed after 0.2 s" in (v.error or "")
    assert sessions[0].closed
    last = read_planner_events(tmp_path)[-1]
    assert last.type == "planner_failed"
    assert last.payload["reason"] == "idle timeout"
    assert _types(tmp_path).count("planner_failed") == 1
    office.start("again")  # an interrupted boss can start over
    wait_for(state_is(office, "asking"))


def test_close_while_asking_joins_and_logs_interruption(tmp_path, make_office):
    sessions: list[DemoPlannerSession] = []

    def factory(repo, progress):
        s = DemoPlannerSession(repo, progress=progress, delay_s=0)
        sessions.append(s)
        return s

    office = make_office(session_factory=factory)
    office.open(1234)
    office.start("x")
    wait_for(state_is(office, "asking"))
    started = time.monotonic()
    office.close()
    assert time.monotonic() - started < 5
    assert sessions[0].closed
    events = read_planner_events(tmp_path)
    assert events[-1].type == "planner_failed"
    assert events[-1].payload["reason"] == "interrupted: server stopped"
    assert [e.type for e in events].count("planner_failed") == 1
    assert not (tmp_path / LOCK_FILE).exists()
    assert office.view().state == "interrupted"


def test_close_during_a_turn_cancels_and_joins(tmp_path, make_office):
    gate = threading.Event()
    session = GateSession(gate)
    office = make_office(session_factory=lambda repo, progress: session)
    office.start("x")
    wait_for(lambda: session.sent)
    threading.Timer(0.3, gate.set).start()
    office.close()
    assert session.closed
    assert read_planner_events(tmp_path)[-1].payload["reason"] == "interrupted: server stopped"


def test_second_open_on_the_same_repo_fails(tmp_path, make_office):
    first, second = make_office(), make_office()
    first.open(8765)
    with pytest.raises(OfficeError, match=r"Another harness ui \(pid \d+, port 8765\)"):
        second.open(9000)
    first.close()
    second.open(9000)  # free again
    held = json.loads((tmp_path / LOCK_FILE).read_text())
    assert held["port"] == 9000


def test_stale_lock_with_a_dead_pid_is_taken_over(tmp_path, make_office):
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    dead = int(done.stdout)
    lock = tmp_path / LOCK_FILE
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": dead, "port": 1, "started": 0}))
    office = make_office()
    office.open(8765)
    assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_live_run_blocks_start_and_message(tmp_path, make_office):
    office = make_office()
    write_running_run(tmp_path)
    v = office.view()
    assert v.state == "supervising"
    assert v.run_id == "running1"
    with pytest.raises(OfficeConflict, match="running1"):
        office.start("x")
    with pytest.raises(OfficeConflict, match="running1"):
        office.message("x")


def test_terminal_planner_blocks_the_office(tmp_path, make_office):
    log = PlannerLog(tmp_path, "planner-term")
    log.emit(
        "planner_started",
        {"source": "terminal", "pid": os.getpid(), "stage": 1, "description": "d"},
    )
    office = make_office()
    v = office.view()
    assert v.state == "terminal"
    assert not v.can_click
    with pytest.raises(OfficeConflict, match="terminal"):
        office.start("x")
    log.emit("planner_done", {"plan_path": "p", "cost_usd": 0, "rounds": 1})
    assert office.view().state == "idle"


def test_start_with_an_existing_plan_archives_it(tmp_path, make_office):
    plan = tmp_path / PROJECT_PLAN_FILE
    plan.parent.mkdir(parents=True)
    plan.write_text("# Old\n\nStatus: approved 2026-01-01\n")
    office = make_office()
    office.start("a new project")
    wait_for(state_is(office, "asking"))
    archived = list(plan.parent.glob("PROJECT_PLAN.*.md"))
    assert len(archived) == 1
    assert archived[0].read_text().startswith("# Old")
    assert not plan.exists()


def test_start_from_draft_resumes_with_the_original_description(tmp_path, make_office):
    sessions: list[DemoPlannerSession] = []

    def factory(repo, progress):
        s = DemoPlannerSession(repo, progress=progress, delay_s=0)
        sessions.append(s)
        return s

    office = make_office(session_factory=factory)
    office.start("build a todo app")
    wait_for(state_is(office, "asking"))
    office.message("answers")
    wait_for(state_is(office, "ready"))
    office.close()
    wait_for(state_is(office, "interrupted"))
    office2 = make_office(session_factory=factory)
    office2.start(from_draft=True)
    wait_for(state_is(office2, "asking"))
    sent = sessions[1].sent[0]
    assert "interrupted" in sent
    assert "build a todo app" in sent
    assert str(tmp_path.resolve() / PROJECT_PLAN_FILE) in sent
    started = [e for e in read_planner_events(tmp_path) if e.type == "planner_started"][-1]
    assert started.payload["resumed_from_draft"] is True
    assert started.payload["description"] == "build a todo app"
    assert (tmp_path / PROJECT_PLAN_FILE).is_file()  # not archived


# ---------------------------------------------------------------- the log-derived view


def _log(tmp_path: Path, *events: tuple[str, dict]) -> None:
    log = PlannerLog(tmp_path, "planner-x")
    for type_, payload in events:
        log.emit(type_, payload)


def _dead_pid() -> int:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    return int(done.stdout)


def test_view_from_log_with_no_log_is_idle(tmp_path):
    v = office_view_from_log(tmp_path)
    assert v.state == "idle" and v.mode == "read-only" and not v.can_click
    assert office_view_from_log(tmp_path, interactive=True).can_click


def test_view_from_log_unended_session_with_a_dead_pid_is_interrupted(tmp_path):
    _log(tmp_path, ("planner_started", {"source": "office", "pid": _dead_pid(), "stage": 1}))
    v = office_view_from_log(tmp_path, interactive=True)
    assert v.state == "interrupted"
    assert "draft is kept" in (v.error or "")


def test_view_from_log_failed_and_interrupted_endings(tmp_path):
    started = ("planner_started", {"source": "office", "pid": os.getpid(), "stage": 1})
    _log(tmp_path, started, ("planner_failed", {"reason": "boom", "cost_usd": 0}))
    v = office_view_from_log(tmp_path)
    assert (v.state, v.error) == ("failed", "boom")
    _log(tmp_path, started, ("planner_failed", {"reason": "interrupted: server stopped"}))
    assert office_view_from_log(tmp_path).state == "interrupted"
    _log(tmp_path, started, ("planner_failed", {"reason": "idle timeout", "idle_s": 7200}))
    v = office_view_from_log(tmp_path)
    assert v.state == "interrupted"
    assert v.error == "Session closed after 2 h without a message; the draft is kept."
    _log(tmp_path, started, ("planner_done", {"plan_path": "p", "cost_usd": 1, "rounds": 2}))
    assert office_view_from_log(tmp_path).state == "idle"


def test_view_from_log_live_office_session_asking_ready_busy(tmp_path):
    started = ("planner_started", {"source": "office", "pid": os.getpid(), "stage": 1})
    _log(tmp_path, started, ("planner_message", {"chars": 3, "phase": "project"}))
    _log(tmp_path, ("planner_progress", {"tool": "Read", "detail": "a.py"}))
    v = office_view_from_log(tmp_path)
    assert v.state == "busy"
    assert v.progress == ["Read: a.py"]
    _log(tmp_path, ("planner_reply", {"text": "**hi**", "plan_changed": False, "cost_usd": 0.5}))
    v = office_view_from_log(tmp_path)
    assert v.state == "asking"
    assert v.reply_blocks[0]["inline"] == [{"t": "strong", "text": "hi"}]
    assert v.cost_usd == 0.5
    _log(tmp_path, ("plan_ready", {"path": "p", "cost_usd": 0.5}))
    assert office_view_from_log(tmp_path).state == "ready"
    _log(tmp_path, ("planner_message", {"chars": 3, "phase": "staging"}))
    assert office_view_from_log(tmp_path).state == "staging"


def test_view_from_log_terminal_and_supervising(tmp_path):
    started = ("planner_started", {"source": "terminal", "pid": os.getpid(), "stage": 1})
    _log(tmp_path, started)
    v = office_view_from_log(tmp_path, interactive=True)
    assert v.state == "terminal" and not v.can_click
    write_running_run(tmp_path)
    v = office_view_from_log(tmp_path, interactive=True)
    assert v.state == "supervising" and v.run_id == "running1" and v.can_click


def test_planner_events_file_is_under_the_harness_dir(tmp_path):
    _log(tmp_path, ("planner_started", {"pid": 1}))
    assert (tmp_path / PLANNER_EVENTS).is_file()
    assert isinstance(read_planner_events(tmp_path)[0], Event)
