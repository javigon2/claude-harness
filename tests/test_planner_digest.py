from pathlib import Path

import pytest
from test_planner import VALID_PLAN, asker, step, write_project
from test_planner_order_tier import factory_for, write2

import harness.planner as planner
from harness.config import PLANNER_ALL_OPUS_ENV
from harness.planner import FakePlannerSession, run_planner
from harness.repo_digest import Digest


@pytest.fixture(autouse=True)
def _sonnet_orders(monkeypatch):
    monkeypatch.delenv(PLANNER_ALL_OPUS_ENV, raising=False)


async def stage2(tmp_path: Path, order, said):
    write_project(tmp_path, "approved 2026-10-14")
    make, _ = factory_for(order)
    ask, _a = asker([])
    return await run_planner(
        "",
        tmp_path,
        FakePlannerSession([]),
        ask,
        said.append,
        stage=2,
        order_session_factory=make,
    )


async def test_prompt_has_digest_and_head(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(planner, "load_or_build", lambda repo: Digest("TREE-TEXT", "abc123", False))
    order = FakePlannerSession([step("ok", 0.1, lambda: write2(tmp_path, VALID_PLAN))])
    said: list[str] = []
    await stage2(tmp_path, order, said)
    assert "git HEAD abc123" in order.sent[0] and order.sent[0].endswith("TREE-TEXT")
    assert "Repo digest: rebuilt (1 lines)" in said


async def test_reused_line(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(planner, "load_or_build", lambda repo: Digest("T", "h", True))
    order = FakePlannerSession([step("ok", 0.1, lambda: write2(tmp_path, VALID_PLAN))])
    said: list[str] = []
    await stage2(tmp_path, order, said)
    assert "Repo digest: reused" in said


async def test_failing_digest_still_sends_prompt(tmp_path: Path, monkeypatch):
    def boom(repo):
        raise RuntimeError("no")

    monkeypatch.setattr(planner, "load_or_build", boom)
    order = FakePlannerSession([step("ok", 0.1, lambda: write2(tmp_path, VALID_PLAN))])
    await stage2(tmp_path, order, [])
    assert "Repo digest" not in order.sent[0] and "plan-stage" in order.sent[0]
