import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from git_helpers import init_repo
from typer.testing import CliRunner
from ui_fixtures import write_running_run

from harness.cli import app
from harness.models import Event
from harness.planner import PROJECT_PLAN_FILE, PlannerTurn, is_approved
from harness.planner_log import PLANNER_EVENTS, PlannerLog, read_planner_events
from harness.state import latest_run, load_state, run_dir, save_state
from harness.ui.demo import DemoPlannerSession
from harness.ui.office import (
    LOCK_FILE,
    Office,
    OfficeConflict,
    OfficeError,
    office_view_from_log,
)
from harness.worktree import ensure_excluded


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


# ---------------------------------------------------------------- implement and the launch


class FakePopen:
    def __init__(self, argv, **kwargs) -> None:
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4242
        self.code: int | None = None

    def poll(self):
        return self.code


class Launches:
    def __init__(self) -> None:
        self.made: list[FakePopen] = []

    def __call__(self, argv, **kwargs) -> FakePopen:
        p = FakePopen(argv, **kwargs)
        self.made.append(p)
        return p


def _to_ready(office: Office) -> None:
    office.start("a demo")
    wait_for(state_is(office, "asking"))
    office.message("answers")
    wait_for(state_is(office, "ready"))


def test_implement_approves_stages_and_launches_with_fixed_argv(tmp_path, make_office):
    launches = Launches()
    office = make_office(fake_delay=0, launcher=launches)
    _to_ready(office)
    office.implement()
    wait_for(state_is(office, "supervising"))
    v = office.view()
    plan = (tmp_path / PROJECT_PLAN_FILE).read_text()
    today = time.strftime("%Y-%m-%d")
    assert f"\nStatus: approved {today}\n" in plan
    assert is_approved(plan)
    (popen,) = launches.made
    rid = v.run_id
    assert rid and v.state == "supervising"
    assert popen.argv == [
        sys.executable, "-m", "harness", "run", ".harness/plans/stage-1/plan.json",
        "--repo", str(tmp_path.resolve()), "--run-id", rid, "--fake", "--fake-delay", "0",
    ]  # fmt: skip
    assert popen.kwargs["cwd"] == tmp_path.resolve()
    assert popen.kwargs["start_new_session"] is True
    assert popen.kwargs["stdin"] == subprocess.DEVNULL
    assert (tmp_path / ".harness" / "office" / f"run-{rid}.log").is_file()
    types = _types(tmp_path)
    assert types[-2:] == ["planner_done", "run_launched"]
    launched = read_planner_events(tmp_path)[-1].payload
    assert launched == {
        "run_id": rid, "plan_path": ".harness/plans/stage-1/plan.json", "pid": 4242, "fake": True,
    }  # fmt: skip


def test_real_mode_argv_has_no_fake_flags_and_passes_max_cost(tmp_path, make_office):
    launches = Launches()

    def factory(repo, progress):
        return DemoPlannerSession(repo, progress=progress, delay_s=0)

    office = make_office(fake=False, session_factory=factory, max_cost=2.5, launcher=launches)
    _to_ready(office)
    office.implement()
    wait_for(state_is(office, "supervising"))
    argv = launches.made[0].argv
    assert "--fake" not in argv and "--fake-delay" not in argv
    assert argv[-2:] == ["--max-cost", "2.5"]


def test_second_implement_is_refused_while_staging_and_supervising(tmp_path, make_office):
    launches = Launches()
    office = make_office(fake_delay=0.3, launcher=launches)
    _to_ready(office)
    office.implement()
    assert office.view().state == "staging"
    with pytest.raises(OfficeConflict, match="already staging"):
        office.implement()
    with pytest.raises(OfficeConflict, match="not waiting"):
        office.message("hi")
    wait_for(state_is(office, "supervising"))
    for call in (office.implement, lambda: office.message("x"), lambda: office.start("x")):
        with pytest.raises(OfficeConflict, match="run is"):
            call()
    assert len(launches.made) == 1


def test_implement_needs_a_waiting_boss_and_a_draft(tmp_path, make_office):
    gate = threading.Event()
    office = make_office(session_factory=lambda repo, progress: GateSession(gate))
    with pytest.raises(OfficeConflict, match="not waiting"):
        office.implement()
    office.start("x")
    wait_for(lambda: office.view().state == "busy")
    with pytest.raises(OfficeConflict, match="not waiting"):
        office.implement()
    gate.set()
    wait_for(state_is(office, "asking"))
    with pytest.raises(OfficeConflict, match="Status: draft"):  # asking, but no plan file
        office.implement()
    plan = tmp_path / PROJECT_PLAN_FILE
    plan.parent.mkdir(parents=True)
    plan.write_text("# P\n\nStatus: approved 2026-01-01\n")
    with pytest.raises(OfficeConflict, match="Status: draft"):
        office.implement()
    assert plan.read_text() == "# P\n\nStatus: approved 2026-01-01\n"


def test_a_run_process_that_exits_without_finishing_returns_to_idle_with_an_error(
    tmp_path, make_office
):
    launches = Launches()
    office = make_office(launcher=launches)
    _to_ready(office)
    office.implement()
    wait_for(state_is(office, "supervising"))
    rid = office.view().run_id
    launches.made[0].code = 2
    v = office.view()
    assert v.state == "idle"
    assert v.error == f"run {rid} exited with code 2; see .harness/office/run-{rid}.log"
    assert v.run_id == rid
    office.start("next")  # the boss can plan again
    wait_for(state_is(office, "asking"))


