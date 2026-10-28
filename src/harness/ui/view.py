"""The run viewer's view model: what the page shows, built from a run's files. Read-only."""

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.config import Tier
from harness.events import EVENTS_FILE
from harness.models import Event, TaskStatus
from harness.plan import load_plan
from harness.report import money, tier_totals
from harness.state import STATE_FILE, load_state, run_dir, runs_root

Phase = Literal["running", "stopped", "ended"]
Outcome = Literal["completed", "needs_attention"]

# statuses named in a "needs attention" text, in this order
_ATTENTION_ORDER: tuple[TaskStatus, ...] = (
    "failed",
    "blocked",
    "paused",
    "pending",
    "running",
    "verifying",
)  # fmt: skip


def outcome_of(statuses: Sequence[TaskStatus]) -> tuple[Outcome, str]:
    """("completed", "completed") when every status is "done" (also for no tasks); else
    ("needs_attention", "needs attention: " + the non-zero counts as "<n> <status>" in
    _ATTENTION_ORDER, joined ", "), e.g. "needs attention: 1 failed, 2 blocked"."""
    if all(s == "done" for s in statuses):
        return "completed", "completed"
    counts = [f"{statuses.count(s)} {s}" for s in _ATTENTION_ORDER if statuses.count(s)]
    return "needs_attention", "needs attention: " + ", ".join(counts)


class StepView(BaseModel):
    """One tool call of a worker, in plain words."""

    model_config = ConfigDict(extra="forbid")

    ts: str
    attempt: int | None
    tier: str | None
    tool: str
    detail: str
    text: str


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
    steps: list[StepView] = Field(default_factory=list)


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
    phase: Phase
    outcome: Outcome | None = None
    outcome_text: str = ""
    stopped_reason: str | None
    idle_minutes: int | None
    wave: int
    waves_total: int
    cost_usd: float
    tiers: list[TierView]
    tasks: list[TaskView]
    events: list[EventView]


IDLE_MINUTES = 10  # a running run whose events.jsonl is this stale is probably dead
MAX_STEPS = 200  # newest worker_progress steps kept per task

# (first word, second word or "" for any) -> plain words; first match wins
_COMMAND_WORDS: tuple[tuple[str, str, str], ...] = (
    ("pytest", "", "Running the tests"),
    ("ruff", "format", "Formatting the code"),
    ("ruff", "", "Checking the code style"),
    ("mypy", "", "Checking the types"),
    ("pyright", "", "Checking the types"),
    ("git", "status", "Checking which files changed"),
    ("git", "diff", "Looking at the changes"),
    ("git", "log", "Reading the commit history"),
    ("git", "show", "Looking at a commit"),
    ("git", "add", "Staging changes"),
    ("git", "commit", "Committing changes"),
    ("git", "", "Running git"),
    ("uv", "sync", "Installing dependencies"),
    ("uv", "add", "Adding a dependency"),
    ("pip", "install", "Installing dependencies"),
    ("npm", "install", "Installing dependencies"),
    ("npm", "test", "Running the tests"),
    ("ls", "", "Listing files"),
    ("find", "", "Finding files"),
    ("tree", "", "Listing files"),
    ("cat", "", "Reading a file"),
    ("head", "", "Reading a file"),
    ("tail", "", "Reading a file"),
    ("sed", "", "Reading a file"),
    ("wc", "", "Counting lines"),
    ("grep", "", "Searching the code"),
    ("rg", "", "Searching the code"),
    ("mkdir", "", "Creating a folder"),
    ("rm", "", "Deleting files"),
    ("mv", "", "Moving files"),
    ("cp", "", "Copying files"),
    ("python", "", "Running a Python script"),
    ("python3", "", "Running a Python script"),
)

# leading words that only say how a command is started
_COMMAND_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("python", "-m"),
    ("python3", "-m"),
    ("uvx",),
    ("npx",),
)


def command_summary(command: str) -> str:
    """Plain words for one shell command line, e.g. "Running the tests" for
    "uv run pytest -q". Exact rules in the `decision: command_summary` context item.
    Never raises."""
    words = command.split()
    changed = True
    while changed:
        changed = False
        for prefix in _COMMAND_PREFIXES:
            if len(words) > len(prefix) and tuple(words[: len(prefix)]) == prefix:
                words = words[len(prefix) :]
                changed = True
    if not words:
        return "Running a command"
    first = words[0].rsplit("/", 1)[-1]
    second = words[1] if len(words) > 1 else ""
    for name, sub, text in _COMMAND_WORDS:
        if first == name and sub in ("", second):
            return text
    return "Running a command"


