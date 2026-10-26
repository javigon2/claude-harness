"""The stage-2 routes over HTTP: the start route choice, the quick and direct lanes, the
resume endpoint and the kind of plan GET /api/plan answers. The office is fake and launches
are recorded, so no model or `harness run` is ever started."""

import json
from pathlib import Path

import pytest
from git_helpers import init_repo
from test_office import Launches
from test_ui_office_server import START, Site, _snapshot, state, wait_for

from harness.planner_log import read_planner_events
from harness.state import RunState, TaskState, run_dir, save_state
from harness.ui.demo import demo_stage_plan
from harness.ui.office import Office

ROUTE_ERROR = "route must be auto, quick or project"


@pytest.fixture
def site(tmp_path):
    init_repo(tmp_path)
    launches = Launches()
    office = Office(tmp_path, fake=True, fake_delay=0, launcher=launches)
    s = Site(tmp_path, office)
    s.launches = launches
    yield s
    s.stop()


@pytest.fixture
def read_only_site(tmp_path):
    s = Site(tmp_path, None)
    yield s
    s.stop()


def test_bad_route_or_extra_key_changes_nothing(site, tmp_path):
    before = _snapshot(tmp_path)
    for body in ({"description": "x", "route": "fast"}, {"description": "x", "route": 5}):
        status, _, out = site.post(START, body)
        assert status == 400
        assert out == {"error": ROUTE_ERROR}
    status, _, out = site.post(START, {"description": "x", "route": "auto", "extra": 1})
    assert status == 400
    assert "exactly one of" in out["error"]
    assert _snapshot(tmp_path) == before
    assert state(site) == "idle"


@pytest.mark.parametrize(
    "body",
    [{"description": "a todo app"}, {"description": "a todo app", "route": "project"}],
)
def test_start_without_a_route_or_with_project_is_the_project_flow(site, body):
    status, _, _ = site.post(START, body)
    assert status == 200
    wait_for(lambda: state(site) == "asking")
    started = [e for e in read_planner_events(site.repo) if e.type == "planner_started"]
    assert started[-1].payload["route"] == "project"


def test_quick_lane_serves_the_quick_plan_and_implements_it(site):
    status, _, _ = site.post(START, {"description": "quick add a flag", "route": "auto"})
    assert status == 200
    wait_for(lambda: state(site) == "ready")
    view = site.get_json("/api/office")
    assert view["route"] == "quick"
    assert view["plan_kind"] == "quick"
    assert view["triage_reason"] == "demo triage: quick"
    quick_plan = view["quick_plan"]
    assert quick_plan.startswith(".harness/plans/quick-")
    assert quick_plan.endswith("/plan.json")

    plan = site.get_json("/api/plan")
    assert plan["kind"] == "quick"
    assert plan["exists"] is True
    assert plan["status"] is None
    assert plan["blocks"][0]["type"] == "heading"
    assert "".join(s["text"] for s in plan["blocks"][0]["inline"]) == "Quick plan (demo)"

    status, _, _ = site.post("/api/planner/implement", {})
    assert status == 200
    wait_for(lambda: state(site) == "supervising")
    wait_for(lambda: len(site.launches.made) == 1)
    assert quick_plan in site.launches.made[-1].argv


def test_direct_lane_hands_off_to_a_haiku_chat(site):
    status, _, _ = site.post(START, {"description": "direct note this", "route": "auto"})
    assert status == 200
    wait_for(lambda: site.get_json("/api/office")["handoff_chat"] is not None)
    view = site.get_json("/api/office")
    chat = view["chats"][0]
    assert chat["tier"] == "haiku"
    assert chat["chat_id"] == view["handoff_chat"]


def test_run_route_refusals(site):
    status, _, out = site.post("/api/run/resume", {})
    assert status == 409
    assert out == {"error": "there is no stopped run to resume"}
    status, _, out = site.post("/api/run/resume", {"x": 1})
    assert status == 400
    assert out == {"error": "this endpoint takes no fields"}
    status, _, _ = site.post("/api/run/other", {})
    assert status == 404
    status, _, _ = site.request("GET", "/api/run/resume")
    assert status == 404


def _write_stopped_run(repo: Path) -> None:
    plan = repo / ".harness" / "plans" / "stage-1" / "plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps(demo_stage_plan(1)), encoding="utf-8")
    run_dir(repo, "r1").mkdir(parents=True, exist_ok=True)
    save_state(
        run_dir(repo, "r1"),
        RunState(
            run_id="r1",
            plan_path=".harness/plans/stage-1/plan.json",
            stage="s",
            finished=True,
            tasks={"demo-a": TaskState(task_id="demo-a", status="failed")},
        ),
    )


def test_resume_relaunches_the_stopped_run(site, tmp_path):
    _write_stopped_run(tmp_path)
    assert site.get_json("/api/office")["resumable_run"] == "r1"
    status, _, out = site.post("/api/run/resume", {})
    assert status == 200
    assert out["office"]["state"] == "supervising"
    assert out["office"]["run_id"] == "r1"
    argv = site.launches.made[-1].argv
    assert argv[-1] == "--resume"
    assert argv[argv.index("--run-id") + 1] == "r1"


def test_read_only_plan_is_the_project_plan(read_only_site):
    assert read_only_site.get_json("/api/plan")["kind"] == "project"
