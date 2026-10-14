"""Worker interface: one work order in, one agent session, one WorkerResult out."""

import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.config import MODEL_IDS, Tier
from harness.models import Usage, WorkOrder

WorkerStatus = Literal["done", "failed", "unknown", "error"]

SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "worker_system.md"

WORKER_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")

WORKER_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

QueryFn = Callable[..., AsyncIterator[Any]]

_JSON_BLOCK = re.compile(r"^```json[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL | re.MULTILINE)


class SelfReportedCommand(BaseModel):
    """One entry of the worker's self-reported `acceptance` list. Not trusted."""

    model_config = ConfigDict(extra="forbid")

    cmd: str
    exit_code: int | None


class WorkerReport(BaseModel):
    """The final JSON block of a worker's reply, as the worker wrote it.
    Unknown extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["done", "failed"]
    summary: str
    files_changed: list[str]
    acceptance: list[SelfReportedCommand]
    notes: str


class WorkerResult(BaseModel):
    """What one worker attempt returns. `status` is the worker's own claim (or why there is
    none); the verifier decides pass or fail."""

    model_config = ConfigDict(extra="forbid")

    status: WorkerStatus
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    notes: str = ""
    self_acceptance: list[SelfReportedCommand] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    num_turns: int = 0
    duration_ms: int = 0
    session_id: str | None = None
    final_text: str = ""
    error: str | None = None

    @property
    def cost_usd(self) -> float:
        """The attempt's cost in USD: self.usage.cost_usd."""
        return self.usage.cost_usd


@runtime_checkable
class Worker(Protocol):
    async def run(
        self, order: WorkOrder, tier: Tier, cwd: Path, retry_feedback: str | None = None
    ) -> WorkerResult:
        """Run one attempt of `order` on `tier` in the worktree `cwd`. Model and SDK
        failures come back as status "error", never as an exception."""
        ...


def parse_report(text: str) -> WorkerReport | None:
    """Find every fenced block that opens with a line of exactly three backticks followed by
    `json` (optional trailing spaces or tabs) and closes with a line of three backticks. Take
    the LAST such block, json.loads its body and validate it as a WorkerReport. Return None
    if there is no such block, the body is not valid JSON, it is not a JSON object, or
    validation fails. Never raises. Text after the last block is ignored."""
    try:
        matches = _JSON_BLOCK.findall(text)
    except TypeError:
        return None
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except (ValueError, RecursionError):
        # ValueError covers json.JSONDecodeError and the int-digit limit error.
        return None
    if not isinstance(data, dict):
        return None
    try:
        return WorkerReport.model_validate(data)
    except ValidationError:
        return None


def apply_report(result: WorkerResult, report: WorkerReport | None) -> WorkerResult:
    """Return a copy of `result` with summary, files_changed, notes and self_acceptance taken
    from `report` (acceptance -> self_acceptance). If report is None, return `result`
    unchanged. Never changes status, usage or the other fields."""
    if report is None:
        return result
    return result.model_copy(
        update={
            "summary": report.summary,
            "files_changed": list(report.files_changed),
            "notes": report.notes,
            "self_acceptance": [c.model_copy() for c in report.acceptance],
        }
    )


def load_system_prompt(shared_context: str = "") -> str:
    """Read SYSTEM_PROMPT_PATH (UTF-8) and strip trailing whitespace. If shared_context.strip()
    is non-empty, append "\\n\\n## Shared context\\n\\n" + shared_context.strip(). Return the
    text. The result must not depend on any order, tier, path or time, so it stays
    byte-identical across calls and the prompt cache applies."""
    text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").rstrip()
    extra = shared_context.strip()
    if extra:
        text += "\n\n## Shared context\n\n" + extra
    return text


