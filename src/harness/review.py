"""Reviewer: one read-only agent session that checks a task's diff against its work order."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.config import MODEL_IDS, Tier
from harness.models import Usage, WorkOrder
from harness.worker import (
    WORKER_ENV,
    QueryFn,
    RateLimitWatch,
    session_init_facts,
    usage_from_result,
)

ReviewVerdict = Literal["pass", "fail", "unknown", "error"]

REVIEWER_PROMPT_PATH = Path(__file__).parent / "prompts" / "reviewer_system.md"

REVIEWER_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

REVIEW_MAX_TURNS = 20

_JSON_BLOCK = re.compile(r"^```json[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL | re.MULTILINE)


class ReviewReport(BaseModel):
    """The final JSON block of a reviewer's reply. Unknown extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "fail"]
    reasons: list[str]


class ReviewResult(BaseModel):
    """What one review returns. Only verdict "fail" fails a task; "unknown" (no valid JSON
    block) and "error" (the session failed) are recorded but do not fail it."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    reasons: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    final_text: str = ""
    error: str | None = None
    session_init: dict[str, Any] | None = None
    rate_limited: bool = False
    rate_limit_resets_at: int | None = None


@runtime_checkable
class Reviewer(Protocol):
    async def review(self, order: WorkOrder, diff: str, tier: Tier, cwd: Path) -> ReviewResult:
        """Review `diff` (the task's changes) against `order` on `tier`, with read-only
        access to the worktree `cwd`. Failures come back as verdict "error", never as an
        exception (cancellation still propagates)."""
        ...


def parse_verdict(text: str) -> ReviewReport | None:
    """Take the LAST fenced ```json block (same fence rules as harness.worker.parse_report:
    the _JSON_BLOCK regex above), json.loads its body and validate it as a ReviewReport.
    Return None if there is no block, the body is not valid JSON (catch ValueError and
    RecursionError), it is not a JSON object, or validation fails. Never raises."""
    try:
        matches = _JSON_BLOCK.findall(text)
    except TypeError:
        return None
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ReviewReport.model_validate(data)
    except ValidationError:
        return None


def load_reviewer_prompt() -> str:
    """Read REVIEWER_PROMPT_PATH (UTF-8) and strip trailing whitespace."""
    return REVIEWER_PROMPT_PATH.read_text(encoding="utf-8").rstrip()


def build_review_prompt(order: WorkOrder, diff: str) -> str:
    """The user message: "## Work order", then the order as a ```json fenced block
    (order.model_dump_json(indent=2)), then "## Diff", then the diff as a ```diff fenced
    block. Exact format in the `decision: review prompt format` context item."""
    body = "(empty diff)" if diff.strip() == "" else diff.rstrip("\n")
    return (
        "## Work order\n\n```json\n"
        + order.model_dump_json(indent=2)
        + "\n```\n\n"
        + "## Diff\n\n```diff\n"
        + body
        + "\n```"
    )


