"""The run-control routes over HTTP: stop, launch, options, stage, GET /api/plans and GET
/api/report/<run_id>. The office is fake and launches are recorded."""

import json
import signal
import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo
from test_office import Launches
from test_ui_office_server import Site, _snapshot, state, wait_for
from ui_fixtures import write_happy_run, write_running_run

from harness.planner import PROJECT_PLAN_FILE
from harness.state import load_state, run_dir, save_state
from harness.ui.demo import DRAFT_PLAN, demo_stage_plan
from harness.ui.office import Office
from harness.ui.runs import RUN_OPTIONS_FILE

STAGE1 = ".harness/plans/stage-1/plan.json"
OPTIONS = {"review": True, "max_parallel": 2, "max_cost": 1.5, "fake": True}


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


def _stage_plan(repo: Path, stage: int = 1, text: str | None = None) -> None:
    d = repo / ".harness" / "plans" / f"stage-{stage}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(text if text is not None else json.dumps(demo_stage_plan(stage)))


def _approve(repo: Path) -> None:
    path = repo / PROJECT_PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DRAFT_PLAN.format(repo=repo).replace("Status: draft", "Status: approved 2026-10-23"),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- stop


def test_stop_without_a_run_is_409(site):
    status, _, out = site.post("/api/run/stop", {})
    assert (status, out) == (409, {"error": "no run is active"})


def test_stop_signals_the_run_and_answers_200(site, tmp_path):
    proc = subprocess.Popen(["sleep", "60"])
    try:
        rid = write_running_run(tmp_path)
        s = load_state(run_dir(tmp_path, rid))
        s.pid = proc.pid
        save_state(run_dir(tmp_path, rid), s)

        status, _, out = site.post("/api/run/stop", {})

        assert status == 200 and out["ok"] is True
        assert out["office"]["stoppable_run"] in (rid, None)
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        proc.kill()
        proc.wait()


def test_stop_rejects_fields(site):
    status, _, out = site.post("/api/run/stop", {"x": 1})
    assert status == 400 and "no fields" in out["error"]


# ---------------------------------------------------------------- launch


def test_launch_runs_a_plan(site, tmp_path):
    _stage_plan(tmp_path)
    status, _, out = site.post("/api/run/launch", {"plan_path": STAGE1})
    assert status == 200
    assert out["office"]["state"] == "supervising"
    assert site.launches.made[0].argv[4] == STAGE1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"plan_path": 5},
        {"plan_path": ""},
        {"plan_path": "x" * 501},
        {"plan_path": STAGE1, "extra": 1},
        [],
    ],
)
def test_launch_bad_bodies_are_400(site, tmp_path, body):
    _stage_plan(tmp_path)
    before = _snapshot(tmp_path)
    status, _, out = site.post("/api/run/launch", body)
    assert status == 400 and "error" in out
    assert _snapshot(tmp_path) == before
    assert site.launches.made == []


def test_launch_conflicts_are_409(site, tmp_path):
    _stage_plan(tmp_path, text="{nope")
    status, _, out = site.post("/api/run/launch", {"plan_path": "nope/plan.json"})
    assert (status, out) == (409, {"error": "unknown plan: nope/plan.json"})
    status, _, out = site.post("/api/run/launch", {"plan_path": STAGE1})
    assert status == 409 and out["error"].startswith(f"plan {STAGE1} is invalid: ")
    assert site.launches.made == []


# ---------------------------------------------------------------- options


def test_options_are_stored_and_shown(site, tmp_path):
    status, _, out = site.post("/api/run/options", OPTIONS)
    assert status == 200
    assert out["office"]["run_options"] == OPTIONS
    assert out["office"]["options_summary"] == "review on · 2 parallel · cap $1.50 · fake workers"
    assert json.loads((tmp_path / RUN_OPTIONS_FILE).read_text()) == OPTIONS
    assert site.get_json("/api/office")["run_options"] == OPTIONS


@pytest.mark.parametrize(
    "body",
    [
        {**OPTIONS, "max_parallel": 0},
        {**OPTIONS, "max_parallel": 17},
        {**OPTIONS, "max_parallel": "2"},
        {**OPTIONS, "max_cost": 0},
        {**OPTIONS, "review": "yes"},
        {**OPTIONS, "extra": 1},
        {"review": True},
        {},
    ],
)
def test_bad_options_are_400(site, tmp_path, body):
    status, _, out = site.post("/api/run/options", body)
    assert status == 400 and out["error"]
    assert not (tmp_path / RUN_OPTIONS_FILE).exists()


