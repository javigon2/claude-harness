import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.config import Tier
from harness.models import Budget, ContextItem, Usage, WorkOrder
from harness.worker import (
    SYSTEM_PROMPT_PATH,
    FakeWorker,
    FakeWorkerExhausted,
    SelfReportedCommand,
    Worker,
    WorkerCall,
    WorkerReport,
    WorkerResult,
    apply_report,
    build_prompt,
    fake_result,
    load_system_prompt,
    parse_report,
)

VALID = {
    "status": "done",
    "summary": "Did the thing.",
    "files_changed": ["src/a.py"],
    "acceptance": [{"cmd": "uv run pytest -q", "exit_code": 0}],
    "notes": "",
}


def block(body: str) -> str:
    return "```json\n" + body + "\n```"


def make_order(order_id: str = "a") -> WorkOrder:
    return WorkOrder(
        id=order_id,
        title="T",
        goal="G",
        tier="haiku",
        depends_on=[],
        files_owned=["src/x.py"],
        files_readonly=[],
        context=[ContextItem(label="l", content="c ```json inside")],
        steps=["s"],
        constraints=[],
        acceptance=["uv run pytest -q"],
        budget=Budget(max_turns=5, max_attempts=2),
        out_of_scope=[],
    )


# parse_report


def test_parse_valid_block() -> None:
    report = parse_report("Did it.\n\n" + block(json.dumps(VALID)))
    assert report == WorkerReport.model_validate(VALID)


def test_parse_takes_last_block() -> None:
    first = dict(VALID, summary="example")
    text = "Example:\n" + block(json.dumps(first)) + "\nReal:\n" + block(json.dumps(VALID))
    report = parse_report(text)
    assert report is not None
    assert report.summary == "Did the thing."


def test_parse_ignores_trailing_text() -> None:
    report = parse_report("Done.\n" + block(json.dumps(VALID)) + "\nThanks")
    assert report is not None
    assert report.status == "done"


def test_parse_no_block() -> None:
    assert parse_report("No block here.") is None


def test_parse_plain_fence_only() -> None:
    assert parse_report("```\n" + json.dumps(VALID) + "\n```") is None


def test_parse_invalid_json() -> None:
    assert parse_report(block("{not json")) is None


def test_parse_not_an_object() -> None:
    assert parse_report(block("[1, 2]")) is None


def test_parse_missing_key() -> None:
    data = {k: v for k, v in VALID.items() if k != "notes"}
    assert parse_report(block(json.dumps(data))) is None


@pytest.mark.parametrize("status", ["unknown", "passed"])
def test_parse_bad_status(status: str) -> None:
    assert parse_report(block(json.dumps(dict(VALID, status=status)))) is None


def test_parse_extra_key_ignored() -> None:
    report = parse_report(block(json.dumps(dict(VALID, extra=1))))
    assert report is not None
    assert not hasattr(report, "extra")


def test_parse_null_exit_code() -> None:
    data = dict(VALID, acceptance=[{"cmd": "x", "exit_code": None}])
    report = parse_report(block(json.dumps(data)))
    assert report is not None
    assert report.acceptance == [SelfReportedCommand(cmd="x", exit_code=None)]


def test_parse_trailing_spaces_on_fence() -> None:
    report = parse_report("```json \t\n" + json.dumps(VALID) + "\n```  ")
    assert report is not None


@pytest.mark.parametrize(
    "text",
    ["", "```json", "```json\n\n```", pytest.param(block("1" * 10000), id="huge-int")],
)
def test_parse_never_raises(text: str) -> None:
    assert parse_report(text) is None


# build_prompt


def expected_prompt(order: WorkOrder, tmp_path: Path) -> str:
    root = str(tmp_path.resolve())
    return (
        f"Repository root: {root}\n\n"
        f"Start every Bash command with `cd {root} && `. "
        "Every path in the work order is relative to this root.\n\n"
        "## Work order\n\n"
        "```json\n" + order.model_dump_json(indent=2) + "\n```"
    )


