import json
from pathlib import Path

import pytest
from test_planner import (
    INVALID_PLAN,
    VALID_PLAN,
    asker,
    step,
    write_project,
)

from harness.config import PLANNER_ALL_OPUS_ENV
from harness.models import Usage
from harness.planner import (
    FALLBACK_SAY,
    MAX_FIX_ROUNDS,
    QUALITY_FIX_PROMPT,
    FakePlannerSession,
    PlannerTurn,
    run_planner,
)


@pytest.fixture(autouse=True)
def _sonnet_orders(monkeypatch):
    monkeypatch.delenv(PLANNER_ALL_OPUS_ENV, raising=False)


BAD_QUALITY = {
    "stage": "s",
    "orders": [{**VALID_PLAN["orders"][0], "files_readonly": ["missing.py"]}],
}


def write_quick(repo: Path, data) -> None:
    d = repo / ".harness" / "plans" / "quick-x"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(data))


def factory_for(session):
    tiers: list[str] = []

    def make(tier):
        tiers.append(tier)
        return session

    return make, tiers


async def run_staging(tmp_path: Path, opus, order, say=None):
    write_project(tmp_path, "approved 2026-10-14")
    make, tiers = factory_for(order)
    ask, _ = asker([])
    result = await run_planner(
        "",
        tmp_path,
        opus,
        ask,
        (say or [].append),
        stage=2,
        order_session_factory=make,
    )
    return result, tiers


def write2(repo, data):
    d = repo / ".harness" / "plans" / "stage-2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(data))


async def test_staging_passes_on_sonnet(tmp_path: Path):
    opus = FakePlannerSession([])
    order = FakePlannerSession([step("ok", 0.3, lambda: write2(tmp_path, VALID_PLAN))])
    said: list[str] = []
    result, tiers = await run_staging(tmp_path, opus, order, said.append)
    assert tiers == ["sonnet"]
    assert opus.sent == [] and len(order.sent) == 1
    assert order.closed and opus.closed
    assert result.order_usage.cost_usd == 0.3
    assert result.usage.cost_usd == 0.3
    assert not any("Falling back" in s for s in said)


async def test_quality_failure_fixed_on_same_session(tmp_path: Path):
    opus = FakePlannerSession([])
    order = FakePlannerSession(
        [
            step("bad", 0.1, lambda: write2(tmp_path, BAD_QUALITY)),
            step("fixed", 0.2, lambda: write2(tmp_path, VALID_PLAN)),
        ]
    )
    await run_staging(tmp_path, opus, order)
    assert len(order.sent) == 2 and "files_readonly missing.py" in order.sent[1]
    assert order.sent[1].startswith(QUALITY_FIX_PROMPT.split("{")[0])
    assert opus.sent == []


async def test_exhausted_fixes_fall_back_to_opus(tmp_path: Path):
    opus = FakePlannerSession([step("opus ok", 1.0, lambda: write2(tmp_path, VALID_PLAN))])
    order = FakePlannerSession(
        [
            step("bad", 0.1, lambda: write2(tmp_path, INVALID_PLAN))
            for _ in range(MAX_FIX_ROUNDS + 1)
        ]
    )
    said: list[str] = []
    result, _ = await run_staging(tmp_path, opus, order, said.append)
    assert order.closed
    assert len(order.sent) == MAX_FIX_ROUNDS + 1
    assert len(opus.sent) == 1
    assert any(s.startswith(FALLBACK_SAY.split("{")[0]) for s in said)
    assert result.usage.cost_usd == 1.0 + 0.1


async def test_session_error_falls_back(tmp_path: Path):
    opus = FakePlannerSession([step("opus ok", 1.0, lambda: write2(tmp_path, VALID_PLAN))])
    order = FakePlannerSession([PlannerTurn(error="boom", usage=Usage(cost_usd=0.05))])
    said: list[str] = []
    await run_staging(tmp_path, opus, order, said.append)
    assert len(opus.sent) == 1
    assert any("boom" in s for s in said if s.startswith("Falling back"))


async def test_without_factory_nothing_changes(tmp_path: Path):
    write_project(tmp_path, "approved 2026-10-14")
    session = FakePlannerSession([step("ok", 0.4, lambda: write2(tmp_path, VALID_PLAN))])
    ask, _ = asker([])
    result = await run_planner("", tmp_path, session, ask, lambda _t: None, stage=2)
    assert result.order_usage == Usage()
    assert "Repo digest" not in session.sent[0]


async def test_quick_route_uses_order_session(tmp_path: Path):
    from harness.planner import APPROVE_QUICK

    opus = FakePlannerSession([])
    order = FakePlannerSession([step("ok", 0.2, lambda: write_quick(tmp_path, VALID_PLAN))])
    make, tiers = factory_for(order)
    ask, _ = asker([APPROVE_QUICK])
    result = await run_planner(
        "do it",
        tmp_path,
        opus,
        ask,
        lambda _t: None,
        route="quick",
        quick_dir=Path(".harness/plans/quick-x"),
        order_session_factory=make,
    )
    assert tiers == ["sonnet"] and opus.sent == []
    assert result.route == "quick" and result.order_usage.cost_usd == 0.2
