"""The whole office loop over HTTP: a real interactive server, the demo planner, and a real
`harness run --fake` subprocess on a git-inited temp repo. No model is called."""

import json

import pytest
from git_helpers import init_repo
from test_ui_office_server import Site, wait_for

from harness.planner_log import read_planner_events
from harness.ui.office import Office
from harness.worktree import ensure_excluded


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_excluded(repo)
    s = Site(repo, Office(repo, fake=True, fake_delay=0))
    yield s
    s.stop()


def state(site: Site) -> str:
    return site.get_json("/api/office")["state"]


def plan_ready_count(site: Site) -> int:
    return sum(1 for e in read_planner_events(site.repo) if e.type == "plan_ready")


def test_the_full_loop_to_a_finished_run(site):
    code, _, body = site.post("/api/planner/start", {"description": "a todo app"})
    assert code == 200
    wait_for(lambda: state(site) == "asking")
    assert site.post("/api/planner/message", {"text": "answers"})[0] == 200
    wait_for(lambda: state(site) == "ready")
    assert plan_ready_count(site) == 1

    assert site.post("/api/planner/message", {"text": "add a risk section"})[0] == 200
    wait_for(lambda: plan_ready_count(site) == 2)
    wait_for(lambda: state(site) == "ready")
    assert "## Revision 1" in (site.repo / ".harness/plans/PROJECT_PLAN.md").read_text()

    code, _, body = site.post("/api/planner/implement", {})
    assert code == 200
    wait_for(lambda: state(site) in ("staging", "supervising"))
    wait_for(lambda: state(site) == "supervising", 30)
    run_id = site.get_json("/api/office")["run_id"]
    assert run_id

    code, _, body = site.post("/api/planner/implement", {})
    assert code == 409
    assert "run is already active" in body["error"]
    assert site.post("/api/planner/message", {"text": "x"})[0] == 409
    assert site.post("/api/planner/start", {"description": "x"})[0] == 409

    wait_for(lambda: state(site) == "idle", 60)
    office = site.get_json("/api/office")
    assert office["error"] is None
    assert office["run_id"] == run_id
    assert office["plan_status"] == "approved"

    runs = site.get_json("/api/runs")
    assert run_id in runs["runs"]
    view = site.get_json(f"/api/run/{run_id}")
    assert view["finished"] is True
    assert [t["status"] for t in view["tasks"]] == ["done", "done", "done"]
    assert [t["task_id"] for t in view["tasks"]] == ["demo-a", "demo-b", "demo-c"]

    types = [e.type for e in read_planner_events(site.repo)]
    assert types[0] == "planner_started"
    assert types[-2:] == ["planner_done", "run_launched"]
    json.dumps(office)


def _stage_one_run(site: Site) -> str:
    site.post("/api/planner/start", {"description": "a todo app"})
    wait_for(lambda: state(site) == "asking")
    site.post("/api/planner/message", {"text": "answers"})
    wait_for(lambda: state(site) == "ready")
    assert site.post("/api/planner/implement", {})[0] == 200
    wait_for(lambda: state(site) == "supervising", 30)
    run_id = site.get_json("/api/office")["run_id"]
    wait_for(lambda: state(site) == "idle", 60)
    return run_id


def test_plan_next_stage_then_no_more_stages(site):
    assert site.post("/api/planner/next-stage", {})[0] == 409  # nothing planned yet
    first = _stage_one_run(site)
    office = site.get_json("/api/office")
    assert office["next_stage"] == 2
    assert office["error"] is None

    code, _, body = site.post("/api/planner/next-stage", {})
    assert code == 200
    assert body["office"]["state"] == "staging"
    assert body["office"]["stage"] == 2
    wait_for(lambda: state(site) == "supervising", 30)
    second = site.get_json("/api/office")["run_id"]
    assert second != first
    code, _, body = site.post("/api/planner/next-stage", {})
    assert code == 409
    assert "run is active" in body["error"]
    wait_for(lambda: state(site) == "idle", 60)

    view = site.get_json(f"/api/run/{second}")
    assert view["stage"] == "Stage 2: Demo polish"
    assert [t["task_id"] for t in view["tasks"]] == ["demo-s2-a", "demo-s2-b"]
    assert [t["status"] for t in view["tasks"]] == ["done", "done"]
    assert site.get_json("/api/runs")["runs"][:2] == [second, first]
    office = site.get_json("/api/office")
    assert office["next_stage"] is None
    assert site.post("/api/planner/next-stage", {})[0] == 409

    paths = [
        e.payload["plan_path"] for e in read_planner_events(site.repo) if e.type == "run_launched"
    ]
    assert paths == [".harness/plans/stage-1/plan.json", ".harness/plans/stage-2/plan.json"]


def test_next_stage_offer_is_derived_from_files_too(site, tmp_path):
    _stage_one_run(site)
    # a fresh Office on the same repo (as after a restart) derives the same offer from files
    from harness.ui.office import office_view_from_log

    assert office_view_from_log(site.repo).next_stage == 2
