"""Agent roles and the Researcher (plans/researcher/PLAN.md): the router picks a role as
well as a tier, a researcher can run on any tier (Opus as a direct chat), a role change
hands the thread to a new agent, and the files a turn produced reach the page."""

import json

import pytest
from git_helpers import init_repo
from test_router_effort import Env, Rec, chat_view, wait_for

from harness.models import Usage
from harness.planner import ORCHESTRATOR_PROMPT, PlannerTurn, Routing, parse_routing
from harness.roles import ROLES, role_or_default, role_title
from harness.ui.chat import (
    ChatConflict,
    ChatManager,
    SdkChatSession,
    artifact_kind,
    chat_tier_ok,
    collect_artifacts,
    read_chat_events,
)
from harness.ui.demo import DemoChatSession
from harness.ui.markdown import parse_markdown
from harness.ui.orchestrator import routing_line


def route(to, role=None, effort="medium", plan="quick", reason="because"):
    data = {"to": to, "plan": plan, "effort": effort, "reason": reason}
    if role is not None:
        data["role"] = role
    return PlannerTurn(text=f"```json\n{json.dumps(data)}\n```", usage=Usage(cost_usd=0.01))


# -- roles and routing -----------------------------------------------------------------


def test_roles_have_titles_and_figures():
    assert [ROLES[r].title for r in ROLES] == [
        "Orchestrator",
        "Planner",
        "Builder",
        "Reviewer",
        "Researcher",
    ]
    assert role_title("researcher") == "Researcher"
    assert role_title("nonsense") == "Builder"


@pytest.mark.parametrize(
    ("given", "want"),
    [("researcher", "researcher"), (" Researcher ", "researcher"), ("builder", "builder"),
     (None, "builder"), ("planner", "builder"), (3, "builder")],
)  # fmt: skip
def test_routing_role_falls_back_to_builder(given, want):
    data = {"to": "sonnet"}
    if given is not None:
        data["role"] = given
    routing = parse_routing(f"```json\n{json.dumps(data)}\n```")
    assert routing is not None and routing.role == want
    assert role_or_default(given) == want


def test_prompt_asks_for_the_role():
    prompt = ORCHESTRATOR_PROMPT.format(
        repo="/r", holder="sonnet", effort="low", role="researcher", tail="(none)", text="x"
    )
    assert "Holder's role: researcher." in prompt
    assert '"role": "builder" | "researcher"' in prompt
    assert Routing(to="opus").role == "builder"


def test_tiers_per_role():
    assert chat_tier_ok("opus", "researcher")
    assert not chat_tier_ok("opus", "builder")
    assert chat_tier_ok("haiku", "builder") and chat_tier_ok("haiku", "researcher")


def test_routing_lines_name_the_researcher():
    assert routing_line("start", "sonnet", "quick", "eval", "medium", None, "researcher") == (
        "Sent to Researcher (sonnet): eval"
    )
    assert routing_line("handoff", "haiku", "quick", "now code", "low", "medium", "builder") == (
        "Handed to Builder (haiku, low effort): now code"
    )
    assert routing_line("start", "haiku", "quick", "x") == "Sent to haiku: x"


# -- the chat manager ------------------------------------------------------------------


class RoleRec(Rec):
    role = None


def manager(repo):
    sessions: list[RoleRec] = []

    def factory(r, tier, progress):
        s = RoleRec(len(sessions) + 1)
        sessions.append(s)
        return s

    return ChatManager(repo, session_factory=factory), sessions


def test_researcher_chat_on_opus_logs_and_shows_its_role(tmp_path):
    init_repo(tmp_path)
    mgr, sessions = manager(tmp_path)
    try:
        cid = mgr.start("opus", "run the eval", role="researcher")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert sessions[0].role == "researcher"
        started = next(e for e in read_chat_events(tmp_path) if e.type == "chat_started")
        assert started.payload["role"] == "researcher"
        view = chat_view(mgr, cid)
        assert view.role == "researcher" and view.tier == "opus"
        with pytest.raises(ChatConflict):
            mgr.start("opus", "build it")  # builders on opus go to the planning desk
    finally:
        mgr.close()


def test_old_logs_without_role_load_as_builder(tmp_path):
    init_repo(tmp_path)
    mgr, _ = manager(tmp_path)
    try:
        cid = mgr.start("haiku", "hi")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        assert chat_view(mgr, cid).role == "builder"
    finally:
        mgr.close()


def test_researcher_session_gets_the_researcher_prompt(tmp_path):
    s = SdkChatSession(tmp_path, "sonnet")
    assert "Researcher" not in s.options().system_prompt["append"]
    s.role = "researcher"
    append = s.options().system_prompt["append"]
    assert "REPORT.md" in append and "without being asked" in append


# -- artifacts -------------------------------------------------------------------------


