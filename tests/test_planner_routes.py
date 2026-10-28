import json
from pathlib import Path

import pytest
from test_planner import INVALID_PLAN, VALID_PLAN, asker, step, write_project

from harness.config import MODEL_IDS
from harness.models import Usage
from harness.planner import (
    APPROVE_QUICK,
    FIX_PROMPT,
    PLANS_DIR,
    PROJECT_PLAN_FILE,
    QUICK_PROMPT,
    STAGE_PLAN_DIR,
    TRIAGE_MAX_TURNS,
    TRIAGE_PROMPT,
    TRIAGE_TOOLS,
    FakePlannerSession,
    PlannerError,
    PlannerTurn,
    SdkTriageSession,
    Triage,
    parse_triage,
    quick_plan_dir,
    run_planner,
)


def triage_text(route: str, tier: str = "sonnet", reason: str = "r") -> str:
    data = {"route": route, "tier": tier, "reason": reason}
    return f"Routing.\n\n```json\n{json.dumps(data)}\n```"


def block(data: object) -> str:
    return f"```json\n{json.dumps(data)}\n```\n"


def write_plan(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_parse_triage_cases() -> None:
    ok = {"route": "quick", "tier": "haiku", "reason": "r"}
    assert parse_triage(block(ok)) == Triage(route="quick", tier="haiku", reason="r")
    assert parse_triage("no block") is None
    assert parse_triage(block({"route": "x"})) is None
    assert parse_triage(block({**ok, "extra": 1})) is None
    assert parse_triage(block({"route": "x"}) + block(ok)) == Triage(
        route="quick", tier="haiku", reason="r"
    )
    assert parse_triage(block(ok) + block({"route": "x"})) is None
    assert parse_triage(block({"route": "direct"})) == Triage(route="direct")
    assert Triage(route="direct").tier == "sonnet"
    assert Triage(route="direct").reason == ""


def test_quick_plan_dir() -> None:
    assert quick_plan_dir(1_800_000_000.0) == PLANS_DIR / "quick-20270115-080000"


def test_triage_options(tmp_path: Path) -> None:
    o = SdkTriageSession(tmp_path).options()
    assert o.model == MODEL_IDS["sonnet"]
    assert o.tools == list(TRIAGE_TOOLS) == ["Read", "Glob", "Grep"]
    assert o.allowed_tools == list(TRIAGE_TOOLS)
    assert o.skills == [] and o.setting_sources == []
    assert o.max_turns == TRIAGE_MAX_TURNS
    assert o.cwd == str(tmp_path.resolve())


async def test_auto_direct(tmp_path: Path) -> None:
    session = FakePlannerSession([])
    sonnet = FakePlannerSession([step(triage_text("direct", "haiku"), 0.1)])
    ask, asked = asker([])
    seen: list[Triage] = []
    result = await run_planner(
        "  fix it  ",
        tmp_path,
        session,
        ask,
        lambda _t: None,
        route="auto",
        on_triage=seen.append,
        triage_session=sonnet,
    )
    assert result.route == "direct"
    assert result.plan_path is None
    assert result.triage is not None and result.triage.tier == "haiku"
    assert result.rounds == 1
    assert result.usage.cost_usd == pytest.approx(0.1)
    assert sonnet.sent == [TRIAGE_PROMPT.format(repo=tmp_path.resolve(), description="fix it")]
    assert session.sent == []
    assert seen == [result.triage]
    assert asked == []
    assert session.closed and sonnet.closed


async def test_auto_needs_a_triage_session(tmp_path: Path) -> None:
    session = FakePlannerSession([])
    ask, _ = asker([])
    with pytest.raises(PlannerError, match="the auto route needs a triage session"):
        await run_planner("x", tmp_path, session, ask, lambda _t: None, route="auto")
    assert session.sent == []
    assert session.closed


@pytest.mark.parametrize(
    "triage_turn",
    [
        PlannerTurn(text="I do not know.", usage=Usage(cost_usd=0.1)),
        PlannerTurn(text="```json\n{not json\n```", usage=Usage(cost_usd=0.1)),
        PlannerTurn(text=triage_text("quick"), usage=Usage(cost_usd=0.1), error="boom"),
    ],
)
async def test_auto_no_route_falls_back_to_project(
    tmp_path: Path, triage_turn: PlannerTurn
) -> None:
    repo = tmp_path.resolve()
    sonnet = FakePlannerSession([triage_turn])
    session = FakePlannerSession(
        [
            step("Questions?", 0.1),
            step("Draft.", 0.1, lambda: write_project(repo, "draft")),
            step(
                "Stage.", 0.1, lambda: write_plan(repo / STAGE_PLAN_DIR / "plan.json", VALID_PLAN)
            ),
            step("Approved.", 0.1, lambda: write_project(repo, "approved 2026-10-14")),
        ]
    )
    ask, _ = asker(["defaults", "approve"])
    seen: list[Triage] = []
    result = await run_planner(
        "x",
        tmp_path,
        session,
        ask,
        lambda _t: None,
        route="auto",
        on_triage=seen.append,
        triage_session=sonnet,
    )
    expected = Triage(route="project", reason="triage gave no route; planning in full")
    assert seen == [expected]
    assert session.sent[0].startswith("Use the plan-project skill")
    assert result.route == "project"
    assert result.usage.cost_usd == pytest.approx(0.2)
    assert sonnet.closed
    assert result.triage == expected
    assert result.plan_path == repo / STAGE_PLAN_DIR / "plan.json"


async def test_auto_quick_flow(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    qdir = repo / ".harness/plans/quick-x"
    plan = qdir / "plan.json"
    sonnet = FakePlannerSession([step(triage_text("quick"), 0.02)])
    session = FakePlannerSession(
        [
            step("Plan written.", 0.1, lambda: write_plan(plan, INVALID_PLAN)),
            step("Fixed.", 0.2, lambda: write_plan(plan, VALID_PLAN)),
            step("Smaller.", 0.3),
        ]
    )
    ask, asked = asker(["make it smaller", APPROVE_QUICK])
    result = await run_planner(
        " do it ",
        tmp_path,
        session,
        ask,
        lambda _t: None,
        route="auto",
        quick_dir=Path(".harness/plans/quick-x"),
        triage_session=sonnet,
    )
    assert len(sonnet.sent) == 1
    assert session.sent[0] == QUICK_PROMPT.format(repo=repo, stage_dir=qdir, description="do it")
    assert session.sent[1].startswith(FIX_PROMPT.split("{")[0])
    assert session.sent[2] == "make it smaller"
    assert result.usage.cost_usd == pytest.approx(0.32)
    assert result.route == "quick"
    assert result.plan_path == plan
    assert result.rounds == 4
    assert result.triage == Triage(route="quick", reason="r")
    assert len(asked) == 2
    assert session.closed


async def test_quick_requires_plan_before_approval(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    session = FakePlannerSession(
        [
            step("Questions?", 0.1),
            step("Nothing yet.", 0.1),
            step(
                "Written.",
                0.1,
                lambda: write_plan(repo / PLANS_DIR / "quick-q" / "plan.json", VALID_PLAN),
            ),
        ]
    )
    ask, _ = asker([APPROVE_QUICK, "go", APPROVE_QUICK])
    result = await run_planner(
        "x", tmp_path, session, ask, lambda _t: None, route="quick", quick_dir=PLANS_DIR / "quick-q"
    )
    assert session.sent[1] == "Write the plan first."
    assert session.sent[2] == "go"
    assert result.triage is None
    assert result.route == "quick"
    assert result.project_plan == repo / PROJECT_PLAN_FILE
    assert not result.project_plan.exists()


async def test_quick_absolute_dir(tmp_path: Path) -> None:
    qdir = tmp_path / "elsewhere" / "q"
    session = FakePlannerSession(
        [step("Written.", 0.1, lambda: write_plan(qdir / "plan.json", VALID_PLAN))]
    )
    ask, _ = asker([APPROVE_QUICK])
    result = await run_planner(
        "x", tmp_path, session, ask, lambda _t: None, route="quick", quick_dir=qdir
    )
    assert result.plan_path == qdir / "plan.json"
    assert (
        QUICK_PROMPT.format(repo=tmp_path.resolve(), stage_dir=qdir, description="x")
        == (session.sent[0])
    )


@pytest.mark.parametrize("route", ["quick", "auto"])
async def test_non_project_route_needs_stage_1(tmp_path: Path, route: str) -> None:
    session = FakePlannerSession([])
    ask, _ = asker([])
    with pytest.raises(PlannerError, match=f"the {route} route needs stage 1"):
        await run_planner("x", tmp_path, session, ask, lambda _t: None, stage=2, route=route)  # type: ignore[arg-type]
    assert session.sent == []
    assert session.closed


async def test_quick_quit(tmp_path: Path) -> None:
    session = FakePlannerSession([step("Questions?", 0.1)])
    ask, _ = asker(["quit"])
    with pytest.raises(PlannerError, match="aborted by the user"):
        await run_planner("x", tmp_path, session, ask, lambda _t: None, route="quick")
    assert session.closed


async def test_quick_stays_invalid(tmp_path: Path) -> None:
    plan = tmp_path / PLANS_DIR / "quick-i" / "plan.json"

    def bad(n: int):
        def run(_msg: str) -> PlannerTurn:
            write_plan(plan, {**INVALID_PLAN, "stage": f"s{n}"})
            return PlannerTurn(text="bad", usage=Usage())

        return run

    session = FakePlannerSession([bad(1), bad(2), bad(3)])
    ask, _ = asker([])
    with pytest.raises(PlannerError, match="still invalid after 2 fixes"):
        await run_planner(
            "x",
            tmp_path,
            session,
            ask,
            lambda _t: None,
            route="quick",
            quick_dir=PLANS_DIR / "quick-i",
        )
    assert session.closed


async def test_explicit_project_route(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    session = FakePlannerSession(
        [
            step("Approved.", 0.1, lambda: write_project(repo, "approved 2026-10-14")),
            step(
                "Stage.", 0.1, lambda: write_plan(repo / STAGE_PLAN_DIR / "plan.json", VALID_PLAN)
            ),
        ]
    )
    ask, _ = asker([])
    result = await run_planner("x", tmp_path, session, ask, lambda _t: None, route="project")
    assert result.route == "project"
    assert result.triage is None
    assert result.rounds == 2
