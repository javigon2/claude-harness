import json
from pathlib import Path

import pytest
from git_helpers import init_repo
from test_office import Launches, wait_for
from test_planner import VALID_PLAN

from harness.models import Usage
from harness.planner import (
    PROJECT_PLAN_FILE,
    STAGE_PLAN_DIR,
    FakePlannerSession,
    PlannerTurn,
    quick_plan_dir,
)
from harness.planner_log import read_planner_events
from harness.ui.office import Office, OfficeConflict, office_view_from_log

NOW = 1_800_000_000.0
QUICK_DIR = Path(".harness/plans/quick-20270115-080000")
QUICK_REL = (QUICK_DIR / "plan.json").as_posix()


def triage(
    route: str, tier: str = "haiku", reason: str = "small", cost: float = 0.0
) -> PlannerTurn:
    body = json.dumps({"route": route, "tier": tier, "reason": reason})
    return PlannerTurn(text="ok\n\n```json\n" + body + "\n```", usage=Usage(cost_usd=cost))


def write_quick(root: Path):
    def step(_text: str) -> PlannerTurn:
        target = root / quick_plan_dir(NOW) / "plan.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(VALID_PLAN), encoding="utf-8")
        return PlannerTurn(text="quick plan written")

    return step


def write_draft(root: Path, cost: float = 0.0):
    def step(_text: str) -> PlannerTurn:
        (root / PROJECT_PLAN_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / PROJECT_PLAN_FILE).write_text("# P\n\nStatus: draft\n", encoding="utf-8")
        return PlannerTurn(text="draft written", usage=Usage(cost_usd=cost))

    return step


def write_draft_orders(root: Path, cost: float = 0.0):
    def step(_text: str) -> PlannerTurn:
        target = root / STAGE_PLAN_DIR / "plan.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(VALID_PLAN), encoding="utf-8")
        return PlannerTurn(text="stage 1 orders written", usage=Usage(cost_usd=cost))

    return step


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path)


@pytest.fixture
def make(repo):
    made: list[Office] = []

    def build(steps, triage_steps=(), **kwargs):
        """The Opus fake is returned; the triage fake is `office.triage_fake`."""
        session = FakePlannerSession(steps)
        triage_fake = FakePlannerSession(triage_steps)
        launches = Launches()
        office = Office(
            repo,
            fake=True,
            fake_delay=0,
            session_factory=lambda _r, _p: session,
            triage_session_factory=lambda _r, _p: triage_fake,
            launcher=launches,
            clock=lambda: NOW,
            **kwargs,
        )
        office.triage_fake = triage_fake  # type: ignore[attr-defined]
        made.append(office)
        return office, session, launches

    yield build
    for office in made:
        office.close()


def state_is(office: Office, state: str):
    return lambda: office.view().state == state


def types(repo: Path) -> list[str]:
    return [e.type for e in read_planner_events(repo)]


def test_auto_direct_hands_to_a_chat(repo, make):
    office, session, launches = make([], [triage("direct")])
    office.start("note this", route="auto")
    wait_for(lambda: office.view().handoff_chat)
    assert session.sent == []
    assert len(office.triage_fake.sent) == 1 and office.triage_fake.closed
    view = office.view()
    assert view.state == "idle"
    assert view.route == "direct"
    assert view.triage_reason == "small"
    assert view.plan_kind is None
    assert view.chats[0].tier == "haiku"
    assert view.chats[0].chat_id == view.handoff_chat
    wait_for(lambda: office.view().chats[0].state == "waiting")
    assert "- note this" in (repo / "notes.md").read_text()
    events = read_planner_events(repo)
    kinds = [e.type for e in events]
    assert kinds.index("planner_started") < kinds.index("planner_triage")
    assert kinds.index("planner_triage") < kinds.index("planner_done")
    started = events[kinds.index("planner_started")].payload
    assert started["route"] == "auto" and started["auto"] is False
    assert events[kinds.index("planner_triage")].payload == {
        "route": "direct",
        "tier": "haiku",
        "reason": "small",
    }
    done = events[kinds.index("planner_done")].payload
    assert done["plan_path"] is None and done["route"] == "direct"
    assert launches.made == []
    log_view = office_view_from_log(repo)
    assert log_view.route == "direct"
    assert log_view.triage_reason == "small"


