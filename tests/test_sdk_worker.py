import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from harness.config import MODEL_IDS, Tier
from harness.models import Budget, ContextItem, Usage, WorkOrder
from harness.worker import (
    PROGRESS_DETAIL_MAX,
    SESSION_INIT_KEYS,
    WORKER_ENV,
    WORKER_TOOLS,
    SdkWorker,
    Worker,
    build_prompt,
    describe_tool,
    load_system_prompt,
    session_init_facts,
)

DONE_REPORT = {
    "status": "done",
    "summary": "did it",
    "files_changed": ["src/x.py"],
    "acceptance": [{"cmd": "uv run pytest -q", "exit_code": 0}],
    "notes": "n",
}


def block(report: dict[str, Any]) -> str:
    return "All good.\n\n```json\n" + json.dumps(report) + "\n```"


def make_order(max_turns: int = 7) -> WorkOrder:
    return WorkOrder(
        id="a",
        title="T",
        goal="G",
        tier="haiku",
        depends_on=[],
        files_owned=["src/x.py"],
        files_readonly=[],
        context=[ContextItem(label="l", content="c")],
        steps=["s"],
        constraints=[],
        acceptance=["uv run pytest -q"],
        budget=Budget(max_turns=max_turns, max_attempts=2),
        out_of_scope=[],
    )


