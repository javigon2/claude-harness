import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.worker import FakeWorker, WorkerResult

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


def _switch_to(repo: Path, branch: str) -> None:
    subprocess.run(["git", "switch", "-c", branch], cwd=repo, check=True, capture_output=True)


def test_run_with_branch_merges_into_checked_out_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _switch_to(repo, "dev")
    plan = _good_plan(tmp_path)
    result = runner.invoke(
        app,
        [
            "run",
            str(plan),
            "--fake",
            "--repo",
            str(repo),
            "--branch",
            "dev",
            "--run-id",
            "r1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Totals: 2 tasks: 2 done" in result.output
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "dev"


def test_run_without_branch_refuses_when_on_other_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _switch_to(repo, "dev")
    plan = _good_plan(tmp_path)
    result = runner.invoke(
        app,
        ["run", str(plan), "--fake", "--repo", str(repo), "--run-id", "r1"],
    )
    assert result.exit_code == 1, result.output
    assert "is on branch dev, not main; check out main first" in result.output


def test_run_rate_limited_on_branch_hint_includes_branch(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _switch_to(repo, "dev")
    plan = _good_plan(tmp_path)
    limited = WorkerResult(
        status="error", error="rate limited", rate_limited=True, rate_limit_resets_at=1760000000
    )
    monkeypatch.setattr("harness.cli._make_worker", lambda fake: FakeWorker(default=limited))
    result = runner.invoke(
        app,
        [
            "run",
            str(plan),
            "--fake",
            "--repo",
            str(repo),
            "--branch",
            "dev",
            "--run-id",
            "r1",
        ],
    )
    assert result.exit_code == 3, result.output
    assert result.output.endswith("--resume --branch dev\n")