def test_artifact_kinds():
    assert artifact_kind("reports/x/figures/a.png") == "image"
    assert artifact_kind("reports/x/REPORT.md") == "report"
    assert artifact_kind("docs/notes.md") is None
    assert artifact_kind("reports/x/data/a.csv") == "data"
    assert artifact_kind("src/a.py") is None


def test_collect_artifacts_orders_and_finds_named_reports(tmp_path):
    (tmp_path / "reports" / "old").mkdir(parents=True)
    (tmp_path / "reports" / "old" / "REPORT.md").write_text("# r")
    paths = ["src/a.py", "reports/x/figures/b.png", "reports/x/REPORT.md", "reports/x/d.csv"]
    got = collect_artifacts(tmp_path, paths, "See reports/old/REPORT.md and reports/nope.md.")
    assert got == [
        {"path": "reports/old/REPORT.md", "kind": "report"},
        {"path": "reports/x/REPORT.md", "kind": "report"},
        {"path": "reports/x/figures/b.png", "kind": "image"},
        {"path": "reports/x/d.csv", "kind": "data"},
    ]


def test_demo_researcher_reply_carries_its_artifacts(tmp_path):
    init_repo(tmp_path)

    def factory(r, tier, progress):
        return DemoChatSession(r, tier, progress=progress, delay_s=0)

    mgr = ChatManager(tmp_path, session_factory=factory)
    try:
        cid = mgr.start("sonnet", "eval the tiers", role="researcher")
        wait_for(lambda: chat_view(mgr, cid).state == "waiting")
        reply = chat_view(mgr, cid).transcript[-1]
        kinds = {a["path"]: a["kind"] for a in reply.artifacts}
        assert kinds == {
            "reports/demo-eval/REPORT.md": "report",
            "reports/demo-eval/figures/pass-rate.svg": "image",
            "reports/demo-eval/data/pass-rate.csv": "data",
        }
    finally:
        mgr.close()


def test_builder_edits_produce_no_artifacts(tmp_path):
    assert collect_artifacts(tmp_path, ["src/a.py", "notes.md"], "done") == []


# -- markdown images -------------------------------------------------------------------


def test_markdown_image_block():
    blocks = parse_markdown("Intro\n![Pass rate](figures/a.png)\nAfter")
    assert blocks[1] == {"type": "image", "alt": "Pass rate", "src": "figures/a.png"}
    assert blocks[0]["type"] == blocks[2]["type"] == "paragraph"
    inline = parse_markdown("see ![x](a.png) inline")
    assert [b["type"] for b in inline] == ["paragraph"]


# -- the Orchestrator ------------------------------------------------------------------


class RoleEnv(Env):
    def __init__(self, repo):
        super().__init__(repo)
        self.chats.close()
        self.chats, self.chat_sessions = manager(repo)
        self.orch.chats = self.chats


@pytest.fixture
def renv(tmp_path):
    init_repo(tmp_path)
    e = RoleEnv(tmp_path)
    yield e
    e.orch.close()
    e.chats.close()


def test_research_request_starts_a_researcher(renv):
    renv.say("eval the router", route("sonnet", "researcher"))
    (turn,) = renv.turns()
    assert turn["role"] == "researcher" and turn["target_kind"] == "chat"
    assert renv.chat_sessions[0].role == "researcher"
    assert "Sent to Researcher (sonnet): because" in renv.lines()
    view = renv.orch.view()
    assert view.holder_role == "researcher"
    reply = next(e for e in view.transcript if e.role == "agent")
    assert reply.agent_role == "researcher"


def test_opus_research_is_a_chat_not_the_planning_desk(renv):
    renv.say("design an experiment", route("opus", "researcher"))
    assert renv.opus.started == []
    assert renv.turns()[0]["to"] == "opus" and renv.turns()[0]["target_kind"] == "chat"


def test_role_change_hands_off_to_a_new_agent(renv):
    renv.say("eval the router", route("sonnet", "researcher"))
    renv.say("now fix the bug you found", route("sonnet", "builder"))
    turns = renv.turns()
    assert turns[-1]["action"] == "handoff" and turns[-1]["role"] == "builder"
    assert turns[-1]["target_id"] != turns[0]["target_id"]
    assert renv.chat_sessions[1].role == "builder"
    assert "Holder's role: researcher." in renv.router_prompts[-1]


def test_same_role_follow_up_forwards(renv):
    renv.say("eval the router", route("sonnet", "researcher"))
    renv.say("add a chart", route("sonnet", "researcher"))
    assert renv.turns()[-1]["action"] == "forward"
    assert len(renv.chat_sessions) == 1


def test_builder_to_opus_still_goes_to_the_planning_desk(renv):
    renv.say("plan it", route("opus", plan="project"))
    assert renv.opus.started and renv.turns()[0]["role"] == "planner"
