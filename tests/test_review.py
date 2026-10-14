import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from harness.config import MODEL_IDS, Tier
from harness.models import Budget, ContextItem, Usage, WorkOrder
from harness.review import (
    REVIEW_MAX_TURNS,
    REVIEWER_PROMPT_PATH,
    REVIEWER_TOOLS,
    FakeReviewer,
    ReviewCall,
    Reviewer,
    ReviewResult,
    SdkReviewer,
    build_review_prompt,
    fake_review,
    load_reviewer_prompt,
    parse_verdict,
)
from harness.worker import WORKER_ENV

DIFF = "diff --git a/x b/x\n+1\n"


def block(report: Any) -> str:
    return "Looks fine.\n\n```json\n" + json.dumps(report) + "\n```"


def make_order(order_id: str = "a") -> WorkOrder:
    return WorkOrder(
        id=order_id,
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
        budget=Budget(max_turns=5, max_attempts=2),
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
        "result": block({"verdict": "pass", "reasons": []}),
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def text_msg(text: str, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


# parse_verdict


def test_parse_verdict_pass() -> None:
    report = parse_verdict(block({"verdict": "pass", "reasons": []}))
    assert report is not None
    assert report.verdict == "pass"
    assert report.reasons == []


def test_parse_verdict_fail_with_reasons() -> None:
    report = parse_verdict(block({"verdict": "fail", "reasons": ["a.py:1: bad", "b.py: x"]}))
    assert report is not None
    assert report.verdict == "fail"
    assert report.reasons == ["a.py:1: bad", "b.py: x"]


def test_parse_verdict_last_block_wins() -> None:
    text = (
        block({"verdict": "pass", "reasons": []})
        + "\n\nOn reflection:\n\n"
        + block({"verdict": "fail", "reasons": ["r"]})
    )
    report = parse_verdict(text)
    assert report is not None
    assert report.verdict == "fail"
    assert report.reasons == ["r"]


def test_parse_verdict_extra_keys_ignored() -> None:
    report = parse_verdict(block({"verdict": "pass", "reasons": ["n"], "extra": 1}))
    assert report is not None
    assert report.verdict == "pass"
    assert report.reasons == ["n"]


@pytest.mark.parametrize(
    "text",
    [
        "no block at all",
        "```json\n{not json}\n```",
        "```json\n[1, 2]\n```",
        block({"verdict": "maybe", "reasons": []}),
        block({"verdict": "pass"}),
    ],
    ids=["no-block", "invalid-json", "array", "bad-verdict", "missing-reasons"],
)
def test_parse_verdict_none(text: str) -> None:
    assert parse_verdict(text) is None


def test_parse_verdict_text_after_block_ignored() -> None:
    report = parse_verdict(block({"verdict": "fail", "reasons": ["r"]}) + "\n\nThanks, bye.")
    assert report is not None
    assert report.verdict == "fail"


# build_review_prompt and load_reviewer_prompt


def test_build_review_prompt_exact() -> None:
    order = make_order()
    expected = (
        "## Work order\n\n```json\n"
        + order.model_dump_json(indent=2)
        + "\n```\n\n## Diff\n\n```diff\ndiff --git a/x b/x\n+1\n```"
    )
    assert build_review_prompt(order, DIFF) == expected


def test_build_review_prompt_empty_diff() -> None:
    prompt = build_review_prompt(make_order(), "  \n\n")
    assert prompt.endswith("## Diff\n\n```diff\n(empty diff)\n```")


def test_build_review_prompt_order_round_trips() -> None:
    order = make_order()
    prompt = build_review_prompt(order, DIFF)
    start = prompt.index("```json\n") + len("```json\n")
    end = prompt.index("\n```", start)
    assert WorkOrder.model_validate_json(prompt[start:end]) == order


def test_load_reviewer_prompt() -> None:
    text = load_reviewer_prompt()
    assert text == REVIEWER_PROMPT_PATH.read_text(encoding="utf-8").rstrip()
    assert '"verdict"' in text


# SdkReviewer


@pytest.mark.parametrize("tier", ["sonnet", "opus"])
def test_options(tier: Tier, tmp_path: Path) -> None:
    fake, _ = scripted()
    reviewer = SdkReviewer(query_fn=fake)
    opts = reviewer.options(tier, tmp_path)
    assert opts.model == MODEL_IDS[tier]
    assert opts.system_prompt == load_reviewer_prompt()
    assert opts.tools == list(REVIEWER_TOOLS) == ["Read", "Glob", "Grep"]
    assert opts.allowed_tools == list(REVIEWER_TOOLS)
    assert opts.permission_mode == "dontAsk"
    assert opts.cwd == str(tmp_path.resolve())
    assert opts.max_turns == REVIEW_MAX_TURNS == 20
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.verbatim_prompts is True
    assert opts.env == WORKER_ENV


async def test_query_receives_prompt_and_options(tmp_path: Path) -> None:
    fake, seen = scripted(result_msg())
    reviewer = SdkReviewer(query_fn=fake)
    order = make_order()
    await reviewer.review(order, DIFF, "sonnet", tmp_path)
    assert seen["prompt"] == build_review_prompt(order, DIFF)
    assert seen["options"] == reviewer.options("sonnet", tmp_path)


async def test_fail_verdict_and_usage(tmp_path: Path) -> None:
    text = block({"verdict": "fail", "reasons": ["src/y.py: not owned"]})
    fake, _ = scripted(result_msg(result=text))
    res = await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)
    assert res.verdict == "fail"
    assert res.reasons == ["src/y.py: not owned"]
    assert res.usage.input_tokens == 5
    assert res.usage.output_tokens == 7
    assert res.usage.cost_usd == 0.25
    assert res.final_text == text
    assert res.error is None


async def test_no_block_unknown_keeps_usage(tmp_path: Path) -> None:
    fake, _ = scripted(result_msg(result="I could not decide."))
    res = await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)
    assert res.verdict == "unknown"
    assert res.reasons == []
    assert res.usage.cost_usd == 0.25
    assert res.usage.input_tokens == 5
    assert res.error is None