def scripted(*messages: Any, raise_after: BaseException | None = None) -> tuple[Any, dict]:
    seen: dict[str, Any] = {}

    async def fake(*, prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        for m in messages:
            yield m
        if raise_after is not None:
            raise raise_after

    return fake, seen


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
        "result": block(DONE_REPORT),
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def text_msg(text: str, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


MAPPED_USAGE = Usage(
    input_tokens=5,
    output_tokens=7,
    cache_read_tokens=11,
    cache_creation_tokens=13,
    cost_usd=0.25,
)


@pytest.mark.parametrize("tier", ["opus", "haiku"])
def test_options_fields(tmp_path: Path, tier: Tier) -> None:
    fake, _ = scripted()
    worker = SdkWorker(query_fn=fake)
    order = make_order(max_turns=9)
    opts = worker.options(order, tier, tmp_path)
    assert opts.model == MODEL_IDS[tier]
    assert opts.system_prompt == worker.system_prompt
    assert opts.tools == list(WORKER_TOOLS)
    assert opts.allowed_tools == list(WORKER_TOOLS)
    assert opts.permission_mode == "acceptEdits"
    assert opts.cwd == str(tmp_path.resolve())
    assert opts.max_turns == order.budget.max_turns
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.verbatim_prompts is True
    assert opts.env == WORKER_ENV
    assert opts.env is not WORKER_ENV
    assert opts.skills == []
    assert WORKER_ENV == {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    }


async def test_query_receives_prompt_and_options(tmp_path: Path) -> None:
    fake, seen = scripted(result_msg())
    worker = SdkWorker(query_fn=fake)
    order = make_order()
    await worker.run(order, "sonnet", tmp_path, "fb")
    assert seen["prompt"] == build_prompt(order, tmp_path, "fb")
    assert seen["options"] == worker.options(order, "sonnet", tmp_path)


async def test_happy_path(tmp_path: Path) -> None:
    fake, _ = scripted(text_msg("working"), result_msg())
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "done"
    assert result.summary == "did it"
    assert result.files_changed == ["src/x.py"]
    assert result.usage == MAPPED_USAGE
    assert result.cost_usd == 0.25
    assert result.num_turns == 3
    assert result.duration_ms == 10
    assert result.session_id == "s1"
    assert result.error is None


async def test_model_usage_summed_and_preferred(tmp_path: Path) -> None:
    model_usage = {
        "m1": {
            "inputTokens": 1,
            "outputTokens": 2,
            "cacheReadInputTokens": 3,
            "cacheCreationInputTokens": 4,
            "costUSD": 0.5,
        },
        "m2": {
            "inputTokens": 10,
            "outputTokens": 20,
            "cacheReadInputTokens": 30,
            "cacheCreationInputTokens": 40,
            "costUSD": 0.25,
        },
    }
    fake, _ = scripted(result_msg(model_usage=model_usage, total_cost_usd=None))
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.usage == Usage(
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_creation_tokens=44,
        cost_usd=0.75,
    )


async def test_result_none_falls_back_to_last_top_level_text(tmp_path: Path) -> None:
    fake, _ = scripted(
        text_msg("first"),
        text_msg(block(DONE_REPORT)),
        text_msg("subagent chatter", parent="t1"),
        result_msg(result=None),
    )
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.final_text == block(DONE_REPORT)
    assert result.status == "done"
    assert result.summary == "did it"


async def test_no_block_is_unknown(tmp_path: Path) -> None:
    fake, _ = scripted(result_msg(result="no block here"))
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "unknown"
    assert result.usage == MAPPED_USAGE
    assert result.error is None


async def test_is_error_result(tmp_path: Path) -> None:
    fake, _ = scripted(
        result_msg(is_error=True, subtype="error_max_turns", errors=["out of turns"])
    )
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "error"
    assert result.error == "result error_max_turns: out of turns"
    assert result.usage == MAPPED_USAGE


async def test_raise_after_result_keeps_usage(tmp_path: Path) -> None:
    fake, _ = scripted(result_msg(), raise_after=RuntimeError("cli died"))
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "error"
    assert result.error == "RuntimeError: cli died"
    assert result.usage == MAPPED_USAGE


async def test_raise_without_messages(tmp_path: Path) -> None:
    fake, _ = scripted(raise_after=RuntimeError("boom"))
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "error"
    assert result.error == "RuntimeError: boom"
    assert result.usage == Usage()


async def test_no_messages(tmp_path: Path) -> None:
    fake, _ = scripted()
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "error"
    assert result.error == "session ended without a result message"


async def test_cancelled_error_propagates(tmp_path: Path) -> None:
    fake, _ = scripted(raise_after=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)


def test_is_worker() -> None:
    fake, _ = scripted()
    assert isinstance(SdkWorker(query_fn=fake), Worker)


def test_shared_context_system_prompt() -> None:
    fake, _ = scripted()
    assert SdkWorker(shared_context="ctx", query_fn=fake).system_prompt == load_system_prompt("ctx")


# session init facts

INIT_DATA: dict[str, Any] = {
    "model": "m-init",
    "tools": ["Read", "Bash"],
    "skills": [],
    "mcp_servers": [{"name": "x", "status": "connected"}],
    "permissionMode": "acceptEdits",
    "apiKeySource": "none",
    "cwd": "/somewhere",
}

INIT_FACTS: dict[str, Any] = {k: INIT_DATA[k] for k in SESSION_INIT_KEYS}


def init_msg(data: Any = None) -> SystemMessage:
    return SystemMessage(subtype="init", data=dict(INIT_DATA) if data is None else data)


def test_session_init_facts_picks_six_keys_in_order() -> None:
    facts = session_init_facts(init_msg())
    assert facts == INIT_FACTS
    assert list(facts) == list(SESSION_INIT_KEYS)
    assert "cwd" not in facts


def test_session_init_facts_missing_key_is_none() -> None:
    data = dict(INIT_DATA)
    del data["apiKeySource"]
    facts = session_init_facts(init_msg(data))
    assert facts is not None
    assert facts["apiKeySource"] is None
    assert list(facts) == list(SESSION_INIT_KEYS)


@pytest.mark.parametrize(
    "message",
    [
        SystemMessage(subtype="status", data=dict(INIT_DATA)),
        SystemMessage(subtype="init", data=["not", "a", "dict"]),  # type: ignore[arg-type]
        object(),
        None,
        text_msg("hi"),
    ],
)
def test_session_init_facts_none(message: Any) -> None:
    assert session_init_facts(message) is None


async def test_run_keeps_init_facts(tmp_path: Path) -> None:
    fake, _ = scripted(
        SystemMessage(subtype="status", data={"model": "other"}),
        init_msg(),
        text_msg("working"),
        result_msg(),
    )
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "done"
    assert result.session_init == INIT_FACTS


async def test_run_second_init_does_not_replace_first(tmp_path: Path) -> None:
    second = dict(INIT_DATA, model="m-second")
    fake, _ = scripted(init_msg(), init_msg(second), result_msg())
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.session_init == INIT_FACTS


async def test_run_without_init_is_none(tmp_path: Path) -> None:
    fake, _ = scripted(text_msg("working"), result_msg())
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.session_init is None


async def test_run_exception_after_init_keeps_facts(tmp_path: Path) -> None:
    fake, _ = scripted(init_msg(), raise_after=RuntimeError("boom"))
    result = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert result.status == "error"
    assert result.session_init == INIT_FACTS


# --- describe_tool ----------------------------------------------------------------------


def test_describe_tool_relative_path(tmp_path: Path) -> None:
    got = describe_tool("Edit", {"file_path": str(tmp_path.resolve() / "src" / "x.py")}, tmp_path)
    assert got == {"tool": "Edit", "detail": "src/x.py"}


def test_describe_tool_outside_path_kept(tmp_path: Path) -> None:
    got = describe_tool("Read", {"file_path": "/etc/hosts"}, tmp_path)
    assert got == {"tool": "Read", "detail": "/etc/hosts"}


def test_describe_tool_pattern(tmp_path: Path) -> None:
    assert describe_tool("Grep", {"pattern": "def f"}, tmp_path)["detail"] == "def f"
    assert describe_tool("Glob", {"pattern": "**/*.py"}, tmp_path)["detail"] == "**/*.py"


def test_describe_tool_pattern_with_path(tmp_path: Path) -> None:
    inside = str(tmp_path.resolve() / "src")
    cwd = tmp_path
    assert describe_tool("Grep", {"pattern": "logs", "path": "src/harness"}, cwd)["detail"] == (
        "logs in src/harness"
    )
    assert describe_tool("Glob", {"pattern": "*.py", "path": inside}, cwd)["detail"] == (
        "*.py in src"
    )
    assert describe_tool("Glob", {"pattern": "*.py", "path": "/etc"}, cwd)["detail"] == (
        "*.py in /etc"
    )
    assert describe_tool("Grep", {"pattern": "x", "path": ""}, cwd)["detail"] == "x"
    assert describe_tool("Grep", {"pattern": "x", "path": 3}, cwd)["detail"] == "x"
    assert describe_tool("Grep", {"path": "src"}, cwd)["detail"] == ""
    long = describe_tool("Grep", {"pattern": "p", "path": "d" * 200}, cwd)["detail"]
    assert len(long) == PROGRESS_DETAIL_MAX
    assert long.endswith("…")


def test_describe_tool_bash_strips_cd(tmp_path: Path) -> None:
    got = describe_tool("Bash", {"command": "cd /a/b && uv run pytest -q\necho hi"}, tmp_path)
    assert got == {"tool": "Bash", "detail": "uv run pytest -q"}


def test_describe_tool_truncates(tmp_path: Path) -> None:
    got = describe_tool("Grep", {"pattern": "x" * 200}, tmp_path)["detail"]
    assert len(got) == PROGRESS_DETAIL_MAX
    assert got.endswith("…")


def test_describe_tool_unknown_and_missing_key(tmp_path: Path) -> None:
    assert describe_tool("WebFetch", {"url": "u"}, tmp_path) == {"tool": "WebFetch", "detail": ""}
    assert describe_tool("Edit", {}, tmp_path)["detail"] == ""
    assert describe_tool("Bash", {"command": 5}, tmp_path)["detail"] == ""


# --- progress ---------------------------------------------------------------------------


def tool_msg(name: str, **tool_input: Any) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id="t", name=name, input=tool_input)], model="m")


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail = fail

    async def __call__(self, p: dict[str, Any]) -> None:
        self.calls.append(p)
        if self.fail:
            raise RuntimeError("boom")


