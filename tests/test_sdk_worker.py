import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from harness.config import MODEL_IDS, Tier
from harness.models import Budget, ContextItem, Usage, WorkOrder
from harness.worker import (
    WORKER_ENV,
    WORKER_TOOLS,
    SdkWorker,
    Worker,
    build_prompt,
    load_system_prompt,
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
