import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ui_fixtures import write_running_run

from harness.planner import PROJECT_PLAN_FILE
from harness.planner_log import read_planner_events
from harness.state import load_state, run_dir, save_state
from harness.ui.demo import DRAFT_PLAN, DemoPlannerSession, demo_stage_plan
from harness.ui.office import Office, OfficeConflict
from harness.ui.runs import RUN_OPTIONS_FILE, RunOptions

STAGE1 = ".harness/plans/stage-1/plan.json"
STAGE2 = ".harness/plans/stage-2/plan.json"


def wait_for(check, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


class FakePopen:
    def __init__(self, argv, **kwargs) -> None:
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4242

    def poll(self):
        return None


class Launches:
    def __init__(self) -> None:
        self.made: list[FakePopen] = []

    def __call__(self, argv, **kwargs) -> FakePopen:
        p = FakePopen(argv, **kwargs)
        self.made.append(p)
        return p


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


def _write_plan(repo: Path, stage: int, plan: dict | str | None = None) -> str:
    d = repo / ".harness" / "plans" / f"stage-{stage}"
    d.mkdir(parents=True, exist_ok=True)
    data = demo_stage_plan(stage) if plan is None else plan
    (d / "plan.json").write_text(data if isinstance(data, str) else json.dumps(data))
    return f".harness/plans/stage-{stage}/plan.json"


def _approve_project_plan(repo: Path) -> None:
    path = repo / PROJECT_PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = DRAFT_PLAN.format(repo=repo).replace("Status: draft", "Status: approved 2026-10-23")
    path.write_text(text, encoding="utf-8")


def _set_pid(repo: Path, run_id: str, pid: int | None) -> None:
    state = load_state(run_dir(repo, run_id))
    state.pid = pid
    save_state(run_dir(repo, run_id), state)


# ---------------------------------------------------------------- options and argv


def test_argv_has_flags_only_for_non_default_options(tmp_path, make_office):
    office = make_office(fake=False, session_factory=lambda r, p: DemoPlannerSession(r, p, 0))
    base = [
        sys.executable, "-m", "harness", "run", "p.json",
        "--repo", str(tmp_path.resolve()), "--run-id", "r1",
    ]  # fmt: skip
    assert office.run_argv("p.json", "r1") == base

    office.set_run_options(RunOptions(review=True, max_parallel=2, max_cost=1.5))
    assert office.run_argv("p.json", "r1") == [*base, "--review", "--max-parallel", "2",
                                               "--max-cost", "1.5"]  # fmt: skip

    office.set_run_options(RunOptions(fake=True))
    assert office.run_argv("p.json", "r1") == [*base, "--fake", "--fake-delay", "0"]


def test_options_default_from_constructor_and_survive_a_restart(tmp_path, make_office):
    office = make_office(fake=False, max_cost=3.0, session_factory=lambda r, p: None)
    assert office.run_options() == RunOptions(fake=False, max_cost=3.0)
    v = office.view()
    assert v.run_options == RunOptions(max_cost=3.0)
    assert v.options_summary == "review off · 4 parallel · cap $3.00 · real workers"

    office.set_run_options(RunOptions(review=True, max_parallel=8))
    assert (tmp_path / RUN_OPTIONS_FILE).is_file()
    office.close()
    again = make_office(fake=False, max_cost=3.0, session_factory=lambda r, p: None)
    assert again.run_options() == RunOptions(review=True, max_parallel=8)
    assert again.view().options_summary == "review on · 8 parallel · no cost cap · real workers"


def test_demo_office_runs_fake_workers_only(tmp_path, make_office):
    office = make_office()
    assert office.run_options().fake is True
    with pytest.raises(OfficeConflict, match="the demo office runs fake workers only"):
        office.set_run_options(RunOptions(fake=False))
    office.set_run_options(RunOptions(fake=True, review=True))
    assert office.run_options().review is True

    (tmp_path / RUN_OPTIONS_FILE).write_text('{"fake": false}')  # stored by a real office
    assert make_office().run_options().fake is True


# ---------------------------------------------------------------- stop


def test_stop_with_no_run_is_refused(make_office):
    office = make_office()
    assert office.view().stoppable_run is None
    with pytest.raises(OfficeConflict, match="^no run is active$"):
        office.stop_run()


def test_stop_signals_the_pid_of_the_live_run(tmp_path, make_office):
    proc = subprocess.Popen(["sleep", "60"])
    try:
        rid = write_running_run(tmp_path)
        _set_pid(tmp_path, rid, proc.pid)
        office = make_office()
        assert office.view().stoppable_run == rid

        office.stop_run()

        assert proc.wait(timeout=10) == -signal.SIGTERM
        stop = [e for e in read_planner_events(tmp_path) if e.type == "run_stop_requested"]
        assert [e.payload for e in stop] == [{"run_id": rid, "pid": proc.pid}]
    finally:
        proc.kill()
        proc.wait()


def test_stop_without_a_recorded_pid(tmp_path, make_office):
    rid = write_running_run(tmp_path)
    office = make_office()
    assert office.view().stoppable_run is None
    with pytest.raises(OfficeConflict, match=f"^run {rid} has no pid recorded$"):
        office.stop_run()


def test_stop_with_a_dead_pid(tmp_path, make_office):
    proc = subprocess.Popen(["true"])
    proc.wait()
    rid = write_running_run(tmp_path)
    _set_pid(tmp_path, rid, proc.pid)
    office = make_office()
    assert office.view().stoppable_run is None
    with pytest.raises(OfficeConflict, match="^no run is active$"):
        office.stop_run()


# ---------------------------------------------------------------- launch


def test_launch_plan_starts_a_run_with_the_stored_options(tmp_path, make_office):
    _write_plan(tmp_path, 1)
    launches = Launches()
    office = make_office(launcher=launches)
    office.set_run_options(RunOptions(fake=True, review=True, max_parallel=3))

    office.launch_plan(STAGE1)

    (popen,) = launches.made
    argv = popen.argv
    assert argv[4] == STAGE1
    assert "--review" in argv and argv[argv.index("--max-parallel") + 1] == "3"
    v = office.view()
    assert v.state == "supervising" and v.run_id == argv[argv.index("--run-id") + 1]
    launched = [e for e in read_planner_events(tmp_path) if e.type == "run_launched"]
    assert launched[-1].payload["plan_path"] == STAGE1


def test_launch_plan_refuses_unknown_and_invalid_plans(tmp_path, make_office):
    _write_plan(tmp_path, 1, "{not json")
    launches = Launches()
    office = make_office(launcher=launches)

    with pytest.raises(OfficeConflict, match="^unknown plan: .harness/plans/stage-9/plan.json$"):
        office.launch_plan(".harness/plans/stage-9/plan.json")
    with pytest.raises(
        OfficeConflict, match=r"^plan \.harness/plans/stage-1/plan\.json is invalid: "
    ):
        office.launch_plan(STAGE1)
    assert launches.made == []


def test_launch_plan_is_the_retry_and_refuses_while_a_run_is_live(
    tmp_path, make_office, monkeypatch
):
    ids_iter = iter(["r-one", "r-two"])
    monkeypatch.setattr("harness.ui.office.new_run_id", lambda: next(ids_iter))
    _write_plan(tmp_path, 1)
    launches = Launches()
    office = make_office(launcher=launches)
    office.launch_plan(STAGE1)
    with pytest.raises(OfficeConflict, match="a run is active"):
        office.launch_plan(STAGE1)

    rid = office.view().run_id
    assert rid is not None
    write_running_run(tmp_path, rid)  # the run starts...
    state = load_state(run_dir(tmp_path, rid))
    state.finished = True  # ...and stops with a task unfinished
    save_state(run_dir(tmp_path, rid), state)
    wait_for(lambda: office.view().state == "idle")

    office.launch_plan(STAGE1)

    assert len(launches.made) == 2
    ids = [p.argv[p.argv.index("--run-id") + 1] for p in launches.made]
    assert ids == ["r-one", "r-two"] and "--resume" not in launches.made[1].argv


def test_launch_plan_refused_while_planning(tmp_path, make_office):
    _write_plan(tmp_path, 1)
    office = make_office(launcher=Launches(), fake_delay=0.3)
    office.start("a demo")
    with pytest.raises(OfficeConflict, match="planning session is already in progress"):
        office.launch_plan(STAGE1)


# ---------------------------------------------------------------- stage


def _sessions(made: list[DemoPlannerSession]):
    def factory(repo, progress):
        s = DemoPlannerSession(repo, progress=progress, delay_s=0)
        made.append(s)
        return s

    return factory


def test_plan_stage_1_skips_the_project_turn_and_launches(tmp_path, make_office):
    _approve_project_plan(tmp_path)
    launches, made = Launches(), []
    office = make_office(launcher=launches, session_factory=_sessions(made))

    office.plan_stage(1)
    assert office.view().state == "staging"
    wait_for(lambda: office.view().state == "supervising")

    (session,) = made
    assert len(session.sent) == 1
    assert "plan-stage skill on stage 1" in session.sent[0]
    assert "plan-project" not in session.sent[0]
    assert (tmp_path / STAGE1).is_file()
    assert launches.made[0].argv[4] == STAGE1
    types = [e.type for e in read_planner_events(tmp_path)]
    assert "planner_user" not in types and types[-2:] == ["planner_done", "run_launched"]
    started = [e for e in read_planner_events(tmp_path) if e.type == "planner_started"][-1]
    assert started.payload["stage"] == 1


def test_plan_stage_2(tmp_path, make_office):
    _approve_project_plan(tmp_path)
    _write_plan(tmp_path, 1)
    launches, made = Launches(), []
    office = make_office(launcher=launches, session_factory=_sessions(made))

    office.plan_stage(2)
    wait_for(lambda: office.view().state == "supervising")

    assert "plan-stage skill on stage 2" in made[0].sent[0]
    assert launches.made[0].argv[4] == STAGE2


def test_plan_stage_refusals(tmp_path, make_office):
    office = make_office(launcher=Launches())
    gone = "^stage 1 has no heading or already has work orders$"
    with pytest.raises(OfficeConflict, match=gone):  # no project plan
        office.plan_stage(1)
    path = tmp_path / PROJECT_PLAN_FILE
    path.parent.mkdir(parents=True)
    path.write_text(DRAFT_PLAN.format(repo=tmp_path), encoding="utf-8")  # still a draft
    with pytest.raises(OfficeConflict, match=gone):
        office.plan_stage(1)
    _approve_project_plan(tmp_path)
    _write_plan(tmp_path, 1)
    with pytest.raises(OfficeConflict, match=gone):  # already has work orders
        office.plan_stage(1)
    with pytest.raises(OfficeConflict, match="^stage 7 has no heading"):
        office.plan_stage(7)


def test_plan_stage_refused_while_a_run_is_live(tmp_path, make_office):
    _approve_project_plan(tmp_path)
    write_running_run(tmp_path)
    office = make_office(launcher=Launches())
    with pytest.raises(OfficeConflict, match="a run is active"):
        office.plan_stage(1)
