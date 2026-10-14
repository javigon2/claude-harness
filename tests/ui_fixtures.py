"""In-code fixture runs for the UI tests. Each helper writes real run files with the
harness's own models and writers. Test helpers only: call them from plain (sync) test
functions, because they use asyncio.run."""

import asyncio
from pathlib import Path
from typing import Any

from harness.config import MODEL_IDS, Tier
from harness.events import EVENTS_FILE, EventLog
from harness.models import AttemptRecord, Budget, Event, Plan, TaskState, Usage, WorkOrder
from harness.router import replan_note
from harness.state import RunState, run_dir, save_state

PLAN_PATH = "plans/plan.json"
STAGE = "UI fixture stage"
FAIL_FEEDBACK = "FAILED: uv run pytest -q (exit 1)"
RATE_LIMIT_RESETS_AT = 1792000800  # 2026-10-14T18:00:00 UTC
RATE_LIMIT_REASON = "rate limited; resets at 2026-10-14T18:00:00+00:00"
CAP_REASON = "estimated cost $1.20 passed the $1.00 cap"

_EventTuple = tuple[str, str | None, dict[str, Any]]


def _order(id_: str, title: str, tier: Tier, depends_on: list[str]) -> WorkOrder:
    return WorkOrder(
        id=id_,
        title=title,
        goal=f"Goal {id_}",
        tier=tier,
        depends_on=depends_on,
        files_owned=[f"{id_}.txt"],
        files_readonly=[],
        context=[],
        steps=[],
        constraints=[],
        acceptance=["true"],
        budget=Budget(max_turns=10, max_attempts=3),
        out_of_scope=[],
    )


def _plan() -> Plan:
    return Plan(
        stage=STAGE,
        orders=[
            _order("a", "Store", "haiku", []),
            _order("b", "CLI", "haiku", ["a"]),
            _order("c", "Docs", "sonnet", ["a", "b"]),
        ],
    )


def _record(n: int, tier: Tier, passed: bool, inp: int, out: int, cost: float) -> AttemptRecord:
    return AttemptRecord(
        attempt=n,
        tier=tier,
        worker_status="done",
        passed=passed,
        usage=Usage(input_tokens=inp, output_tokens=out, cost_usd=cost),
        feedback="" if passed else FAIL_FEEDBACK,
    )


def _task(
    task_id: str, status: Any, tier_used: Tier, history: list[AttemptRecord], **extra: Any
) -> TaskState:
    usage = Usage()
    for rec in history:
        usage = usage.plus(rec.usage)
    return TaskState(
        task_id=task_id,
        status=status,
        attempts=len(history),
        tier_used=tier_used,
        usage=usage,
        history=history,
        **extra,
    )


_A_DONE = [_record(1, "haiku", True, 100, 10, 0.01)]
_B_DONE = [_record(1, "haiku", False, 100, 10, 0.01), _record(2, "sonnet", True, 200, 20, 0.04)]
_C_DONE = [_record(1, "sonnet", True, 300, 30, 0.06)]
_A_BLOCKED = [
    _record(1, "haiku", False, 100, 10, 0.01),
    _record(2, "sonnet", False, 200, 20, 0.04),
    _record(3, "opus", False, 400, 40, 0.2),
]


def write_plan(repo: Path) -> Path:
    path = repo / PLAN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan().model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _run_started() -> _EventTuple:
    return (
        "run_started",
        None,
        {
            "plan_path": PLAN_PATH,
            "stage": STAGE,
            "orders": 3,
            "waves": [["a"], ["b"], ["c"]],
            "max_parallel": 4,
            "resume": False,
            "review": False,
        },
    )


def _wave_started(i: int, t: str) -> _EventTuple:
    return ("wave_started", None, {"wave": i, "tasks": [t]})


def _wave_finished(i: int, t: str, status: str) -> _EventTuple:
    return ("wave_finished", None, {"wave": i, "statuses": {t: status}})


def _task_started(repo: Path, run_id: str, t: str, tier: Tier) -> _EventTuple:
    return (
        "task_started",
        t,
        {
            "worktree": str(repo / ".harness" / "worktrees" / t),
            "branch": f"harness/{run_id}/{t}",
            "base": "0" * 40,
            "tier": tier,
        },
    )


