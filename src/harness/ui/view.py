"""The run viewer's view model: what the page shows, built from a run's files. Read-only."""

from pydantic import BaseModel, ConfigDict

from harness.config import Tier
from harness.models import TaskStatus


class TaskView(BaseModel):
    """One row of the task table."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    status: TaskStatus
    tier: Tier | None
    attempt: int
    max_attempts: int | None
    escalated: bool
    cost_usd: float
    input_tokens: int
    output_tokens: int
    detail: str | None


class TierView(BaseModel):
    """Worker attempts and cost booked on one tier."""

    model_config = ConfigDict(extra="forbid")

    tier: Tier
    attempts: int
    cost_usd: float


class EventView(BaseModel):
    """One line of the event feed."""

    model_config = ConfigDict(extra="forbid")

    ts: str
    task_id: str | None
    type: str
    text: str


class RunView(BaseModel):
    """Everything the page shows for one run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    stage: str
    finished: bool
    wave: int
    waves_total: int
    cost_usd: float
    tiers: list[TierView]
    tasks: list[TaskView]
    events: list[EventView]


def _val(payload: dict, key: str) -> str:
    value = payload.get(key)
    return "?" if value is None else str(value)


def _listed(payload: dict, key: str) -> str:
    value = payload.get(key)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return "?"


def _describe(type_: str, task_id: str | None, payload: dict) -> str:
    t = "?" if task_id is None else task_id
    if type_ == "run_started":
        waves = payload.get("waves")
        n = str(len(waves)) if isinstance(waves, list) else "?"
        return f"run started: {_val(payload, 'orders')} orders in {n} waves"
    if type_ == "wave_started":
        return f"wave {_val(payload, 'wave')} started: {_listed(payload, 'tasks')}"
    if type_ == "wave_finished":
        return f"wave {_val(payload, 'wave')} finished"
    if type_ == "task_started":
        return f"{t} started on {_val(payload, 'tier')}"
    if type_ == "task_skipped":
        return f"{t} skipped: {_val(payload, 'reason')}"
    if type_ == "attempt_started":
        return f"{t} attempt {_val(payload, 'attempt')} on {_val(payload, 'tier')}"
    if type_ == "escalated":
        return f"{t} escalated {_val(payload, 'from_tier')} -> {_val(payload, 'to_tier')}"
    if type_ == "session_init":
        return f"{t} {_val(payload, 'role')} session: {_val(payload, 'model')}"
    if type_ == "worker_finished":
        return f"{t} worker finished: {_val(payload, 'status')}"
    if type_ == "verify_started":
        return f"{t} verifying attempt {_val(payload, 'attempt')}"
    if type_ == "verify_finished":
        if payload.get("passed") is True:
            return f"{t} verification passed"
        return f"{t} verification failed"
    if type_ == "task_done":
        return f"{t} done on {_val(payload, 'tier')} after {_val(payload, 'attempts')} attempts"
    if type_ == "task_failed":
        return f"{t} failed: {_val(payload, 'reason')}"
    if type_ == "task_blocked":
        reason = payload.get("reason")
        if reason:
            return f"{t} blocked: {reason}"
        return f"{t} blocked after {_val(payload, 'attempts')} attempts on {_val(payload, 'tier')}"
    if type_ == "merge_conflict":
        return f"{t} merge conflict"
    if type_ == "merged":
        return f"{t} merged into main"
    if type_ == "run_finished":
        return (
            f"run finished: {_val(payload, 'done')} done, "
            f"{_val(payload, 'failed')} failed, {_val(payload, 'blocked')} blocked"
        )
    return type_


def describe_event(type_: str, task_id: str | None, payload: dict) -> str:
    """One plain-words line for an event. Exact texts in the `decision: describe_event
    texts` context item. Never raises."""
    try:
        return _describe(type_, task_id, payload if isinstance(payload, dict) else {})
    except Exception:
        return type_