def build_prompt(order: WorkOrder, cwd: Path, retry_feedback: str | None = None) -> str:
    """The user message for one attempt: the repository-root preamble, then the order, then
    the retry feedback (if retry_feedback is not None and retry_feedback.strip() is
    non-empty). Exact format in the order's `decision: prompt format` context item."""
    root = str(Path(cwd).resolve())
    parts = [
        f"Repository root: {root}",
        f"Start every Bash command with `cd {root} && `. "
        "Every path in the work order is relative to this root.",
        "## Work order",
        "```json\n" + order.model_dump_json(indent=2) + "\n```",
    ]
    if retry_feedback is not None and retry_feedback.strip():
        parts += ["## Retry feedback", retry_feedback.strip()]
    return "\n\n".join(parts)


def _int(value: Any) -> int:
    return int(value) if value is not None else 0


def usage_from_result(message: ResultMessage | None) -> Usage:
    """Token counts and cost of one session, from its ResultMessage. Rules in the order's
    `decision: usage` context item. Usage() (all zeros) if message is None."""
    if message is None:
        return Usage()
    model_usage = message.model_usage
    has_model_usage = isinstance(model_usage, dict) and bool(model_usage)
    if has_model_usage:
        values = list(model_usage.values())
        input_tokens = sum(_int(v.get("inputTokens")) for v in values)
        output_tokens = sum(_int(v.get("outputTokens")) for v in values)
        cache_read = sum(_int(v.get("cacheReadInputTokens")) for v in values)
        cache_creation = sum(_int(v.get("cacheCreationInputTokens")) for v in values)
    else:
        raw = message.usage or {}
        input_tokens = _int(raw.get("input_tokens"))
        output_tokens = _int(raw.get("output_tokens"))
        cache_read = _int(raw.get("cache_read_input_tokens"))
        cache_creation = _int(raw.get("cache_creation_input_tokens"))
    if message.total_cost_usd is not None:
        cost = float(message.total_cost_usd)
    elif has_model_usage:
        cost = sum(float(v.get("costUSD") or 0.0) for v in model_usage.values())
    else:
        cost = 0.0
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cost_usd=cost,
    )


class SdkWorker:
    """A Worker backed by the Claude Agent SDK: one query() session per attempt."""

    def __init__(self, shared_context: str = "", query_fn: QueryFn | None = None) -> None:
        """self.system_prompt = load_system_prompt(shared_context), computed once here.
        self._query = query_fn or query (tests pass a fake with the same keyword call shape:
        query_fn(prompt=<str>, options=<ClaudeAgentOptions>))."""
        self.system_prompt = load_system_prompt(shared_context)
        self._query: QueryFn = query_fn or query

    def options(self, order: WorkOrder, tier: Tier, cwd: Path) -> ClaudeAgentOptions:
        """The exact ClaudeAgentOptions for one attempt; field values in the order's
        `decision: SDK options` context item."""
        return ClaudeAgentOptions(
            model=MODEL_IDS[tier],
            system_prompt=self.system_prompt,
            tools=list(WORKER_TOOLS),
            allowed_tools=list(WORKER_TOOLS),
            permission_mode="acceptEdits",
            cwd=str(Path(cwd).resolve()),
            max_turns=order.budget.max_turns,
            setting_sources=[],
            strict_mcp_config=True,
            verbatim_prompts=True,
            env=dict(WORKER_ENV),
        )

    async def run(
        self, order: WorkOrder, tier: Tier, cwd: Path, retry_feedback: str | None = None
    ) -> WorkerResult:
        """Run one session and turn it into a WorkerResult; algorithm in the order's
        `decision: run algorithm` context item. Never raises for SDK or model failures
        (catches Exception, not BaseException, so cancellation still propagates)."""
        prompt = build_prompt(order, cwd, retry_feedback)
        options = self.options(order, tier, cwd)
        result_msg: ResultMessage | None = None
        last_text = ""
        error: str | None = None
        try:
            async for message in self._query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                    text = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                    if text.strip():
                        last_text = text
                elif isinstance(message, ResultMessage):
                    result_msg = message
        except Exception as err:  # not BaseException: cancellation must propagate
            error = f"{type(err).__name__}: {err}"[:2000]

        if (
            result_msg is not None
            and isinstance(result_msg.result, str)
            and result_msg.result.strip()
        ):
            final_text = result_msg.result
        else:
            final_text = last_text
        report = parse_report(final_text)

        status: WorkerStatus
        if error is not None:
            status = "error"
        elif result_msg is None:
            status = "error"
            error = "session ended without a result message"
        elif result_msg.is_error:
            status = "error"
            error = f"result {result_msg.subtype}"
            if isinstance(result_msg.errors, list) and result_msg.errors:
                error += ": " + "; ".join(str(e) for e in result_msg.errors)
        elif report is None:
            status = "unknown"
        else:
            status = report.status

        result = WorkerResult(
            status=status,
            usage=usage_from_result(result_msg),
            num_turns=result_msg.num_turns if result_msg else 0,
            duration_ms=result_msg.duration_ms if result_msg else 0,
            session_id=result_msg.session_id if result_msg else None,
            final_text=final_text,
            error=error,
        )
        return apply_report(result, report)