def _attempt(t: str, rec: AttemptRecord, prev: Tier | None) -> list[_EventTuple]:
    n = rec.attempt
    events: list[_EventTuple] = []
    if prev is not None and prev != rec.tier:
        events.append(("escalated", t, {"attempt": n, "from_tier": prev, "to_tier": rec.tier}))
    events += [
        ("attempt_started", t, {"attempt": n, "tier": rec.tier}),
        (
            "session_init",
            t,
            {
                "role": "worker",
                "attempt": n,
                "tier": rec.tier,
                "model": MODEL_IDS[rec.tier],
                "tools": [],
                "skills": [],
                "mcp_servers": [],
                "permissionMode": "bypassPermissions",
                "apiKeySource": "none",
            },
        ),
        (
            "worker_finished",
            t,
            {
                "attempt": n,
                "tier": rec.tier,
                "status": "done",
                "summary": "",
                "error": None,
                "cost_usd": rec.usage.cost_usd,
                "input_tokens": rec.usage.input_tokens,
                "output_tokens": rec.usage.output_tokens,
                "num_turns": 5,
                "duration_ms": 1000,
            },
        ),
        ("verify_started", t, {"attempt": n}),
        (
            "verify_finished",
            t,
            {
                "attempt": n,
                "passed": rec.passed,
                "failed_commands": [] if rec.passed else ["uv run pytest -q"],
                "outside_files": [],
                "scope_error": None,
                "review_verdict": None,
                "review_reasons": [],
            },
        ),
    ]
    return events


def _attempts(t: str, history: list[AttemptRecord]) -> list[_EventTuple]:
    events: list[_EventTuple] = []
    prev: Tier | None = None
    for rec in history:
        events += _attempt(t, rec, prev)
        prev = rec.tier
    return events


def _done(run_id: str, t: str, task: TaskState) -> list[_EventTuple]:
    return [
        (
            "task_done",
            t,
            {"attempts": task.attempts, "tier": task.tier_used, "cost_usd": task.usage.cost_usd},
        ),
        ("merged", t, {"branch": f"harness/{run_id}/{t}", "committed": True}),
    ]


async def _emit_all(log: EventLog, events: list[_EventTuple]) -> None:
    for type_, task_id, payload in events:
        await log.emit(type_, task_id=task_id, payload=payload)


def _write(
    repo: Path,
    run_id: str,
    finished: bool,
    tasks: dict[str, TaskState],
    events: list[_EventTuple],
) -> str:
    write_plan(repo)
    directory = run_dir(repo, run_id)
    save_state(
        directory,
        RunState(run_id=run_id, plan_path=PLAN_PATH, stage=STAGE, finished=finished, tasks=tasks),
    )
    asyncio.run(_emit_all(EventLog(directory, run_id), events))
    return run_id


def _done_block(
    repo: Path, run_id: str, i: int, t: str, tier: Tier, task: TaskState
) -> list[_EventTuple]:
    return [
        _wave_started(i, t),
        _task_started(repo, run_id, t, tier),
        *_attempts(t, task.history),
        *_done(run_id, t, task),
        _wave_finished(i, t, "done"),
    ]


def write_happy_run(repo: Path, run_id: str = "happy1") -> str:
    tasks = {
        "a": _task("a", "done", "haiku", _A_DONE),
        "b": _task("b", "done", "sonnet", _B_DONE),
        "c": _task("c", "done", "sonnet", _C_DONE),
    }
    events = [_run_started()]
    for i, t, tier in ((1, "a", "haiku"), (2, "b", "haiku"), (3, "c", "sonnet")):
        events += _done_block(repo, run_id, i, t, tier, tasks[t])
    cost = sum(task.usage.cost_usd for task in tasks.values())
    events.append(("run_finished", None, {"done": 3, "failed": 0, "blocked": 0, "cost_usd": cost}))
    return _write(repo, run_id, True, tasks, events)


def write_blocked_run(repo: Path, run_id: str = "blocked1") -> str:
    note = replan_note(_plan().orders[0], _A_BLOCKED)
    a = _task("a", "blocked", "opus", _A_BLOCKED, replan_note=note)
    tasks = {
        "a": a,
        "b": TaskState(task_id="b", status="blocked", error="dependency a is blocked"),
        "c": TaskState(task_id="c", status="blocked", error="dependency b is blocked"),
    }
    events: list[_EventTuple] = [
        _run_started(),
        _wave_started(1, "a"),
        _task_started(repo, run_id, "a", "haiku"),
        *_attempts("a", _A_BLOCKED),
        ("task_blocked", "a", {"attempts": 3, "tier": "opus", "replan_note": note}),
        _wave_finished(1, "a", "blocked"),
        _wave_started(2, "b"),
        ("task_blocked", "b", {"reason": "dependency a is blocked", "dependency": "a"}),
        _wave_finished(2, "b", "blocked"),
        _wave_started(3, "c"),
        ("task_blocked", "c", {"reason": "dependency b is blocked", "dependency": "b"}),
        _wave_finished(3, "c", "blocked"),
        (
            "run_finished",
            None,
            {"done": 0, "failed": 0, "blocked": 3, "cost_usd": a.usage.cost_usd},
        ),
    ]
    return _write(repo, run_id, True, tasks, events)