def test_a_bad_value_names_the_option(site):
    _, _, out = site.post("/api/run/options", {**OPTIONS, "max_parallel": 0})
    assert out["error"].startswith("bad run options: max_parallel")


def test_max_cost_null_clears_the_cap(site):
    status, _, out = site.post("/api/run/options", {**OPTIONS, "max_cost": None})
    assert status == 200 and out["office"]["run_options"]["max_cost"] is None


def test_demo_office_refuses_real_workers(site):
    status, _, out = site.post("/api/run/options", {**OPTIONS, "fake": False})
    assert (status, out) == (409, {"error": "the demo office runs fake workers only"})


# ---------------------------------------------------------------- stage


def test_stage_writes_work_orders_and_launches(site, tmp_path):
    _approve(tmp_path)
    status, _, out = site.post("/api/planner/stage", {"stage": 1})
    assert status == 200 and out["office"]["state"] == "staging"
    wait_for(lambda: state(site) == "supervising")
    assert (tmp_path / STAGE1).is_file()
    assert site.launches.made[0].argv[4] == STAGE1


@pytest.mark.parametrize(
    "body", [{}, {"stage": 0}, {"stage": -1}, {"stage": "1"}, {"stage": True}, {"stage": 1.5}]
)
def test_stage_bad_bodies_are_400(site, tmp_path, body):
    _approve(tmp_path)
    status, _, out = site.post("/api/planner/stage", body)
    assert status == 400 and out["error"]
    assert state(site) == "idle"


def test_stage_conflict_is_409(site):
    status, _, out = site.post("/api/planner/stage", {"stage": 3})
    assert (status, out) == (409, {"error": "stage 3 has no heading or already has work orders"})


# ---------------------------------------------------------------- GETs


def test_get_plans(site, tmp_path):
    _approve(tmp_path)
    _stage_plan(tmp_path, 1)
    data = site.get_json("/api/plans")
    assert data["plan_status"] == "approved"
    assert data["unstaged"] == [2]
    (plan,) = data["plans"]
    assert plan["path"] == STAGE1 and plan["kind"] == "stage" and plan["stage"] == 1
    assert (plan["orders"], plan["waves"], plan["error"]) == (3, 2, None)


def test_get_plans_with_nothing(site):
    assert site.get_json("/api/plans") == {"plans": [], "unstaged": [], "plan_status": None}


def test_get_report(site, tmp_path):
    rid = write_happy_run(tmp_path)
    data = site.get_json(f"/api/report/{rid}")
    assert data["run_id"] == rid
    assert "done" in data["status"]
    assert "$" in data["report"]


def test_get_report_unknown_run_is_404(site):
    status, _, data = site.request("GET", "/api/report/nope")
    assert status == 404 and json.loads(data) == {"error": "unknown run"}


def test_the_gets_work_read_only(read_only_site, tmp_path):
    rid = write_happy_run(tmp_path)
    assert read_only_site.get_json("/api/plans")["plans"] == []
    assert read_only_site.get_json(f"/api/report/{rid}")["run_id"] == rid


# ---------------------------------------------------------------- safety


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/run/stop", {}),
        ("/api/run/launch", {"plan_path": STAGE1}),
        ("/api/run/options", OPTIONS),
        ("/api/planner/stage", {"stage": 1}),
    ],
)
def test_token_origin_and_content_type_are_checked(site, tmp_path, path, body):
    _stage_plan(tmp_path)
    _approve(tmp_path)
    before = _snapshot(tmp_path)
    assert site.post(path, body, token="wrong")[0] == 403
    assert site.post(path, body, token=None)[0] == 403
    assert site.post(path, body, origin="http://evil.example")[0] == 403
    assert site.post(path, body, origin=None)[0] == 403
    assert site.post(path, body, ctype="text/plain")[0] == 415
    assert site.post(path, None, raw=b"x" * 70000)[0] == 413
    assert site.post(path, None, raw=b"{nope")[0] == 400
    assert site.launches.made == []
    assert _snapshot(tmp_path) == before


def test_read_only_server_refuses_the_posts(read_only_site):
    for path in ("/api/run/stop", "/api/run/launch", "/api/run/options", "/api/planner/stage"):
        assert read_only_site.post(path, {})[0] == 405