# tool -> (prefix used when there is a detail, text used when there is none)
_STEP_WORDS: dict[str, tuple[str, str]] = {
    "Read": ("Reading", "Reading a file"),
    "Edit": ("Editing", "Editing a file"),
    "Write": ("Writing", "Writing a file"),
    "Grep": ("Searching for", "Searching the code"),
    "Glob": ("Finding files matching", "Finding files"),
}


def step_text(tool: str, detail: str, summary: str = "") -> str:
    """Plain words for one tool call, e.g. "Reading README.md" or "Running the tests".
    A non-empty `summary` (the agent's own words for the call) is returned as is; a Bash
    call without one gets command_summary(detail). Exact texts in the `decision:
    step_text` context item. Never raises."""
    if summary:
        return summary
    if tool == "Bash":
        return command_summary(detail)
    if tool not in _STEP_WORDS:
        return f"Using {tool}: {detail}" if detail else f"Using {tool}"
    prefix, bare = _STEP_WORDS[tool]
    return f"{prefix} {detail}" if detail else bare


def _step(e: Event) -> StepView:
    """The StepView of one worker_progress event. Wrong-typed payload keys become None,
    "?" or "" rather than raising."""
    p = e.payload if isinstance(e.payload, dict) else {}
    attempt = p.get("attempt")
    tier = p.get("tier")
    tool = p.get("tool")
    detail = p.get("detail")
    summary = p.get("summary")
    attempt = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None
    tier = tier if isinstance(tier, str) else None
    tool = tool if isinstance(tool, str) else "?"
    detail = detail if isinstance(detail, str) else ""
    summary = summary if isinstance(summary, str) else ""
    return StepView(
        ts=e.ts.isoformat(),
        attempt=attempt,
        tier=tier,
        tool=tool,
        detail=detail,
        text=step_text(tool, detail, summary),
    )


def _val(payload: dict, key: str) -> str:
    value = payload.get(key)
    return "?" if value is None else str(value)


def _listed(payload: dict, key: str) -> str:
    value = payload.get(key)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return "?"


def progress_text(payload: dict) -> str:
    """step_text of one progress payload: its "tool", "detail" and "summary" keys, each
    replaced by "?", "" and "" when missing or not a str. Never raises."""
    tool = payload.get("tool")
    detail = payload.get("detail")
    summary = payload.get("summary")
    return step_text(
        tool if isinstance(tool, str) else "?",
        detail if isinstance(detail, str) else "",
        summary if isinstance(summary, str) else "",
    )


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
        return f"{t} {progress_text(payload)}"
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


def build_view(
    repo: Path,
    run_id: str,
    max_events: int = 50,
    now: float | None = None,
    max_steps: int = MAX_STEPS,
) -> RunView:
    """The RunView of run `run_id` in `repo`. Exact rules in the `decision: build_view
    rules` context item. Each task's steps are its newest `max_steps` worker tool calls
    (none when max_steps is 0). Raises FileNotFoundError(f"No run state at {path}") (path =
    the state.json path, as a str) when it is not a file; StateError from load_state
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
    steps: dict[str, list[StepView]] = {}
    for e in events:
        if e.task_id is None:
            continue
        if e.type == "attempt_started":
            activity.pop(e.task_id, None)
        elif e.type == "worker_progress":
            activity[e.task_id] = progress_text(e.payload)
            steps.setdefault(e.task_id, []).append(_step(e))

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
                steps=steps.get(task_id, [])[-max_steps:] if max_steps > 0 else [],
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
        phase: Phase = "running"
    elif stopped_reason is not None or any(t.status == "paused" for t in tasks):
        phase = "stopped"
    else:
        phase = "ended"
    outcome: Outcome | None = None
    outcome_text = ""
    if phase != "running":
        outcome, outcome_text = outcome_of([t.status for t in tasks])

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
        outcome=outcome,
        outcome_text=outcome_text,
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
