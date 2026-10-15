"""The run viewer's view model: what the page shows, built from a run's files. Read-only."""

import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from harness.config import Tier
from harness.events import EVENTS_FILE
from harness.models import Event, TaskStatus
from harness.plan import load_plan
from harness.report import money, tier_totals
from harness.state import STATE_FILE, load_state, run_dir, runs_root


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
    activity: str | None


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
    phase: Literal["running", "stopped", "finished"]
    stopped_reason: str | None
    idle_minutes: int | None
    wave: int
    waves_total: int
    cost_usd: float
    tiers: list[TierView]
    tasks: list[TaskView]
    events: list[EventView]


IDLE_MINUTES = 10  # a running run whose events.jsonl is this stale is probably dead


def _val(payload: dict, key: str) -> str:
    value = payload.get(key)
    return "?" if value is None else str(value)


def _listed(payload: dict, key: str) -> str:
    value = payload.get(key)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return "?"


def _progress(payload: dict) -> str:
    tool = _val(payload, "tool")
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return f"{tool}: {detail}"
    return tool


def _money(payload: dict, key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "?"
    return money(value)


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
    if type_ == "worker_progress":
        return f"{t} {_progress(payload)}"
    if type_ == "task_paused":
        return f"{t} paused: {_val(payload, 'reason')}"
    if type_ == "run_stopping":
        return f"run stopping: {_val(payload, 'reason')}"
    if type_ == "budget_exceeded":
        return (
            f"cost cap reached: {_money(payload, 'cost_usd')} of {_money(payload, 'max_cost_usd')}"
        )
    if type_ == "run_finished":
        text = (
            f"run finished: {_val(payload, 'done')} done, "
            f"{_val(payload, 'failed')} failed, {_val(payload, 'blocked')} blocked"
        )
        paused = payload.get("paused")
        if isinstance(paused, int) and not isinstance(paused, bool) and paused > 0:
            text += f", {paused} paused"
        return text
    return type_


def describe_event(type_: str, task_id: str | None, payload: dict) -> str:
    """One plain-words line for an event. Exact texts in the `decision: describe_event
    texts` context item. Never raises."""
    try:
        return _describe(type_, task_id, payload if isinstance(payload, dict) else {})
    except Exception:
        return type_


def list_runs(repo: Path) -> list[str]:
    """Names of the directories under runs_root(repo) that contain a state.json file,
    newest first (reverse plain string order). [] if runs_root(repo) is not a directory."""
    root = runs_root(repo)
    if not root.is_dir():
        return []
    names = [p.name for p in root.iterdir() if p.is_dir() and (p / STATE_FILE).is_file()]
    return sorted(names, reverse=True)


def _read_events(path: Path) -> list[Event]:
    """Every readable event in the events.jsonl at `path`, in file order. [] if the file
    does not exist (FileNotFoundError); any other OSError propagates. The file is read as
    UTF-8 with errors="replace"; blank lines are skipped, and so is every line that
    Event.model_validate_json rejects (catch ValueError, which covers pydantic's
    ValidationError), e.g. a partial last line."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    events: list[Event] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(Event.model_validate_json(line))
        except ValueError:
            continue
    return events


def build_view(repo: Path, run_id: str, max_events: int = 50, now: float | None = None) -> RunView:
    """The RunView of run `run_id` in `repo`. Exact rules in the `decision: build_view
    rules` context item. Raises FileNotFoundError(f"No run state at {path}") (path = the
    state.json path, as a str) when it is not a file; StateError from load_state
    propagates. Never writes to disk."""
    directory = run_dir(repo, run_id)
    path = directory / STATE_FILE
    if not path.is_file():
        raise FileNotFoundError(f"No run state at {path}")
    state = load_state(directory)

    plan_file = Path(state.plan_path)
    if not plan_file.is_absolute():
        plan_file = repo / plan_file
    try:
        orders = {o.id: o for o in load_plan(plan_file).orders}
    except (OSError, ValueError):
        orders = {}

    events = _read_events(directory / EVENTS_FILE)
    activity: dict[str, str] = {}
    for e in events:
        if e.task_id is None:
            continue
        if e.type == "attempt_started":
            activity.pop(e.task_id, None)
        elif e.type == "worker_progress":
            activity[e.task_id] = _progress(e.payload)

    tasks: list[TaskView] = []
    for task_id, ts in state.tasks.items():
        order = orders.get(task_id)
        detail: str | None = None
        if ts.replan_note:
            detail = ts.replan_note
        elif ts.error:
            detail = ts.error
        elif ts.status == "failed" and ts.history and ts.history[-1].feedback:
            detail = ts.history[-1].feedback
        tasks.append(
            TaskView(
                task_id=task_id,
                title=order.title if order else task_id,
                status=ts.status,
                tier=ts.tier_used if ts.tier_used is not None else (order.tier if order else None),
                attempt=ts.attempts,
                max_attempts=order.budget.max_attempts if order else None,
                escalated=len({r.tier for r in ts.history}) > 1,
                cost_usd=ts.usage.cost_usd,
                input_tokens=ts.usage.input_tokens,
                output_tokens=ts.usage.output_tokens,
                detail=detail,
                activity=activity.get(task_id) if ts.status == "running" else None,
            )
        )

    k = None
    for i, e in enumerate(events):
        if e.type == "run_started":
            k = i
    wave = sum(1 for e in events[k or 0 :] if e.type == "wave_started")
    waves_total = 0
    if k is not None:
        waves = events[k].payload.get("waves")
        if isinstance(waves, list):
            waves_total = len(waves)

    stopped_reason: str | None = None
    for e in events[k or 0 :]:
        key = {"run_stopping": "reason", "run_finished": "stopped_reason"}.get(e.type)
        if key is None:
            continue
        value = e.payload.get(key)
        if isinstance(value, str) and value:
            stopped_reason = value
    if not state.finished:
        phase: Literal["running", "stopped", "finished"] = "running"
    elif stopped_reason is not None or any(t.status == "paused" for t in tasks):
        phase = "stopped"
    else:
        phase = "finished"

    idle_minutes: int | None = None
    if phase == "running":
        try:
            age = (time.time() if now is None else now) - (directory / EVENTS_FILE).stat().st_mtime
        except OSError:
            age = 0.0
        if age >= IDLE_MINUTES * 60:
            idle_minutes = int(age // 60)

    feed = [e for e in events if e.type != "worker_progress"]
    tail = feed[-max_events:] if max_events > 0 else []
    return RunView(
        run_id=state.run_id,
        stage=state.stage,
        finished=state.finished,
        phase=phase,
        stopped_reason=stopped_reason,
        idle_minutes=idle_minutes,
        wave=wave,
        waves_total=waves_total,
        cost_usd=sum(ts.usage.cost_usd for ts in state.tasks.values()),
        tiers=[
            TierView(tier=t, attempts=attempts, cost_usd=usage.cost_usd)
            for t, (attempts, _reviews, usage) in tier_totals(state).items()
        ],
        tasks=tasks,
        events=[
            EventView(
                ts=e.ts.isoformat(),
                task_id=e.task_id,
                type=e.type,
                text=describe_event(e.type, e.task_id, e.payload),
            )
            for e in tail
        ],
    )