FakeStep = (
    WorkerResult
    | BaseException
    | Callable[[WorkOrder, Tier, Path, str | None], WorkerResult | Awaitable[WorkerResult]]
)


@dataclass(frozen=True)
class WorkerCall:
    """One recorded FakeWorker.run call."""

    order_id: str
    tier: Tier
    cwd: Path
    retry_feedback: str | None


class FakeWorkerExhausted(RuntimeError):
    """FakeWorker.run was called for an order with no scripted step left and no default."""


class FakeWorker:
    """A scriptable Worker for tests. Each order id has its own queue of steps; every run()
    call pops the next step for order.id."""

    def __init__(
        self,
        script: Mapping[str, Sequence[FakeStep]] | None = None,
        default: FakeStep | None = None,
    ) -> None:
        """Copy each sequence in `script` into a per-order list (later changes to the
        caller's sequences have no effect). `default` is used when an order's queue is empty.
        self.calls starts as an empty list[WorkerCall]."""
        self._queues: dict[str, list[FakeStep]] = {
            order_id: list(steps) for order_id, steps in (script or {}).items()
        }
        self._default = default
        self.calls: list[WorkerCall] = []

    def add(self, order_id: str, *steps: FakeStep) -> None:
        """Append steps to the end of order_id's queue (creating it if missing)."""
        self._queues.setdefault(order_id, []).extend(steps)

    def remaining(self, order_id: str) -> int:
        """Number of scripted steps still queued for order_id (0 if none). Ignores default."""
        return len(self._queues.get(order_id, []))

    async def run(
        self, order: WorkOrder, tier: Tier, cwd: Path, retry_feedback: str | None = None
    ) -> WorkerResult:
        """Append WorkerCall(order.id, tier, cwd, retry_feedback) to self.calls FIRST, then
        take the next step: pop the front of order.id's queue, or use `default` if the queue
        is empty or missing; if there is neither, raise
        FakeWorkerExhausted(f"no scripted step left for order {order.id}").
        - WorkerResult: return step.model_copy(deep=True).
        - BaseException instance: raise it.
        - callable: call step(order, tier, cwd, retry_feedback); if the return value is
          awaitable, await it; return the WorkerResult.
        A `default` step is never consumed: it is reused for every call that needs it."""
        self.calls.append(WorkerCall(order.id, tier, cwd, retry_feedback))
        queue = self._queues.get(order.id)
        if queue:
            step = queue.pop(0)
        elif self._default is not None:
            step = self._default
        else:
            raise FakeWorkerExhausted(f"no scripted step left for order {order.id}")
        if isinstance(step, WorkerResult):
            return step.model_copy(deep=True)
        if isinstance(step, BaseException):
            raise step
        value = step(order, tier, cwd, retry_feedback)
        if inspect.isawaitable(value):
            value = await value
        return value


def fake_result(
    status: WorkerStatus = "done",
    summary: str = "",
    files_changed: Sequence[str] = (),
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> WorkerResult:
    """Shorthand for scripting FakeWorker: a WorkerResult with these fields, the files as a
    list, and Usage(input_tokens=..., output_tokens=..., cost_usd=...)."""
    return WorkerResult(
        status=status,
        summary=summary,
        files_changed=list(files_changed),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd),
    )