def test_build_prompt_exact(tmp_path: Path) -> None:
    order = make_order()
    assert build_prompt(order, tmp_path) == expected_prompt(order, tmp_path)


def test_build_prompt_with_feedback(tmp_path: Path) -> None:
    prompt = build_prompt(make_order(), tmp_path, "boom")
    assert prompt.endswith("\n\n## Retry feedback\n\nboom")
    assert prompt.startswith(expected_prompt(make_order(), tmp_path))


@pytest.mark.parametrize("feedback", ["   ", None])
def test_build_prompt_blank_feedback(tmp_path: Path, feedback: str | None) -> None:
    order = make_order()
    assert build_prompt(order, tmp_path, feedback) == expected_prompt(order, tmp_path)


def test_build_prompt_order_round_trips(tmp_path: Path) -> None:
    order = make_order()
    prompt = build_prompt(order, tmp_path)
    body = prompt.split("## Work order\n\n```json\n", 1)[1]
    body = body.rsplit("\n```", 1)[0]
    assert WorkOrder.model_validate_json(body) == order


# load_system_prompt


def test_load_system_prompt_matches_file() -> None:
    assert load_system_prompt() == SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").rstrip()


def test_load_system_prompt_content() -> None:
    text = load_system_prompt()
    assert "files_owned" in text
    assert "```json" in text


def test_load_system_prompt_shared_context() -> None:
    assert load_system_prompt(" ctx ").endswith("\n\n## Shared context\n\nctx")


def test_load_system_prompt_blank_shared_context() -> None:
    assert load_system_prompt("   ") == load_system_prompt()


def test_load_system_prompt_stable() -> None:
    assert load_system_prompt() == load_system_prompt()


# WorkerResult and apply_report


def test_cost_usd_property() -> None:
    result = WorkerResult(status="done", usage=Usage(cost_usd=1.25))
    assert result.cost_usd == 1.25 == result.usage.cost_usd


def test_worker_result_forbids_unknown_field() -> None:
    with pytest.raises(ValidationError):
        WorkerResult(status="done", bogus=1)  # type: ignore[call-arg]


def test_apply_report_copies_fields() -> None:
    result = fake_result(status="unknown", summary="old", cost_usd=0.5, input_tokens=3)
    report = WorkerReport.model_validate(dict(VALID, status="failed", notes="n"))
    applied = apply_report(result, report)
    assert applied.summary == "Did the thing."
    assert applied.files_changed == ["src/a.py"]
    assert applied.notes == "n"
    assert applied.self_acceptance == [SelfReportedCommand(cmd="uv run pytest -q", exit_code=0)]
    assert applied.status == "unknown"
    assert applied.usage == result.usage
    assert result.summary == "old"


def test_apply_report_none() -> None:
    result = fake_result(summary="keep")
    assert apply_report(result, None) == result


# FakeWorker


async def test_fake_fail_then_done(tmp_path: Path) -> None:
    worker = FakeWorker({"a": [fake_result("failed"), fake_result("done")]})
    order = make_order("a")
    assert (await worker.run(order, "haiku", tmp_path)).status == "failed"
    assert (await worker.run(order, "sonnet", tmp_path)).status == "done"


async def test_fake_queues_per_order(tmp_path: Path) -> None:
    worker = FakeWorker(
        {
            "a": [fake_result(summary="a1"), fake_result(summary="a2")],
            "b": [fake_result(summary="b1"), fake_result(summary="b2")],
        }
    )
    a, b = make_order("a"), make_order("b")
    got = [
        (await worker.run(a, "haiku", tmp_path)).summary,
        (await worker.run(b, "haiku", tmp_path)).summary,
        (await worker.run(b, "haiku", tmp_path)).summary,
        (await worker.run(a, "haiku", tmp_path)).summary,
    ]
    assert got == ["a1", "b1", "b2", "a2"]


