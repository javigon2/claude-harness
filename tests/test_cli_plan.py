import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import harness.cli
from harness.cli import app
from harness.planner import (
    PROJECT_PLAN_FILE,
    STAGE_PLAN_DIR,
    FakePlannerSession,
    PlannerError,
    PlannerTurn,
    stage_plan_dir,
)

runner = CliRunner()

PLAN = {
    "stage": "s",
    "orders": [
        {
            "id": "a",
            "title": "T",
            "goal": "G",
            "tier": "haiku",
            "depends_on": [],
            "files_owned": ["a.py"],
            "files_readonly": [],
            "context": [],
            "steps": ["s"],
            "constraints": [],
            "acceptance": ["true"],
            "budget": {"max_turns": 5, "max_attempts": 1},
            "out_of_scope": [],
        }
    ],
}


def _steps(repo: Path) -> list:
    project_plan = repo / PROJECT_PLAN_FILE

    def draft(_: str) -> PlannerTurn:
        project_plan.parent.mkdir(parents=True, exist_ok=True)
        project_plan.write_text("# Plan\n\nStatus: draft\n", encoding="utf-8")
        return PlannerTurn(text="Which stack do you want?")

    def approve(_: str) -> PlannerTurn:
        project_plan.write_text("# Plan\n\nStatus: approved 2026-10-14\n", encoding="utf-8")
        return PlannerTurn(text="Approved and saved.")

    def stage(_: str) -> PlannerTurn:
        d = repo / STAGE_PLAN_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        return PlannerTurn(text="Stage plan written.")

    return [draft, PlannerTurn(text="Here is the draft. Approve?"), approve, stage]


@pytest.fixture
def no_skill_check(monkeypatch):
    monkeypatch.setattr(harness.cli, "check_skills", lambda *a, **k: None)


def test_plan_happy_path(tmp_path, monkeypatch, no_skill_check):
    session = FakePlannerSession(_steps(tmp_path))
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(
        app, ["plan", "a todo app", "--repo", str(tmp_path)], input="defaults\napprove\n"
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Which stack do you want?" in out
    assert "Here is the draft. Approve?" in out
    assert "Stage plan written." in out
    plan_path = tmp_path.resolve() / STAGE_PLAN_DIR / "plan.json"
    assert f"Plan written to {plan_path}" in out
    assert "Planner cost: $0.0000 (" in out
    assert f"Next: harness run {plan_path} --repo {tmp_path.resolve()}" in out
    assert session.sent[1:3] == ["defaults", "approve"]
    assert session.closed
    assert runner.invoke(app, ["validate", str(plan_path)]).exit_code == 0


def test_plan_input_ends_early(tmp_path, monkeypatch, no_skill_check):
    session = FakePlannerSession(_steps(tmp_path))
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(app, ["plan", "a todo app", "--repo", str(tmp_path)], input="")
    assert result.exit_code == 1
    assert "aborted: no answer" in result.output
    assert session.closed


def test_plan_missing_skills(tmp_path, monkeypatch):
    session = FakePlannerSession(_steps(tmp_path))

    def boom(*a, **k):
        raise PlannerError("planner skills not found under X")

    monkeypatch.setattr(harness.cli, "check_skills", boom)
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(app, ["plan", "a todo app", "--repo", str(tmp_path)], input="")
    assert result.exit_code == 1
    assert "planner skills not found under X" in result.output
    assert session.sent == []
    assert not session.closed


def test_plan_missing_repo_dir(tmp_path, monkeypatch, no_skill_check):
    session = FakePlannerSession([])
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    missing = tmp_path / "nope"
    result = runner.invoke(app, ["plan", "a todo app", "--repo", str(missing)], input="")
    assert result.exit_code == 1
    assert "Not a directory: " in result.output
    assert session.sent == []


def test_plan_help_lists_description():
    result = runner.invoke(app, ["plan", "--help"])
    assert result.exit_code == 0
    assert "DESCRIPTION" in result.output.upper()


def test_plan_stage_2_rejects_description(tmp_path, monkeypatch, no_skill_check):
    session = FakePlannerSession([])
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(app, ["plan", "--stage", "2", "desc", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert (
        "Pass no description with --stage above 1: the approved project plan is the input."
        in result.output
    )
    assert session.sent == []


def test_plan_stage_1_needs_description(tmp_path, monkeypatch, no_skill_check):
    session = FakePlannerSession([])
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(app, ["plan", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "A project description is required for stage 1." in result.output
    assert session.sent == []


def test_plan_stage_2(tmp_path, monkeypatch, no_skill_check):
    project_plan = tmp_path / PROJECT_PLAN_FILE
    project_plan.parent.mkdir(parents=True, exist_ok=True)
    project_plan.write_text("# Plan\n\nStatus: approved 2026-10-14\n", encoding="utf-8")
    plan_path = tmp_path.resolve() / stage_plan_dir(2) / "plan.json"

    def stage(_: str) -> PlannerTurn:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(PLAN), encoding="utf-8")
        return PlannerTurn(text="Stage 2 written.")

    session = FakePlannerSession([stage])
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(app, ["plan", "--stage", "2", "--repo", str(tmp_path)], input="")
    assert result.exit_code == 0, result.output
    assert f"Plan written to {plan_path}" in result.output
    assert "stage 2 of" in session.sent[0]
    assert session.closed


def test_plan_excludes_harness_dir_in_git_repo(tmp_path, monkeypatch, no_skill_check):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    session = FakePlannerSession(_steps(tmp_path))
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(
        app, ["plan", "a todo app", "--repo", str(tmp_path)], input="defaults\napprove\n"
    )
    assert result.exit_code == 0, result.output
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".harness/" in exclude.splitlines()