async def test_is_error_result(tmp_path: Path) -> None:
    msg = result_msg(subtype="error_max_turns", is_error=True, errors=["out of turns"], result=None)
    fake, _ = scripted(msg)
    res = await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)
    assert res.verdict == "error"
    assert res.error == "result error_max_turns: out of turns"


async def test_exception_after_result_keeps_usage(tmp_path: Path) -> None:
    fake, _ = scripted(result_msg(), raise_after=RuntimeError("cli died"))
    res = await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)
    assert res.verdict == "error"
    assert res.error == "RuntimeError: cli died"
    assert res.usage.cost_usd == 0.25
    assert res.usage.output_tokens == 7


async def test_no_messages(tmp_path: Path) -> None:
    fake, _ = scripted()
    res = await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)
    assert res.verdict == "error"
    assert res.error == "session ended without a result message"
    assert res.usage == Usage()


async def test_cancelled_error_propagates(tmp_path: Path) -> None:
    fake, _ = scripted(raise_after=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)


async def test_result_none_falls_back_to_last_top_level_text(tmp_path: Path) -> None:
    top = block({"verdict": "fail", "reasons": ["top"]})
    sub = block({"verdict": "pass", "reasons": ["sub"]})
    fake, _ = scripted(text_msg(top), text_msg(sub, parent="tool-1"), result_msg(result=None))
    res = await SdkReviewer(query_fn=fake).review(make_order(), DIFF, "opus", tmp_path)
    assert res.final_text == top
    assert res.verdict == "fail"
    assert res.reasons == ["top"]


def test_sdk_reviewer_is_reviewer() -> None:
    fake, _ = scripted()
    assert isinstance(SdkReviewer(query_fn=fake), Reviewer)


# FakeReviewer


async def test_fake_steps_in_order(tmp_path: Path) -> None:
    fr = FakeReviewer([fake_review("pass"), fake_review("fail", ["r"])])
    first = await fr.review(make_order(), DIFF, "opus", tmp_path)
    second = await fr.review(make_order(), DIFF, "opus", tmp_path)
    assert first.verdict == "pass"
    assert second.verdict == "fail"
    assert second.reasons == ["r"]


async def test_fake_records_calls_even_when_raising(tmp_path: Path) -> None:
    fr = FakeReviewer([fake_review(), ValueError("boom")])
    await fr.review(make_order("a"), "d1", "sonnet", tmp_path)
    with pytest.raises(ValueError, match="boom"):
        await fr.review(make_order("b"), "d2", "opus", tmp_path)
    assert fr.calls == [
        ReviewCall("a", "d1", "sonnet", tmp_path),
        ReviewCall("b", "d2", "opus", tmp_path),
    ]


async def test_fake_default_reused_never_consumed(tmp_path: Path) -> None:
    default = fake_review("fail", ["d"])
    fr = FakeReviewer([fake_review("pass")], default=default)
    results = [await fr.review(make_order(), DIFF, "opus", tmp_path) for _ in range(3)]
    assert [r.verdict for r in results] == ["pass", "fail", "fail"]
    assert results[1] == default
    assert results[1] is not results[2]


async def test_fake_no_step_no_default(tmp_path: Path) -> None:
    fr = FakeReviewer()
    with pytest.raises(RuntimeError, match="no scripted review left"):
        await fr.review(make_order(), DIFF, "opus", tmp_path)
    assert len(fr.calls) == 1


async def test_fake_returns_deep_copies(tmp_path: Path) -> None:
    step = fake_review("fail", ["r"])
    fr = FakeReviewer([step])
    res = await fr.review(make_order(), DIFF, "opus", tmp_path)
    assert res == step
    assert res is not step
    res.reasons.append("x")
    assert step.reasons == ["r"]


async def test_fake_copies_callers_list(tmp_path: Path) -> None:
    steps: list[ReviewResult | BaseException] = [fake_review("pass")]
    fr = FakeReviewer(steps)
    steps.append(fake_review("fail"))
    steps.clear()
    res = await fr.review(make_order(), DIFF, "opus", tmp_path)
    assert res.verdict == "pass"
    with pytest.raises(RuntimeError, match="no scripted review left"):
        await fr.review(make_order(), DIFF, "opus", tmp_path)


def test_fake_reviewer_is_reviewer() -> None:
    assert isinstance(FakeReviewer(), Reviewer)


def test_fake_review_fields() -> None:
    res = fake_review("fail", ["r"], 0.5)
    assert res.verdict == "fail"
    assert res.reasons == ["r"]
    assert res.usage == Usage(cost_usd=0.5)
    assert res.final_text == ""
    assert res.error is None