def test_direct_handoff_refused(repo, make):
    office, _s, _l = make([], [triage("direct")])
    office.chats.start("haiku", "x")
    wait_for(lambda: office.view().chats[0].state == "waiting")
    office.start("note this", route="auto")
    wait_for(state_is(office, "failed"))
    reason = "could not hand to haiku: a haiku chat is already open"
    assert office.view().error == reason
    last = read_planner_events(repo)[-1]
    assert last.type == "planner_failed" and last.payload["reason"] == reason


def test_quick_route_to_run(repo, make):
    office, session, launches = make([write_quick(repo), PlannerTurn(text="starting")])
    office.start("add a flag", route="quick")
    wait_for(state_is(office, "ready"))
    assert session.sent[0].startswith("Use the plan-stage skill in quick mode")
    assert str(repo.resolve() / QUICK_DIR) in session.sent[0]
    view = office.view()
    assert view.plan_kind == "quick" and view.route == "quick"
    assert view.quick_plan == QUICK_REL
    ready = [e for e in read_planner_events(repo) if e.type == "plan_ready"]
    assert ready[-1].payload["path"] == QUICK_REL
    office.implement()
    wait_for(state_is(office, "supervising"))
    assert launches.made[0].argv[4] == QUICK_REL
    done = [e for e in read_planner_events(repo) if e.type == "planner_done"][-1]
    assert done.payload["route"] == "quick"
    assert not (repo / PROJECT_PLAN_FILE).exists()
    assert office_view_from_log(repo).quick_plan == QUICK_REL


def test_quick_implement_before_plan_loads(repo, make):
    office, _s, _l = make([PlannerTurn(text="thinking"), write_quick(repo)])
    office.start("add a flag", route="quick")
    wait_for(state_is(office, "asking"))
    with pytest.raises(OfficeConflict, match="the quick plan is not written yet"):
        office.implement()
    office.message("write it")
    wait_for(state_is(office, "ready"))


def test_auto_to_quick(repo, make):
    office, session, _l = make([write_quick(repo)], [triage("quick", "sonnet", "one stage")])
    office.start("add a flag", route="auto")
    wait_for(state_is(office, "ready"))
    assert "planner_triage" in types(repo)
    phases = [e.payload["phase"] for e in read_planner_events(repo) if e.type == "planner_message"]
    assert phases == ["triage", "quick"]
    assert office.view().triage_reason == "one stage"
    assert "quick mode" in session.sent[0]


def test_auto_to_project(repo, make):
    office, session, _l = make(
        [write_draft(repo), write_draft_orders(repo)], [triage("project", "sonnet", "big")]
    )
    office.start("build an app", route="auto")
    wait_for(state_is(office, "ready"))
    view = office.view()
    assert view.route == "project" and view.plan_kind == "project"
    assert "plan-project" in session.sent[0]
    assert "plan_ready" in types(repo)


def test_planner_cost_includes_the_triage_turn(repo, make):
    office, _s, _l = make(
        [write_draft(repo, 0.25), write_draft_orders(repo, 0.25)],
        [triage("project", "sonnet", "big", 0.03)],
    )
    office.start("build an app", route="auto")
    wait_for(state_is(office, "ready"))
    assert office.view().cost_usd == pytest.approx(0.28)
    events = read_planner_events(repo)
    replies = [e.payload for e in events if e.type == "planner_reply"]
    assert [r["cost_usd"] for r in replies] == pytest.approx([0.03, 0.28, 0.28])
    phases = [e.payload["phase"] for e in events if e.type == "planner_message"]
    assert phases == ["triage", "project", "project"]
    assert office_view_from_log(repo).cost_usd == pytest.approx(0.28)


def test_from_draft_forces_project_route(repo, make):
    office, _s, _l = make([PlannerTurn(text="q?")])
    office.start("first")
    wait_for(state_is(office, "asking"))
    office.close()
    wait_for(state_is(office, "interrupted"))
    (repo / PROJECT_PLAN_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / PROJECT_PLAN_FILE).write_text("# P\n\nStatus: draft\n", encoding="utf-8")
    office2, _s2, _l2 = make([PlannerTurn(text="q?")])
    office2.start(from_draft=True, route="quick")
    wait_for(state_is(office2, "asking"))
    started = [e for e in read_planner_events(repo) if e.type == "planner_started"][-1]
    assert started.payload["route"] == "project"


def test_old_log_has_no_route(repo):
    from harness.planner_log import PlannerLog

    log = PlannerLog(repo, "old-session")
    log.emit(
        "planner_started",
        {"session_id": "old-session", "source": "office", "pid": 1, "stage": 1},
    )
    view = office_view_from_log(repo)
    assert view.route is None and view.plan_kind is None and view.quick_plan is None
