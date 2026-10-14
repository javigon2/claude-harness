import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.worker import FakeWorker, WorkerResult, fake_result

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("line one\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _order(oid: str, depends_on: list[str], acceptance: list[str], max_attempts: int = 3) -> dict:
    return {
        "id": oid,
        "title": f"Task {oid}",
        "goal": f"Do {oid}",
        "tier": "haiku",
        "depends_on": depends_on,
        "files_owned": [f"{oid}.txt"],
        "files_readonly": [],
        "context": [],
        "steps": ["do it"],
        "constraints": [],
        "acceptance": acceptance,
        "budget": {"max_turns": 5, "max_attempts": max_attempts},
        "out_of_scope": [],
    }


def _write_plan(tmp_path: Path, name: str, orders: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"stage": "test", "orders": orders}))
    return path


def _good_plan(tmp_path: Path) -> Path:
    return _write_plan(
        tmp_path, "plan.json", [_order("a", [], ["true"]), _order("b", ["a"], ["true"])]
    )


def _failing_plan(tmp_path: Path) -> Path:
    return _write_plan(tmp_path, "fail.json", [_order("x", [], ["false"], max_attempts=2)])


def _events(repo: Path, rid: str) -> list[dict]:
    path = repo / ".harness" / "runs" / rid / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_validate_ok(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(_good_plan(tmp_path))])
    assert result.exit_code == 0, result.output
    assert result.output == "OK: 2 orders in 2 waves\nwave 1: a\nwave 2: b\n"


def test_validate_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
    assert "Cannot read plan: " in result.output


def test_validate_cycle(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path, "cycle.json", [_order("a", ["b"], ["true"]), _order("b", ["a"], ["true"])]
    )
    result = runner.invoke(app, ["validate", str(plan)])
    assert result.exit_code == 1
    assert "Invalid plan: " in result.output


def test_run_status_report_resume(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _good_plan(tmp_path)
    result = runner.invoke(app, ["run", str(plan), "--fake", "--repo", str(repo), "--run-id", "r1"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Run r1: 2 orders, state in ")
    assert "Per task" in result.output
    assert "Totals: 2 tasks: 2 done" in result.output
    run_dir = repo / ".harness" / "runs" / "r1"
    assert (run_dir / "state.json").exists()
    assert (run_dir / "events.jsonl").exists()

    for args in (["--run-id", "r1"], []):
        result = runner.invoke(app, ["status", "--repo", str(repo), *args])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        a_line = next(line for line in lines if line.startswith("a "))
        b_line = next(line for line in lines if line.startswith("b "))
        assert "done" in a_line
        assert "done" in b_line

    result = runner.invoke(app, ["report", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Per tier" in result.output

    before = [e for e in _events(repo, "r1") if e["type"] == "task_skipped"]
    result = runner.invoke(app, ["run", str(plan), "--resume", "--fake", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    after = [e for e in _events(repo, "r1") if e["type"] == "task_skipped"]
    assert len(after) - len(before) == 2


def test_run_failing_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _failing_plan(tmp_path)
    result = runner.invoke(app, ["run", str(plan), "--fake", "--repo", str(repo)])
    assert result.exit_code == 1, result.output
    assert "Needs attention" in result.output


def test_resume_with_no_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["run", str(_good_plan(tmp_path)), "--resume", "--fake", "--repo", str(repo)]
    )
    assert result.exit_code == 1
    assert "No run to resume in" in result.output


def test_status_with_no_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["status", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "No runs in" in result.output


def test_run_with_review_fake(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["run", str(_good_plan(tmp_path)), "--review", "--fake", "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    assert "Totals: 2 tasks: 2 done" in result.output


def test_unknown_review_tier(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app,
        ["run", str(_good_plan(tmp_path)), "--review-tier", "bogus", "--fake", "--repo", str(repo)],
    )
    assert result.exit_code == 1
    assert "Unknown tier: bogus" in result.output


def test_run_id_already_exists(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _good_plan(tmp_path)
    args = ["run", str(plan), "--fake", "--repo", str(repo), "--run-id", "dup"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, args)
    assert second.exit_code == 1
    assert "already exists" in second.output


def test_run_rate_limited_exits_3_with_resume_hint(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    plan = _good_plan(tmp_path)
    limited = WorkerResult(
        status="error", error="rate limited", rate_limited=True, rate_limit_resets_at=1760000000
    )
    monkeypatch.setattr("harness.cli._make_worker", lambda fake: FakeWorker(default=limited))
    result = runner.invoke(app, ["run", str(plan), "--fake", "--repo", str(repo), "--run-id", "r1"])
    assert result.exit_code == 3, result.output
    assert "1 paused" in result.output
    assert (
        "Run stopped: rate limited; resets at 2025-10-09T08:53:20+00:00. Resume with: "
        f"harness run {plan} --repo {repo.resolve()} --resume\n"
    ) in result.output


def test_run_max_cost_must_be_positive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["run", str(_good_plan(tmp_path)), "--fake", "--repo", str(repo), "--max-cost", "0"]
    )
    assert result.exit_code == 1
    assert "--max-cost must be greater than 0" in result.output


def test_run_max_cost_with_fake_that_costs_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["run", str(_good_plan(tmp_path)), "--fake", "--repo", str(repo), "--max-cost", "1"]
    )
    assert result.exit_code == 0, result.output


def test_run_max_cost_stops_with_exit_3(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    plan = _write_plan(
        tmp_path,
        "cost.json",
        [_order("a", [], ["true"]), _order("b", ["a"], ["true"]), _order("c", ["b"], ["true"])],
    )
    worker = FakeWorker(default=fake_result("done", cost_usd=0.5))
    monkeypatch.setattr("harness.cli._make_worker", lambda fake: worker)
    result = runner.invoke(
        app,
        ["run", str(plan), "--fake", "--repo", str(repo), "--run-id", "r1", "--max-cost", "0.7"],
    )
    # a and b run (0.5, then 1.0 > 0.7 after b); c stays pending, but the run was stopped.
    assert [c.order_id for c in worker.calls] == ["a", "b"]
    assert "budget_exceeded" in {e["type"] for e in _events(repo, "r1")}
    assert result.exit_code == 3, result.output
    assert (
        "Run stopped: estimated cost $1.00 passed the $0.70 cap. Resume with: "
        f"harness run {plan} --repo {repo.resolve()} --resume\n"
    ) in result.output
