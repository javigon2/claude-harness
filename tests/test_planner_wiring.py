"""Order sessions are wired into `harness plan` and the office page."""

import json
from pathlib import Path

import pytest
from test_planner import VALID_PLAN, write_project
from typer.testing import CliRunner

import harness.cli
from harness.config import PLANNER_ALL_OPUS_ENV
from harness.models import Usage
from harness.planner import (
    FakePlannerSession,
    PlannerTurn,
    SdkPlannerSession,
)
from harness.planner_log import read_planner_events

runner = CliRunner()


def write_stage2(repo: Path) -> None:
    d = repo / ".harness" / "plans" / "stage-2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(VALID_PLAN))


def scripted(repo: Path) -> FakePlannerSession:
    def run(_msg: str) -> PlannerTurn:
        write_stage2(repo)
        return PlannerTurn(text="orders written", usage=Usage(cost_usd=0.1))

    return FakePlannerSession([run])


@pytest.fixture
def cli_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_project(tmp_path, "approved 2026-10-14")
    monkeypatch.setattr(harness.cli, "check_skills", lambda: None)
    monkeypatch.delenv(PLANNER_ALL_OPUS_ENV, raising=False)
    return tmp_path


def test_cli_staging_turn_goes_to_order_session(cli_repo, monkeypatch):
    opus = FakePlannerSession([])
    order = scripted(cli_repo)
    tiers: list[str] = []
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: opus)

    def make_order(repo, tier):
        tiers.append(tier)
        return order

    monkeypatch.setattr(harness.cli, "_make_order_session", make_order)
    res = runner.invoke(harness.cli.app, ["plan", "--stage", "2", "--repo", str(cli_repo)])
    assert res.exit_code == 0, res.output
    assert tiers == ["sonnet"] and opus.sent == [] and len(order.sent) == 1
    events = read_planner_events(cli_repo)
    msgs = [e for e in events if e.type == "planner_message"]
    assert [e.payload["phase"] for e in msgs] == ["staging"]


def test_cli_all_opus_never_builds_order_session(cli_repo, monkeypatch):
    monkeypatch.setenv(PLANNER_ALL_OPUS_ENV, "1")
    opus = scripted(cli_repo)
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: opus)

    def boom(repo, tier):
        raise AssertionError("order session must not be built")

    monkeypatch.setattr(harness.cli, "_make_order_session", boom)
    res = runner.invoke(harness.cli.app, ["plan", "--stage", "2", "--repo", str(cli_repo)])
    assert res.exit_code == 0, res.output
    assert len(opus.sent) == 1


def test_make_order_session_tier(tmp_path: Path):
    s = harness.cli._make_order_session(tmp_path, "sonnet")
    assert isinstance(s, SdkPlannerSession) and s.tier == "sonnet"


# ---------------------------------------------------------------- office


def _office_pieces(tmp_path: Path, order_steps):
    from test_office import Launches, wait_for
    from test_office_run_control import STAGE1, _approve_project_plan

    from harness.ui.office import Office

    _approve_project_plan(tmp_path)
    opus = FakePlannerSession([])
    order = FakePlannerSession(order_steps)
    launches = Launches()
    office = Office(
        tmp_path,
        fake=True,
        fake_delay=0,
        session_factory=lambda _r, _p: opus,
        order_session_factory=lambda _r, _p: order,
        launcher=launches,
    )
    return office, opus, order, wait_for, STAGE1


def test_office_staging_turn_goes_to_order_session(tmp_path, monkeypatch):
    monkeypatch.delenv(PLANNER_ALL_OPUS_ENV, raising=False)

    def write(_msg: str) -> PlannerTurn:
        d = tmp_path / ".harness" / "plans" / "stage-1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan.json").write_text(json.dumps(VALID_PLAN))
        return PlannerTurn(text="orders", usage=Usage(cost_usd=0.1))

    office, opus, order, wait_for, _ = _office_pieces(tmp_path, [write])
    try:
        office.plan_stage(1)
        wait_for(lambda: office.view().state == "supervising")
    finally:
        office.close()
    assert opus.sent == [] and len(order.sent) == 1
    msgs = [e for e in read_planner_events(tmp_path) if e.type == "planner_message"]
    assert [e.payload["phase"] for e in msgs] == ["staging"]


def test_office_all_opus_skips_order_session(tmp_path, monkeypatch):
    monkeypatch.setenv(PLANNER_ALL_OPUS_ENV, "1")

    def write(_msg: str) -> PlannerTurn:
        d = tmp_path / ".harness" / "plans" / "stage-1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan.json").write_text(json.dumps(VALID_PLAN))
        return PlannerTurn(text="orders", usage=Usage(cost_usd=0.1))

    office, opus, order, wait_for, _ = _office_pieces(tmp_path, [])
    opus._steps.append(write)  # the Opus fake writes the orders instead
    try:
        office.plan_stage(1)
        wait_for(lambda: office.view().state == "supervising")
    finally:
        office.close()
    assert order.sent == [] and len(opus.sent) == 1