def write_running_run(repo: Path, run_id: str = "running1") -> str:
    a = _task("a", "done", "haiku", _A_DONE)
    tasks = {
        "a": a,
        "b": TaskState(task_id="b", status="running", attempts=1, tier_used="haiku"),
        "c": TaskState(task_id="c"),
    }
    events: list[_EventTuple] = [
        _run_started(),
        *_done_block(repo, run_id, 1, "a", "haiku", a),
        _wave_started(2, "b"),
        _task_started(repo, run_id, "b", "haiku"),
        ("attempt_started", "b", {"attempt": 1, "tier": "haiku"}),
    ]
    return _write(repo, run_id, False, tasks, events)


def write_paused_run(repo: Path, run_id: str = "paused1") -> str:
    """Write a finished run stopped by a rate limit: a done, b paused (with three
    worker_progress events before the pause), c pending. Returns run_id. 21 events."""
    a = _task("a", "done", "haiku", _A_DONE)
    tasks = {
        "a": a,
        "b": TaskState(
            task_id="b",
            status="paused",
            attempts=0,
            tier_used="haiku",
            usage=Usage(input_tokens=50, output_tokens=5, cost_usd=0.005),
            error=RATE_LIMIT_REASON,
        ),
        "c": TaskState(task_id="c"),
    }
    progress = [
        (
            "worker_progress",
            "b",
            {"attempt": 1, "tier": "haiku", "tool": tool, "detail": detail},
        )
        for tool, detail in (
            ("Read", "src/b.py"),
            ("Edit", "src/b.py"),
            ("Bash", "uv run pytest -q"),
        )
    ]
    events: list[_EventTuple] = [
        _run_started(),
        *_done_block(repo, run_id, 1, "a", "haiku", a),
        _wave_started(2, "b"),
        _task_started(repo, run_id, "b", "haiku"),
        ("attempt_started", "b", {"attempt": 1, "tier": "haiku"}),
        *progress,
        (
            "task_paused",
            "b",
            {
                "attempt": 1,
                "tier": "haiku",
                "reason": RATE_LIMIT_REASON,
                "resets_at": RATE_LIMIT_RESETS_AT,
            },
        ),
        _wave_finished(2, "b", "paused"),
        ("run_stopping", None, {"reason": RATE_LIMIT_REASON}),
        (
            "run_finished",
            None,
            {
                "done": 1,
                "failed": 0,
                "blocked": 0,
                "paused": 1,
                "stopped_reason": RATE_LIMIT_REASON,
                "cost_usd": 0.015,
            },
        ),
    ]
    return _write(repo, run_id, True, tasks, events)


def write_capped_run(repo: Path, run_id: str = "capped1") -> str:
    """Write a finished run stopped by the --max-cost cap after a: a done ($1.20), b and c
    pending. Returns run_id. 14 events."""
    a = _task("a", "done", "haiku", [_record(1, "haiku", True, 1000, 100, 1.2)])
    tasks = {"a": a, "b": TaskState(task_id="b"), "c": TaskState(task_id="c")}
    done, merged = _done(run_id, "a", a)
    events: list[_EventTuple] = [
        _run_started(),
        _wave_started(1, "a"),
        _task_started(repo, run_id, "a", "haiku"),
        *_attempts("a", a.history),
        done,
        ("budget_exceeded", None, {"cost_usd": 1.2, "max_cost_usd": 1.0}),
        merged,
        _wave_finished(1, "a", "done"),
        ("run_stopping", None, {"reason": CAP_REASON}),
        (
            "run_finished",
            None,
            {
                "done": 1,
                "failed": 0,
                "blocked": 0,
                "paused": 0,
                "stopped_reason": CAP_REASON,
                "cost_usd": 1.2,
            },
        ),
    ]
    return _write(repo, run_id, True, tasks, events)


def append_event(
    repo: Path, run_id: str, type_: str, task_id: str | None = None, payload: dict | None = None
) -> None:
    directory = run_dir(repo, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    event = Event(run_id=run_id, task_id=task_id, type=type_, payload=payload or {})
    with (directory / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")