async def test_progress_every_block_with_zero_interval(tmp_path: Path) -> None:
    fake, _ = scripted(
        tool_msg("Edit", file_path="a.py"), tool_msg("Edit", file_path="b.py"), result_msg()
    )
    rec = _Recorder()
    await SdkWorker(query_fn=fake, progress_interval_s=0).run(
        make_order(), "haiku", tmp_path, progress=rec
    )
    assert [c["detail"] for c in rec.calls] == ["a.py", "b.py"]


async def test_progress_throttled_by_interval_and_tool_change(tmp_path: Path) -> None:
    fake, _ = scripted(
        tool_msg("Edit", file_path="a.py"), tool_msg("Edit", file_path="b.py"), result_msg()
    )
    rec = _Recorder()
    await SdkWorker(query_fn=fake, progress_interval_s=3600).run(
        make_order(), "haiku", tmp_path, progress=rec
    )
    assert [c["tool"] for c in rec.calls] == ["Edit"]

    fake, _ = scripted(
        tool_msg("Edit", file_path="a.py"), tool_msg("Bash", command="ls"), result_msg()
    )
    rec = _Recorder()
    await SdkWorker(query_fn=fake, progress_interval_s=3600).run(
        make_order(), "haiku", tmp_path, progress=rec
    )
    assert [c["tool"] for c in rec.calls] == ["Edit", "Bash"]


