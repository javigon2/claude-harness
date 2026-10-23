"""The whole office loop over HTTP: a real interactive server, the demo planner, and a real
`harness run --fake` subprocess on a git-inited temp repo. No model is called."""

import json
import subprocess

import pytest
from git_helpers import init_repo
from test_ui_office_server import START, Site, wait_for

from harness.planner_log import read_planner_events
from harness.ui.office import Office
from harness.worktree import ensure_excluded


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_excluded(repo)
    s = Site(repo, Office(repo, fake=True, fake_delay=0, auto_advance=False))
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


# ---------------------------------------------------------------- the three MVP lanes


@pytest.fixture
def lanes(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_excluded(repo)
    s = Site(repo, Office(repo, fake=True, fake_delay=0))
    yield s
    s.stop()


def office(site: Site) -> dict:
    return site.get_json("/api/office")


def launched_paths(site: Site) -> list[str]:
    return [
        e.payload["plan_path"] for e in read_planner_events(site.repo) if e.type == "run_launched"
    ]


def idle_on_stage(site: Site, stage: int) -> bool:
    o = office(site)
    return o["stage"] == stage and o["state"] == "idle"


def test_lane_direct_ends_in_a_haiku_chat_that_commits(lanes):
    site = lanes
    code, _, _ = site.post(START, {"description": "direct add a note", "route": "auto"})
    assert code == 200
    wait_for(lambda: office(site)["handoff_chat"], 10)
    o = office(site)
    assert o["state"] == "idle"
    assert o["route"] == "direct"
    assert o["run_id"] is None
    wait_for(lambda: office(site)["chats"] and office(site)["chats"][0]["state"] == "waiting", 10)
    o = office(site)
    chat = o["chats"][0]
    assert chat["chat_id"] == o["handoff_chat"]
    assert chat["tier"] == "haiku"
    assert [m["role"] for m in chat["transcript"]] == ["user", "agent"]
    assert chat["transcript"][1]["text"] == "Done (haiku demo): noted it in notes.md."
    assert len(chat["commits"]) == 1
    assert chat["commits"][0].endswith(" chat haiku: direct add a note")
    assert (site.repo / "notes.md").read_text() == "- direct add a note\n"
    out = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=site.repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "chat haiku: direct add a note"
    assert not any(e.type == "run_launched" for e in read_planner_events(site.repo))


def test_lane_quick_ends_in_a_finished_run(lanes):
    site = lanes
    code, _, _ = site.post(START, {"description": "quick add a flag", "route": "auto"})
    assert code == 200
    wait_for(lambda: office(site)["state"] == "ready", 10)
    o = office(site)
    assert o["route"] == "quick"
    quick_plan = o["quick_plan"]
    assert quick_plan.startswith(".harness/plans/quick-")
    assert site.post("/api/planner/implement", {})[0] == 200
    wait_for(lambda: office(site)["state"] == "supervising", 30)
    run_id = office(site)["run_id"]
    wait_for(lambda: office(site)["state"] == "idle", 60)
    view = site.get_json(f"/api/run/{run_id}")
    assert view["finished"] is True
    assert [t["status"] for t in view["tasks"]] == ["done", "done", "done"]
    assert launched_paths(site) == [quick_plan]
    assert office(site)["error"] is None


def test_lane_project_runs_two_stages_after_one_approval(lanes):
    site = lanes
    code, _, _ = site.post(START, {"description": "a todo app", "route": "auto"})
    assert code == 200
    wait_for(lambda: office(site)["state"] == "asking", 10)
    assert office(site)["route"] == "project"
    assert site.post("/api/planner/message", {"text": "answers"})[0] == 200
    wait_for(lambda: office(site)["state"] == "ready", 10)
    assert site.post("/api/planner/implement", {})[0] == 200
    wait_for(lambda: idle_on_stage(site, 2), 90)
    assert launched_paths(site) == [
        ".harness/plans/stage-1/plan.json",
        ".harness/plans/stage-2/plan.json",
    ]
    events = read_planner_events(site.repo)
    started = [
        (e.payload["stage"], e.payload["auto"]) for e in events if e.type == "planner_started"
    ]
    assert started == [(1, False), (2, True)]
    run_ids = site.get_json("/api/runs")["runs"][:2]
    assert len(run_ids) == 2
    for rid in run_ids:
        view = site.get_json(f"/api/run/{rid}")
        assert view["finished"] is True
        assert all(t["status"] == "done" for t in view["tasks"])
    o = office(site)
    assert o["error"] is None
    assert o["next_stage"] is None
    assert plan_ready_count(site) == 1
