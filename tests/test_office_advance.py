import threading

import pytest
from test_office import (
    Launches,
    _to_ready,
    _types,
    state_is,
    wait_for,
)
from test_office_chats import GatedChat
from ui_fixtures import write_blocked_run, write_running_run

from harness.models import TaskStatus
from harness.planner_log import read_planner_events
from harness.state import RunState, TaskState, run_dir, save_state
from harness.ui.office import (
    CHAT_WORKING,
    Office,
    OfficeConflict,
    office_view_from_log,
    resumable_run_of,
)

PLAN1 = ".harness/plans/stage-1/plan.json"


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


@pytest.fixture
def ids(monkeypatch):
    it = iter(["run1", "run2", "run3"])
    monkeypatch.setattr("harness.ui.office.new_run_id", lambda: next(it))


def finish(repo, rid, statuses, plan=PLAN1):
    tasks = {f"t{i}": TaskState(task_id=f"t{i}", status=s) for i, s in enumerate(statuses)}
    save_state(
        run_dir(repo, rid),
        RunState(run_id=rid, plan_path=plan, stage="s", finished=True, tasks=tasks),
    )


def launched(make_office, **kwargs):
    launches = Launches()
    office = make_office(launcher=launches, **kwargs)
    _to_ready(office)
    office.implement()
    wait_for(state_is(office, "supervising"))
    return office, launches


def test_all_done_advances_to_stage_two(tmp_path, make_office, ids):
    office, launches = launched(make_office)
    finish(tmp_path, "run1", ["done", "done"])
    wait_for(lambda: office.view() and office.view().state == "staging")
    started = [e for e in read_planner_events(tmp_path) if e.type == "planner_started"]
    assert started[-1].payload["auto"] is True
    assert started[-1].payload["stage"] == 2
    wait_for(lambda: len(launches.made) == 2)
    assert launches.made[1].argv[4] == ".harness/plans/stage-2/plan.json"


def test_auto_advance_off_stays_idle(tmp_path, make_office, ids):
    office, launches = launched(make_office, auto_advance=False)
    finish(tmp_path, "run1", ["done"])
    wait_for(state_is(office, "idle"))
    assert office.view().next_stage == 2
    assert _types(tmp_path).count("planner_started") == 1


def test_stopped_stage_reports_counts_and_is_resumable(tmp_path, make_office, ids):
    office, launches = launched(make_office)
    finish(tmp_path, "run1", ["done", "failed", "blocked", "paused"])
    wait_for(state_is(office, "idle"))
    v = office.view()
    assert v.error == "Stage 1 stopped: 1 done, 1 failed, 1 blocked, 1 paused."
    assert v.resumable_run == "run1"
    assert _types(tmp_path).count("planner_started") == 1


def test_chat_working_blocks_advance(tmp_path, make_office, ids, monkeypatch):
    office, launches = launched(make_office)
    monkeypatch.setattr(office.chats, "working", lambda: True)
    finish(tmp_path, "run1", ["done"])
    wait_for(state_is(office, "idle"))
    assert _types(tmp_path).count("planner_started") == 1


def test_resume_run_relaunches_and_advances(tmp_path, make_office, ids):
    office, launches = launched(make_office)
    finish(tmp_path, "run1", ["done", "failed"])
    wait_for(state_is(office, "idle"))
    office.resume_run()
    argv = launches.made[-1].argv
    assert argv[-1] == "--resume"
    assert argv[argv.index("--run-id") + 1] == "run1"
    assert read_planner_events(tmp_path)[-1].payload["resume"] is True
    assert office.view().state == "supervising"
    assert office.view().state == "supervising"
    finish(tmp_path, "run1", ["done", "done"])
    wait_for(lambda: office.view() and office.view().state == "staging")


def test_resume_run_conflicts(tmp_path, make_office, ids):
    office = make_office(launcher=Launches())
    with pytest.raises(OfficeConflict, match="there is no stopped run to resume"):
        office.resume_run()
    office.start("a demo")
    wait_for(state_is(office, "asking"))
    with pytest.raises(OfficeConflict, match="a planning session is already in progress"):
        office.resume_run()


def test_resume_run_refused_while_chat_works(tmp_path, make_office, ids):
    gate = threading.Event()
    office = make_office(
        launcher=Launches(),
        chat_session_factory=lambda repo, tier, progress: GatedChat(repo, gate),
    )
    write_blocked_run(tmp_path)
    office.chats.start("haiku", "x")
    wait_for(office.chats.working)
    with pytest.raises(OfficeConflict, match=CHAT_WORKING):
        office.resume_run()
    gate.set()
    wait_for(lambda: not office.chats.working())


def test_resumable_run_of(tmp_path):
    assert resumable_run_of(tmp_path) is None
    write_running_run(tmp_path)
    assert resumable_run_of(tmp_path) is None
    done: list[TaskStatus] = ["done"]
    finish(tmp_path, "zzdone", done)
    assert resumable_run_of(tmp_path) is None
    finish(tmp_path, "zzfail", ["done", "failed"])
    assert resumable_run_of(tmp_path) is None  # another run is live


def test_resumable_run_of_failed_run(tmp_path):
    write_blocked_run(tmp_path)
    assert resumable_run_of(tmp_path) == "blocked1"
    assert office_view_from_log(tmp_path).resumable_run == "blocked1"