async def test_fake_records_calls(tmp_path: Path) -> None:
    worker = FakeWorker(default=fake_result())
    await worker.run(make_order("a"), "haiku", tmp_path)
    await worker.run(make_order("b"), "opus", tmp_path / "x", "fb")
    assert worker.calls == [
        WorkerCall("a", "haiku", tmp_path, None),
        WorkerCall("b", "opus", tmp_path / "x", "fb"),
    ]


async def test_fake_exception_step(tmp_path: Path) -> None:
    worker = FakeWorker({"a": [ValueError("bad")]})
    with pytest.raises(ValueError, match="bad"):
        await worker.run(make_order("a"), "haiku", tmp_path)
    assert worker.calls == [WorkerCall("a", "haiku", tmp_path, None)]


async def test_fake_callable_steps(tmp_path: Path) -> None:
    seen: list[tuple[str, Tier, Path, str | None]] = []

    def sync_step(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        seen.append((order.id, tier, cwd, fb))
        return fake_result(summary="sync")

    async def async_step(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        seen.append((order.id, tier, cwd, fb))
        return fake_result(summary="async")

    worker = FakeWorker({"a": [sync_step, async_step]})
    order = make_order("a")
    assert (await worker.run(order, "haiku", tmp_path, "f1")).summary == "sync"
    assert (await worker.run(order, "opus", tmp_path, None)).summary == "async"
    assert seen == [("a", "haiku", tmp_path, "f1"), ("a", "opus", tmp_path, None)]


async def test_fake_default_reused(tmp_path: Path) -> None:
    worker = FakeWorker({"a": [fake_result("failed")]}, default=fake_result("done"))
    order = make_order("a")
    statuses = [(await worker.run(order, "haiku", tmp_path)).status for _ in range(4)]
    assert statuses == ["failed", "done", "done", "done"]
    assert (await worker.run(make_order("z"), "haiku", tmp_path)).status == "done"


async def test_fake_exhausted(tmp_path: Path) -> None:
    worker = FakeWorker({"a": []})
    with pytest.raises(FakeWorkerExhausted) as exc:
        await worker.run(make_order("a"), "haiku", tmp_path)
    assert str(exc.value) == "no scripted step left for order a"
    with pytest.raises(FakeWorkerExhausted):
        await worker.run(make_order("b"), "haiku", tmp_path)


async def test_fake_deep_copy(tmp_path: Path) -> None:
    scripted = fake_result(files_changed=["x.py"])
    worker = FakeWorker(default=scripted)
    order = make_order("a")
    first = await worker.run(order, "haiku", tmp_path)
    first.files_changed.append("y.py")
    first.usage.cost_usd = 9.0
    second = await worker.run(order, "haiku", tmp_path)
    assert second.files_changed == ["x.py"]
    assert second.usage.cost_usd == 0.0


async def test_fake_script_copied(tmp_path: Path) -> None:
    steps = [fake_result("failed")]
    worker = FakeWorker({"a": steps})
    steps.append(fake_result("done"))
    steps[0] = fake_result("done")
    assert worker.remaining("a") == 1
    assert (await worker.run(make_order("a"), "haiku", tmp_path)).status == "failed"


async def test_fake_remaining(tmp_path: Path) -> None:
    worker = FakeWorker({"a": [fake_result(), fake_result()]}, default=fake_result())
    assert worker.remaining("a") == 2
    assert worker.remaining("missing") == 0
    worker.add("a", fake_result())
    worker.add("b", fake_result(), fake_result())
    assert worker.remaining("a") == 3
    assert worker.remaining("b") == 2
    await worker.run(make_order("a"), "haiku", tmp_path)
    assert worker.remaining("a") == 2


def test_fake_is_worker() -> None:
    assert isinstance(FakeWorker(), Worker)


def test_fake_result_fields() -> None:
    result = fake_result("failed", "s", ("a.py",), 0.5, 10, 20)
    assert result.files_changed == ["a.py"]
    assert result.usage == Usage(input_tokens=10, output_tokens=20, cost_usd=0.5)
    assert result.cost_usd == 0.5