class SdkReviewer:
    """A Reviewer backed by the Claude Agent SDK: one query() session per review."""

    def __init__(self, query_fn: QueryFn | None = None) -> None:
        """self.system_prompt = load_reviewer_prompt(), computed once here.
        self._query = query_fn or query (same keyword call shape as SdkWorker:
        query_fn(prompt=<str>, options=<ClaudeAgentOptions>))."""
        self.system_prompt = load_reviewer_prompt()
        self._query: QueryFn = query_fn or query

    def options(self, tier: Tier, cwd: Path) -> ClaudeAgentOptions:
        """The exact ClaudeAgentOptions for one review; values in the
        `decision: reviewer SDK options` context item."""
        return ClaudeAgentOptions(
            model=MODEL_IDS[tier],
            system_prompt=self.system_prompt,
            tools=list(REVIEWER_TOOLS),
            allowed_tools=list(REVIEWER_TOOLS),
            skills=[],
            permission_mode="dontAsk",
            cwd=str(Path(cwd).resolve()),
            max_turns=REVIEW_MAX_TURNS,
            setting_sources=[],
            strict_mcp_config=True,
            verbatim_prompts=True,
            env=dict(WORKER_ENV),
        )

    async def review(self, order: WorkOrder, diff: str, tier: Tier, cwd: Path) -> ReviewResult:
        """Run one session; algorithm in the `decision: review algorithm` context item.
        Catches Exception (not BaseException), so cancellation propagates."""
        prompt = build_review_prompt(order, diff)
        options = self.options(tier, cwd)
        result_msg: ResultMessage | None = None
        last_text = ""
        error: str | None = None
        session_init: dict[str, Any] | None = None
        watch = RateLimitWatch()
        try:
            async for message in self._query(prompt=prompt, options=options):
                watch.observe(message)
                if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                    text = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                    if text.strip():
                        last_text = text
                elif isinstance(message, ResultMessage):
                    result_msg = message
                elif isinstance(message, SystemMessage):
                    if session_init is None:
                        session_init = session_init_facts(message)
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

        verdict: ReviewVerdict
        reasons: list[str] = []
        if watch.limited:
            verdict = "error"
            if error is None and result_msg is not None and result_msg.is_error:
                error = f"result {result_msg.subtype}"
            error = watch.error(error)
        elif error is not None:
            verdict = "error"
        elif result_msg is None:
            verdict = "error"
            error = "session ended without a result message"
        elif result_msg.is_error:
            verdict = "error"
            error = f"result {result_msg.subtype}"
            if isinstance(result_msg.errors, list) and result_msg.errors:
                error += ": " + "; ".join(str(e) for e in result_msg.errors)
        else:
            report = parse_verdict(final_text)
            if report is None:
                verdict = "unknown"
            else:
                verdict = report.verdict
                reasons = list(report.reasons)

        return ReviewResult(
            verdict=verdict,
            reasons=reasons,
            usage=usage_from_result(result_msg),
            final_text=final_text,
            error=error,
            session_init=session_init,
            rate_limited=watch.limited,
            rate_limit_resets_at=watch.resets_at if watch.limited else None,
        )


@dataclass(frozen=True)
class ReviewCall:
    """One recorded FakeReviewer.review call."""

    order_id: str
    diff: str
    tier: Tier
    cwd: Path


class FakeReviewer:
    """A scriptable Reviewer for tests: each review() call takes the next scripted step."""

    def __init__(
        self,
        steps: Sequence[ReviewResult | BaseException] = (),
        default: ReviewResult | None = None,
    ) -> None:
        """Copy `steps` into a list (later changes to the caller's sequence have no effect).
        `default` is used when the list is empty. self.calls starts as an empty
        list[ReviewCall]."""
        self._steps: list[ReviewResult | BaseException] = list(steps)
        self._default = default
        self.calls: list[ReviewCall] = []

    async def review(self, order: WorkOrder, diff: str, tier: Tier, cwd: Path) -> ReviewResult:
        """Append ReviewCall(order.id, diff, tier, cwd) to self.calls FIRST. Then pop the
        front step, or use `default` if no step is left; if there is neither, raise
        RuntimeError("no scripted review left"). A ReviewResult step is returned as
        step.model_copy(deep=True); an exception step is raised. `default` is never
        consumed."""
        self.calls.append(ReviewCall(order.id, diff, tier, cwd))
        if self._steps:
            step = self._steps.pop(0)
        elif self._default is not None:
            step = self._default
        else:
            raise RuntimeError("no scripted review left")
        if isinstance(step, BaseException):
            raise step
        return step.model_copy(deep=True)


def fake_review(
    verdict: ReviewVerdict = "pass", reasons: Sequence[str] = (), cost_usd: float = 0.0
) -> ReviewResult:
    """Shorthand for tests: ReviewResult(verdict=verdict, reasons=list(reasons),
    usage=Usage(cost_usd=cost_usd))."""
    return ReviewResult(verdict=verdict, reasons=list(reasons), usage=Usage(cost_usd=cost_usd))
