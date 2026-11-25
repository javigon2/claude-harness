import json

from typer.testing import CliRunner

from harness.cli import app
from harness.planner_log import PlannerLog

runner = CliRunner()


def _seed(repo, sid):
    log = PlannerLog(repo, sid)
    log.emit("planner_started", {"stage": 1})
    log.emit("planner_message", {"phase": "project"})
    log.emit("planner_reply", {"phase": "project", "duration_ms": 2000, "cost_usd": 0.3})


def test_markdown(tmp_path):
    _seed(tmp_path, "planner-a")
    res = runner.invoke(app, ["planning-report", "--repo", str(tmp_path)])
    assert res.exit_code == 0 and "## planner-a" in res.stdout


def test_json_and_last(tmp_path):
    _seed(tmp_path, "planner-a")
    _seed(tmp_path, "planner-b")
    res = runner.invoke(app, ["planning-report", "--repo", str(tmp_path), "--json", "--last", "1"])
    data = json.loads(res.stdout)
    assert [d["session_id"] for d in data] == ["planner-b"]


def test_unknown_session_and_empty_repo(tmp_path):
    _seed(tmp_path, "planner-a")
    res = runner.invoke(app, ["planning-report", "--repo", str(tmp_path), "--session", "x"])
    assert res.exit_code == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert runner.invoke(app, ["planning-report", "--repo", str(empty)]).exit_code == 1
