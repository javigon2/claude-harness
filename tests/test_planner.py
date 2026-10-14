import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from harness.config import MODEL_IDS
from harness.models import Usage
from harness.planner import (
    FIX_PROMPT,
    HARNESS_ROOT,
    PLANNER_APPEND,
    PLANNER_INIT_FILE,
    PLANNER_MAX_TURNS,
    PLANNER_SKILLS,
    PLANNER_TOOLS,
    PROJECT_PROMPT,
    STAGE_PLAN_DIR,
    STAGE_PROMPT,
    FakePlannerSession,
    PlannerError,
    PlannerSession,
    PlannerTurn,
    SdkPlannerSession,
    check_skills,
    format_session_init,
    is_approved,
    run_planner,
)
from harness.worker import WORKER_ENV

# ---------------------------------------------------------------- helpers


def result_msg(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 3,
        "session_id": "s1",
        "total_cost_usd": 0.25,
        "usage": {
            "input_tokens": 5,
            "output_tokens": 7,
            "cache_read_input_tokens": 11,
            "cache_creation_input_tokens": 13,
        },
        "result": "the reply",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def text_msg(text: str, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(
        self,
        options: Any,
        scripts: list[list[Any]] | None = None,
        connect_error: BaseException | None = None,
        raise_after: BaseException | None = None,
    ) -> None:
        self.options = options
        self.scripts = list(scripts or [])
        self.connect_error = connect_error
        self.raise_after = raise_after
        self.connects = 0
        self.queries: list[str] = []
        self.disconnects = 0
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive_response(self):
        messages = self.scripts.pop(0) if self.scripts else []
        for m in messages:
            yield m
        if self.raise_after is not None:
            raise self.raise_after

    async def disconnect(self) -> None:
        self.disconnects += 1


def factory(**kwargs: Any):
    made: list[FakeClient] = []

    def make(options: Any) -> FakeClient:
        c = FakeClient(options, **kwargs)
        made.append(c)
        return c

    return make, made


VALID_PLAN = {
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

INVALID_PLAN = {"stage": "s", "orders": [{**VALID_PLAN["orders"][0], "depends_on": ["zz"]}]}


def write_project(repo: Path, status: str, body: str = "plan body") -> None:
    (repo / "PROJECT_PLAN.md").write_text(f"# Plan\n\nStatus: {status}\n\n{body}\n")


def write_stage(repo: Path, data: Any) -> None:
    d = repo / STAGE_PLAN_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(data) if not isinstance(data, str) else data)


def step(text: str, cost: float, action=None):
    def run(_msg: str) -> PlannerTurn:
        if action is not None:
            action()
        return PlannerTurn(text=text, usage=Usage(cost_usd=cost))

    return run


def asker(answers: list[str | None]):
    asked: list[str] = []

    def ask(prompt: str) -> str | None:
        asked.append(prompt)
        return answers.pop(0) if answers else None

    return ask, asked


# ---------------------------------------------------------------- small helpers


def test_check_skills_real_root() -> None:
    check_skills(HARNESS_ROOT)
    check_skills()


def test_check_skills_missing(tmp_path: Path) -> None:
    with pytest.raises(PlannerError) as exc:
        check_skills(tmp_path)
    assert str(exc.value) == f"planner skills not found under {tmp_path / '.claude' / 'skills'}"


def test_check_skills_partial(tmp_path: Path) -> None:
    d = tmp_path / ".claude" / "skills" / "plan-project"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("x")
    with pytest.raises(PlannerError):
        check_skills(tmp_path)


def test_is_approved() -> None:
    assert is_approved("Status: approved 2026-10-14")
    assert is_approved("# Title\n\nStatus: approved 2026-10-14\n\nmore text")
    assert not is_approved("Status: approved soon")
    assert not is_approved("Status: draft")
    assert not is_approved("  Status: approved 2026-10-14 extra")
    assert not is_approved("")


# ---------------------------------------------------------------- SdkPlannerSession


def test_options(tmp_path: Path) -> None:
    s = SdkPlannerSession(tmp_path, harness_root=HARNESS_ROOT)
    o = s.options()
    assert o.model == MODEL_IDS["opus"]
    assert o.system_prompt == {"type": "preset", "preset": "claude_code", "append": PLANNER_APPEND}
    assert o.tools == [*PLANNER_TOOLS, "Skill"]
    assert o.allowed_tools == list(PLANNER_TOOLS)
    assert o.skills == list(PLANNER_SKILLS)
    assert o.setting_sources == ["project"]
    assert o.add_dirs == [str(HARNESS_ROOT.resolve())]
    assert o.permission_mode == "acceptEdits"
    assert o.cwd == str(tmp_path.resolve())
    assert o.max_turns == PLANNER_MAX_TURNS
    assert o.strict_mcp_config is True
    assert o.env == dict(WORKER_ENV)


def test_is_planner_session(tmp_path: Path) -> None:
    assert isinstance(SdkPlannerSession(tmp_path), PlannerSession)
    assert isinstance(FakePlannerSession([]), PlannerSession)


async def test_send_connects_once_and_returns_result(tmp_path: Path) -> None:
    make, made = factory(
        scripts=[
            [text_msg("thinking"), result_msg(result="first", total_cost_usd=0.1)],
            [text_msg("again"), result_msg(result="second", total_cost_usd=0.3)],
        ]
    )
    s = SdkPlannerSession(tmp_path, client_factory=make)
    t1 = await s.send("hello")
    t2 = await s.send("next")
    assert len(made) == 1
    c = made[0]
    assert c.connects == 1
    assert c.queries == ["hello", "next"]
    assert c.options.model == MODEL_IDS["opus"]
    assert t1.text == "first" and t1.error is None
    assert t1.usage.cost_usd == pytest.approx(0.1)
    assert t2.text == "second" and t2.error is None
    assert t2.usage.cost_usd == pytest.approx(0.3)
    assert t2.usage.input_tokens == 5
    assert t2.usage.output_tokens == 7
    assert t2.usage.cache_read_tokens == 11
    assert t2.usage.cache_creation_tokens == 13


async def test_send_falls_back_to_last_top_level_text(tmp_path: Path) -> None:
    make, _ = factory(
        scripts=[
            [
                text_msg("top one"),
                text_msg("top two"),
                text_msg("   "),
                text_msg("sub agent", parent="tool-1"),
                result_msg(result="  "),
            ]
        ]
    )
    s = SdkPlannerSession(tmp_path, client_factory=make)
    t = await s.send("x")
    assert t.text == "top two"
    assert t.error is None


async def test_send_result_none_falls_back(tmp_path: Path) -> None:
    make, _ = factory(scripts=[[text_msg("only text"), result_msg(result=None)]])
    t = await SdkPlannerSession(tmp_path, client_factory=make).send("x")
    assert t.text == "only text"


async def test_send_error_result(tmp_path: Path) -> None:
    make, _ = factory(
        scripts=[[result_msg(is_error=True, subtype="error_max_turns", errors=["boom"])]]
    )
    t = await SdkPlannerSession(tmp_path, client_factory=make).send("x")
    assert t.error == "result error_max_turns: boom"
    assert t.usage.cost_usd == pytest.approx(0.25)


async def test_send_error_result_without_errors(tmp_path: Path) -> None:
    make, _ = factory(scripts=[[result_msg(is_error=True, subtype="error_during_execution")]])
    t = await SdkPlannerSession(tmp_path, client_factory=make).send("x")
    assert t.error == "result error_during_execution"


async def test_send_exception_from_receive(tmp_path: Path) -> None:
    make, _ = factory(scripts=[[text_msg("partial")]], raise_after=RuntimeError("kaput"))
    t = await SdkPlannerSession(tmp_path, client_factory=make).send("x")
    assert t.error == "RuntimeError: kaput"
    assert t.text == "partial"


async def test_send_connect_failure(tmp_path: Path) -> None:
    make, made = factory(connect_error=OSError("no cli"))
    s = SdkPlannerSession(tmp_path, client_factory=make)
    t = await s.send("x")
    assert t.error == "connect failed: OSError: no cli"
    assert t.text == ""
    assert made[0].queries == []
    await s.close()
    assert made[0].disconnects == 0


async def test_send_no_result_message(tmp_path: Path) -> None:
    make, _ = factory(scripts=[[text_msg("hi")]])
    t = await SdkPlannerSession(tmp_path, client_factory=make).send("x")
    assert t.error == "turn ended without a result message"
    assert t.text == "hi"
    assert t.usage == Usage()


async def test_close(tmp_path: Path) -> None:
    make, made = factory(scripts=[[result_msg()]])
    s = SdkPlannerSession(tmp_path, client_factory=make)
    await s.close()
    assert made == []
    await s.send("x")
    await s.close()
    await s.close()
    assert made[0].disconnects == 1


async def test_close_ignores_disconnect_errors(tmp_path: Path) -> None:
    class Bad(FakeClient):
        async def disconnect(self) -> None:
            raise RuntimeError("nope")

    s = SdkPlannerSession(tmp_path, client_factory=lambda options: Bad(options, [[result_msg()]]))
    await s.send("x")
    await s.close()
    await s.close()


# ---------------------------------------------------------------- FakePlannerSession


async def test_fake_session_runs_out() -> None:
    s = FakePlannerSession([PlannerTurn(text="a")])
    assert (await s.send("1")).text == "a"
    with pytest.raises(RuntimeError, match="no scripted planner turn left"):
        await s.send("2")
    assert s.sent == ["1", "2"]


# ---------------------------------------------------------------- run_planner


def happy_steps(repo: Path) -> list:
    return [
        step("Some questions?", 0.1),
        step("Draft plan written. Approve?", 0.2, lambda: write_project(repo, "draft")),
        step("Approved.", 0.3, lambda: write_project(repo, "approved 2026-10-14")),
        step("Stage plan written.", 0.4, lambda: write_stage(repo, VALID_PLAN)),
    ]


async def test_run_planner_happy_path(tmp_path: Path) -> None:
    session = FakePlannerSession(happy_steps(tmp_path))
    ask, asked = asker(["defaults", "approve"])
    said: list[str] = []
    result = await run_planner("  build a thing  ", tmp_path, session, ask, said.append)
    repo = tmp_path.resolve()
    assert result.plan_path == repo / STAGE_PLAN_DIR / "plan.json"
    assert result.project_plan == repo / "PROJECT_PLAN.md"
    assert result.rounds == 4
    assert result.usage.cost_usd == pytest.approx(0.4)
    assert session.sent[0] == PROJECT_PROMPT.format(repo=repo, description="build a thing")
    assert session.sent[1:3] == ["defaults", "approve"]
    assert session.sent[3] == STAGE_PROMPT.format(
        project_plan=repo / "PROJECT_PLAN.md", stage_dir=repo / STAGE_PLAN_DIR
    )
    assert said == [
        "Some questions?",
        "Draft plan written. Approve?",
        "Approved.",
        "Stage plan written.",
    ]
    assert asked == ["> ", "> "]
    assert session.closed


async def test_run_planner_fixes_invalid_plan(tmp_path: Path) -> None:
    steps = happy_steps(tmp_path)[:3] + [
        step("bad", 0.4, lambda: write_stage(tmp_path, INVALID_PLAN)),
        step("fixed", 0.5, lambda: write_stage(tmp_path, VALID_PLAN)),
    ]
    session = FakePlannerSession(steps)
    ask, _ = asker(["defaults", "approve"])
    result = await run_planner("d", tmp_path, session, ask, lambda _t: None)
    plan_path = tmp_path.resolve() / STAGE_PLAN_DIR / "plan.json"
    fix = session.sent[4]
    assert fix.startswith(FIX_PROMPT.split("{plan_path}")[0])
    assert "unknown dependency: a depends on zz" in fix
    assert fix == FIX_PROMPT.format(
        plan_path=plan_path, error="unknown dependency: a depends on zz"
    )
    assert result.rounds == 5
    assert result.usage.cost_usd == pytest.approx(0.5)
    assert session.closed


async def test_run_planner_still_invalid(tmp_path: Path) -> None:
    def bad(n: int):
        return step(f"bad {n}", 0.1, lambda: write_stage(tmp_path, "{not json" + "x" * n))

    steps = happy_steps(tmp_path)[:3] + [bad(1), bad(2), bad(3)]
    session = FakePlannerSession(steps)
    ask, _ = asker(["defaults", "approve"])
    with pytest.raises(PlannerError, match="still invalid after 2 fixes"):
        await run_planner("d", tmp_path, session, ask, lambda _t: None)
    assert session.closed


async def test_run_planner_eof(tmp_path: Path) -> None:
    session = FakePlannerSession([step("question?", 0.1)])
    ask, _ = asker([])
    with pytest.raises(PlannerError) as exc:
        await run_planner("d", tmp_path, session, ask, lambda _t: None)
    assert str(exc.value) == "aborted: no answer (end of input)"
    assert session.closed


@pytest.mark.parametrize("word", ["quit", "  EXIT "])
async def test_run_planner_quit(tmp_path: Path, word: str) -> None:
    session = FakePlannerSession([step("question?", 0.1)])
    ask, _ = asker([word])
    with pytest.raises(PlannerError) as exc:
        await run_planner("d", tmp_path, session, ask, lambda _t: None)
    assert str(exc.value) == "aborted by the user"
    assert session.closed


async def test_run_planner_error_turn(tmp_path: Path) -> None:
    session = FakePlannerSession([PlannerTurn(text="partial", error="result error_max_turns")])
    said: list[str] = []
    ask, _ = asker([])
    with pytest.raises(PlannerError) as exc:
        await run_planner("d", tmp_path, session, ask, said.append)
    assert str(exc.value) == "planner session failed: result error_max_turns"
    assert said == ["partial"]
    assert session.closed


async def test_run_planner_max_rounds(tmp_path: Path) -> None:
    session = FakePlannerSession([step("q", 0.1) for _ in range(5)])
    ask, _ = asker(["a"] * 10)
    with pytest.raises(PlannerError) as exc:
        await run_planner("d", tmp_path, session, ask, lambda _t: None, max_rounds=3)
    assert str(exc.value) == "no valid plan after 3 rounds"
    assert len(session.sent) == 3
    assert session.closed


async def test_run_planner_preexisting_approved_plan_needs_rewrite(tmp_path: Path) -> None:
    write_project(tmp_path, "approved 2026-01-01", "old")
    steps = [
        step("Revise or start over?", 0.1),
        step("Revised.", 0.2, lambda: write_project(tmp_path, "approved 2026-10-14", "new")),
        step("Stage written.", 0.3, lambda: write_stage(tmp_path, VALID_PLAN)),
    ]
    session = FakePlannerSession(steps)
    ask, _ = asker(["revise"])
    result = await run_planner("d", tmp_path, session, ask, lambda _t: None)
    assert result.rounds == 3
    assert session.sent[1] == "revise"
    assert session.sent[2].startswith(str(tmp_path.resolve() / "PROJECT_PLAN.md"))


async def test_run_planner_ignores_stale_plan_json(tmp_path: Path) -> None:
    write_stage(tmp_path, VALID_PLAN)
    steps = happy_steps(tmp_path)[:3] + [
        step("Which tiers?", 0.4),
        step(
            "Written.",
            0.5,
            lambda: write_stage(tmp_path, {**VALID_PLAN, "stage": "new"}),
        ),
    ]
    session = FakePlannerSession(steps)
    ask, _ = asker(["defaults", "approve", "sonnet"])
    result = await run_planner("d", tmp_path, session, ask, lambda _t: None)
    assert result.rounds == 5
    assert session.sent[4] == "sonnet"
    assert result.usage.cost_usd == pytest.approx(0.5)


async def test_run_planner_closes_on_session_exception(tmp_path: Path) -> None:
    session = FakePlannerSession([])
    ask, _ = asker([])
    with pytest.raises(RuntimeError, match="no scripted planner turn left"):
        await run_planner("d", tmp_path, session, ask, lambda _t: None)
    assert session.closed


# ---------------------------------------------------------------- session init facts

FACTS: dict[str, Any] = {
    "model": MODEL_IDS["opus"],
    "tools": ["Read", "Write"],
    "skills": ["plan-project", "plan-stage"],
    "mcp_servers": [],
    "permissionMode": "acceptEdits",
    "apiKeySource": "none",
}


def test_format_session_init_lines() -> None:
    assert format_session_init(FACTS).split("\n") == [
        f"Planner session: model={MODEL_IDS['opus']} apiKeySource=none permissionMode=acceptEdits",
        "  skills: plan-project, plan-stage",
        "  tools: Read, Write",
        "  mcp_servers: none",
    ]


def test_format_session_init_mcp_names() -> None:
    facts = {**FACTS, "mcp_servers": [{"name": "a", "status": "connected"}]}
    assert format_session_init(facts).split("\n")[3] == "  mcp_servers: a"


def test_format_session_init_empty() -> None:
    assert format_session_init({}).split("\n") == [
        "Planner session: model=None apiKeySource=None permissionMode=None",
        "  skills: none",
        "  tools: none",
        "  mcp_servers: none",
    ]


async def test_send_returns_session_init(tmp_path: Path) -> None:
    data = {**FACTS, "cwd": "/x", "agents": []}
    make, _ = factory(
        scripts=[
            [SystemMessage(subtype="init", data=data), text_msg("hi"), result_msg()],
            [text_msg("hi"), result_msg()],
        ]
    )
    s = SdkPlannerSession(tmp_path, client_factory=make)
    t1 = await s.send("one")
    t2 = await s.send("two")
    assert t1.error is None and t1.session_init == FACTS
    assert t2.error is None and t2.session_init is None


async def test_run_planner_reports_session_init_once(tmp_path: Path) -> None:
    steps = happy_steps(tmp_path)

    def first(_msg: str) -> PlannerTurn:
        return PlannerTurn(text="Some questions?", usage=Usage(cost_usd=0.1), session_init=FACTS)

    def third(_msg: str) -> PlannerTurn:
        write_project(tmp_path, "approved 2026-10-14")
        return PlannerTurn(
            text="Approved.", usage=Usage(cost_usd=0.3), session_init={**FACTS, "model": "x"}
        )

    steps[0] = first
    steps[2] = third
    session = FakePlannerSession(steps)
    ask, _ = asker(["defaults", "approve"])
    said: list[str] = []
    result = await run_planner("build", tmp_path, session, ask, said.append)
    assert said[0] == format_session_init(FACTS)
    assert said[1:] == [
        "Some questions?",
        "Draft plan written. Approve?",
        "Approved.",
        "Stage plan written.",
    ]
    saved = tmp_path.resolve() / PLANNER_INIT_FILE
    assert json.loads(saved.read_text(encoding="utf-8")) == FACTS
    assert result.session_init == FACTS