def test_end_to_end_with_the_real_launcher_in_fake_mode(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_excluded(repo)
    office = Office(repo, fake=True, fake_delay=0)
    try:
        _to_ready(office)
        office.implement()
        wait_for(state_is(office, "supervising"), 20)
        rid = office.view().run_id
        wait_for(state_is(office, "idle"), 60)
        v = office.view()
        assert v.error is None, v.error
        assert v.run_id == rid
        state = load_state(run_dir(repo, rid))
        assert state.finished
        assert {t.status for t in state.tasks.values()} == {"done"}
        assert set(state.tasks) == {"demo-a", "demo-b", "demo-c"}
        out = CliRunner().invoke(app, ["status", "--repo", str(repo)])
        assert out.exit_code == 0, out.output
        assert rid in out.output or "demo-a" in out.output
        assert latest_run(repo) == rid
    finally:
        office.close()


# ---------------------------------------------------------------- restarts


def test_open_after_a_crash_shows_interrupted_with_the_draft_viewable(tmp_path, make_office):
    plan = tmp_path / PROJECT_PLAN_FILE
    plan.parent.mkdir(parents=True)
    plan.write_text("# Draft\n\nStatus: draft\n\n## Goal\n\nx\n")
    log = PlannerLog(tmp_path, "planner-old")
    log.emit(
        "planner_started",
        {"source": "office", "pid": _dead_pid(), "stage": 1, "description": "orig"},
    )
    log.emit("planner_reply", {"text": "**Draft**", "plan_changed": True, "cost_usd": 0.7})
    office = make_office()
    office.open(8765)
    v = office.view()
    assert v.state == "interrupted"
    assert v.plan_exists and v.plan_status == "draft"
    assert v.cost_usd == 0.7
    assert v.reply_blocks[0]["inline"] == [{"t": "strong", "text": "Draft"}]
    assert "draft is kept" in (v.error or "")
    last = read_planner_events(tmp_path)[-1]
    assert last.payload["reason"] == "interrupted: server restarted"
    second = make_office()  # a second restart does not log it again
    office.close()
    second.open(8765)
    assert _types(tmp_path).count("planner_failed") == 1
    assert second.view().state == "interrupted"


def test_open_with_a_failed_last_session_shows_failed(tmp_path, make_office):
    log = PlannerLog(tmp_path, "planner-old")
    log.emit("planner_started", {"source": "office", "pid": os.getpid(), "stage": 1})
    log.emit("planner_failed", {"reason": "planner session failed: boom", "cost_usd": 0.1})
    office = make_office()
    office.open(1)
    v = office.view()
    assert (v.state, v.error) == ("failed", "planner session failed: boom")
    office.start("again")  # failed boss starts over
    wait_for(state_is(office, "asking"))


def test_open_with_a_live_run_supervises_it_until_it_finishes(tmp_path, make_office):
    rid = write_running_run(tmp_path)
    office = make_office()
    office.open(8765)
    v = office.view()
    assert (v.state, v.run_id) == ("supervising", rid)
    assert v.can_click  # the modal shows the status
    state = load_state(run_dir(tmp_path, rid))
    state.finished = True
    save_state(run_dir(tmp_path, rid), state)
    v = office.view()
    assert v.state == "idle"
    assert v.error is None


def test_a_supervised_run_that_goes_quiet_returns_to_idle(tmp_path, make_office):
    rid = write_running_run(tmp_path)
    old = time.time() - 3600
    events = run_dir(tmp_path, rid) / "events.jsonl"
    os.utime(events, (old, old))
    office = make_office()
    office.open(8765)
    assert office.view().state == "idle"


def test_start_fresh_archives_the_old_plan_after_a_restart(tmp_path, make_office):
    plan = tmp_path / PROJECT_PLAN_FILE
    plan.parent.mkdir(parents=True)
    plan.write_text("# Old\n\nStatus: approved 2026-01-01\n")
    PlannerLog(tmp_path, "planner-old").emit(
        "planner_started", {"source": "office", "pid": _dead_pid(), "stage": 1}
    )
    office = make_office()
    office.open(1)
    assert office.view().state == "interrupted"
    office.start("brand new")
    wait_for(state_is(office, "asking"))
    assert not plan.exists()
    assert len(list(plan.parent.glob("PROJECT_PLAN.*.md"))) == 1


def test_transcript_follows_the_conversation(tmp_path, make_office):
    office = make_office()
    assert office.view().transcript == []
    office.start("a demo project")
    wait_for(state_is(office, "asking"))
    office.message("  my answers \n")
    wait_for(state_is(office, "ready"))
    tr = office.view().transcript
    assert [m.role for m in tr] == ["user", "opus", "user", "opus"]
    assert tr[0].text == "a demo project"
    assert tr[2].text == "my answers"
    assert all(m.blocks == [] for m in tr if m.role == "user")
    assert all(m.blocks for m in tr if m.role == "opus")
    assert office_view_from_log(tmp_path).transcript == tr


def test_planner_user_only_for_typed_text(tmp_path, make_office):
    office = make_office(launcher=Launches())
    _to_ready(office)
    office.implement()
    wait_for(state_is(office, "supervising"))
    assert _types(tmp_path).count("planner_user") == 2
    with pytest.raises(OfficeConflict):
        office.message("late")
    assert _types(tmp_path).count("planner_user") == 2


def test_transcript_of_a_terminal_session_has_only_replies(tmp_path):
    _log(
        tmp_path,
        ("planner_started", {"source": "terminal", "pid": os.getpid(), "stage": 1}),
        ("planner_message", {"chars": 3, "phase": "project"}),
        ("planner_reply", {"text": "hello", "plan_changed": False, "cost_usd": 0.1}),
        ("planner_reply", {"text": "", "plan_changed": False, "cost_usd": 0.1}),
    )
    tr = office_view_from_log(tmp_path).transcript
    assert [(m.role, m.text) for m in tr] == [("opus", "hello")]