async def test_progress_exception_is_ignored(tmp_path: Path) -> None:
    fake, _ = scripted(tool_msg("Edit", file_path="a.py"), result_msg())
    rec = _Recorder(fail=True)
    result = await SdkWorker(query_fn=fake, progress_interval_s=0).run(
        make_order(), "haiku", tmp_path, progress=rec
    )
    assert result.status == "done"
    assert len(rec.calls) == 1


async def test_progress_default_reports_every_call(tmp_path: Path) -> None:
    fake, _ = scripted(
        tool_msg("Edit", file_path="a.py"), tool_msg("Edit", file_path="b.py"), result_msg()
    )
    rec = _Recorder()
    await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path, progress=rec)
    assert [c["detail"] for c in rec.calls] == ["a.py", "b.py"]


# --- rate limit detection ---------------------------------------------------------------


def _rl_event(status: str = "rejected", resets_at: int | None = 1760000000) -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status, resets_at=resets_at),
        uuid="u",
        session_id="s",
    )


def _rl_signals() -> list[tuple[list[Any], int | None]]:
    return [
        ([_rl_event(), result_msg(is_error=True, subtype="error_during_execution")], 1760000000),
        (
            [
                AssistantMessage(content=[], model="m", error="rate_limit"),
                result_msg(is_error=True, subtype="error_during_execution"),
            ],
            None,
        ),
        ([result_msg(is_error=True, subtype="error_during_execution", api_error_status=429)], None),
    ]


@pytest.mark.parametrize(("messages", "resets_at"), _rl_signals())
async def test_worker_rate_limit_signals(
    tmp_path: Path, messages: list[Any], resets_at: int | None
) -> None:
    fake, _ = scripted(*messages)
    res = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert res.status == "error"
    assert res.rate_limited is True
    assert res.rate_limit_resets_at == resets_at
    assert res.error is not None and res.error.startswith("rate limited")


async def test_worker_rate_limit_last_reset_wins(tmp_path: Path) -> None:
    fake, _ = scripted(_rl_event(resets_at=100), _rl_event(resets_at=200), result_msg())
    res = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert res.rate_limited is True
    assert res.rate_limit_resets_at == 200


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
async def test_worker_non_rejected_event_ignored(tmp_path: Path, status: str) -> None:
    fake, _ = scripted(_rl_event(status), result_msg())
    res = await SdkWorker(query_fn=fake).run(make_order(), "haiku", tmp_path)
    assert res.status == "done"
    assert res.rate_limited is False
    assert res.rate_limit_resets_at is None
