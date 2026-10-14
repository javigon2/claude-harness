"""Router: run one order through worker attempts and verification, escalating up the tiers."""

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from harness.config import TIER_LADDER, Tier
from harness.events import EventLog
from harness.models import AttemptRecord, TaskState, Usage, WorkOrder
from harness.review import Reviewer
from harness.verify import DEFAULT_TIMEOUT_S, format_feedback, verify
from harness.worker import Worker, WorkerResult

StateCallback = Callable[[TaskState], Awaitable[None]]

REPLAN_FEEDBACK_LINES = 40


def tier_for_attempt(start: Tier, attempt: int) -> Tier:
    """The tier for 1-based `attempt` of an order whose tier is `start`: one step up
    TIER_LADDER per earlier attempt, staying at the top once there.
    TIER_LADDER[min(TIER_LADDER.index(start) + attempt - 1, len(TIER_LADDER) - 1)].
    Raises ValueError if attempt < 1."""
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return TIER_LADDER[min(TIER_LADDER.index(start) + attempt - 1, len(TIER_LADDER) - 1)]


def replan_note(order: WorkOrder, history: Sequence[AttemptRecord]) -> str:
    """The short note for a blocked task. Exact format in the `decision: replan note`
    context item."""
    tiers = ", ".join(r.tier for r in history)
    lines = [
        f"Task {order.id} is blocked after {len(history)} attempts (tiers: {tiers}); "
        "the last one ran on opus.",
        "Last verifier output:",
    ]
    if history and history[-1].feedback.strip():
        lines.extend(history[-1].feedback.splitlines()[:REPLAN_FEEDBACK_LINES])
    else:
        lines.append("(none)")
    lines.append(
        "Next step: re-plan this order. Split it, paste the missing context, or fix its "
        "acceptance commands."
    )
    return "\n".join(lines)


class Router:
    def __init__(
        self,
        worker: Worker,
        events: EventLog,
        *,
        reviewer: Reviewer | None = None,
        review_tier: Tier = "sonnet",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        on_state: StateCallback | None = None,
    ) -> None:
        """Store every argument on self under the same name (self.worker, self.events,
        self.reviewer, self.review_tier, self.timeout_s, self.on_state)."""
        self.worker = worker
        self.events = events
        self.reviewer = reviewer
        self.review_tier = review_tier
        self.timeout_s = timeout_s
        self.on_state = on_state

    async def _changed(self, state: TaskState) -> None:
        """await self.on_state(state) if on_state is set; otherwise do nothing."""
        if self.on_state is not None:
            await self.on_state(state)

    async def route(
        self, order: WorkOrder, cwd: Path, state: TaskState, base: str = "HEAD"
    ) -> TaskState:
        """Run attempts of `order` in the worktree `cwd` until one passes verification or
        order.budget.max_attempts attempts are used. Mutates and returns `state` (the
        caller passes a fresh TaskState(task_id=order.id)). Algorithm, events and final
        states in the `decision: route algorithm` context item. Never raises except
        CancelledError."""

        async def emit(type_: str, payload: dict) -> None:
            await self.events.emit(type_, task_id=order.id, payload=payload)

        feedback: str | None = None
        prev_tier: Tier | None = None
        n = 0
        tier: Tier = order.tier
        for n in range(1, order.budget.max_attempts + 1):
            tier = tier_for_attempt(order.tier, n)
            if prev_tier is not None and tier != prev_tier:
                await emit("escalated", {"attempt": n, "from_tier": prev_tier, "to_tier": tier})

            state.status = "running"
            state.attempts = n
            state.tier_used = tier
            await self._changed(state)
            await emit("attempt_started", {"attempt": n, "tier": tier})

            try:
                result = await self.worker.run(order, tier, cwd, feedback)
            except Exception as err:
                result = WorkerResult(status="error", error=f"{type(err).__name__}: {err}"[:2000])
            if result.session_init is not None:
                await emit(
                    "session_init",
                    {"role": "worker", "attempt": n, "tier": tier, **result.session_init},
                )
            await emit(
                "worker_finished",
                {
                    "attempt": n,
                    "tier": tier,
                    "status": result.status,
                    "summary": result.summary,
                    "error": result.error,
                    "cost_usd": result.usage.cost_usd,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "num_turns": result.num_turns,
                    "duration_ms": result.duration_ms,
                },
            )

            state.status = "verifying"
            state.usage = state.usage.plus(result.usage)
            await self._changed(state)
            await emit("verify_started", {"attempt": n})

            vr = await verify(
                order,
                cwd,
                base=base,
                timeout_s=self.timeout_s,
                reviewer=self.reviewer,
                review_tier=self.review_tier,
            )
            review_usage = vr.review.usage if vr.review is not None else Usage()
            fb = format_feedback(vr)
            if vr.review is not None and vr.review.session_init is not None:
                await emit(
                    "session_init",
                    {
                        "role": "reviewer",
                        "attempt": n,
                        "tier": self.review_tier,
                        **vr.review.session_init,
                    },
                )
            await emit(
                "verify_finished",
                {
                    "attempt": n,
                    "passed": vr.passed,
                    "failed_commands": [c.cmd for c in vr.commands if not c.passed],
                    "outside_files": vr.outside_files,
                    "scope_error": vr.scope_error,
                    "review_verdict": vr.review.verdict if vr.review is not None else None,
                    "review_reasons": vr.review.reasons if vr.review is not None else [],
                },
            )

            state.usage = state.usage.plus(review_usage)
            state.history.append(
                AttemptRecord(
                    attempt=n,
                    tier=tier,
                    worker_status=result.status,
                    passed=vr.passed,
                    usage=result.usage.plus(review_usage),
                    summary=result.summary,
                    feedback=fb,
                )
            )

            if vr.passed:
                state.status = "done"
                await self._changed(state)
                await emit(
                    "task_done", {"attempts": n, "tier": tier, "cost_usd": state.usage.cost_usd}
                )
                return state
            feedback = fb
            prev_tier = tier

        if tier == "opus":
            state.status = "blocked"
            state.replan_note = replan_note(order, state.history)
            await self._changed(state)
            await emit(
                "task_blocked", {"attempts": n, "tier": tier, "replan_note": state.replan_note}
            )
        else:
            state.status = "failed"
            state.error = f"budget exhausted after {n} attempts; last tier {tier}"
            await self._changed(state)
            await emit("task_failed", {"attempts": n, "tier": tier, "reason": state.error})
        return state
