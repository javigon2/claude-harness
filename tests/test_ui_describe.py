import pytest
from pydantic import ValidationError

from harness.config import MODEL_IDS
from harness.ui.view import EventView, RunView, TaskView, TierView, describe_event

CASES = [
    (
        "run_started",
        None,
        {"orders": 3, "waves": [["a"], ["b"], ["c"]]},
        "run started: 3 orders in 3 waves",
    ),
    ("run_started", None, {}, "run started: ? orders in ? waves"),
    (
        "wave_started",
        None,
        {"wave": 2, "tasks": ["b", "d"]},
        "wave 2 started: b, d",
    ),
    ("wave_started", None, {"wave": 2}, "wave 2 started: ?"),
    ("wave_finished", None, {"wave": 1, "statuses": {"a": "done"}}, "wave 1 finished"),
    ("task_started", "a", {"tier": "haiku"}, "a started on haiku"),
    (
        "task_skipped",
        "a",
        {"reason": "done in an earlier run"},
        "a skipped: done in an earlier run",
    ),
    (
        "attempt_started",
        "b",
        {"attempt": 2, "tier": "sonnet"},
        "b attempt 2 on sonnet",
    ),
    (
        "escalated",
        "b",
        {"attempt": 2, "from_tier": "haiku", "to_tier": "sonnet"},
        "b escalated haiku -> sonnet",
    ),
    (
        "session_init",
        "a",
        {"role": "worker", "model": MODEL_IDS["haiku"]},
        f"a worker session: {MODEL_IDS['haiku']}",
    ),
    ("worker_finished", "a", {"status": "done"}, "a worker finished: done"),
    ("verify_started", "a", {"attempt": 1}, "a verifying attempt 1"),
    ("verify_finished", "a", {"passed": True}, "a verification passed"),
    ("verify_finished", "a", {"passed": False}, "a verification failed"),
    ("verify_finished", "a", {}, "a verification failed"),
    (
        "task_done",
        "a",
        {"attempts": 1, "tier": "haiku", "cost_usd": 0.01},
        "a done on haiku after 1 attempts",
    ),
    (
        "task_failed",
        "a",
        {"attempts": 2, "tier": "sonnet", "reason": "budget exhausted"},
        "a failed: budget exhausted",
    ),
    (
        "task_blocked",
        "b",
        {"reason": "dependency a is blocked", "dependency": "a"},
        "b blocked: dependency a is blocked",
    ),
    (
        "task_blocked",
        "a",
        {"attempts": 3, "tier": "opus", "replan_note": "x"},
        "a blocked after 3 attempts on opus",
    ),
    (
        "merge_conflict",
        "a",
        {"branch": "x", "worktree": "y"},
        "a merge conflict",
    ),
    ("merged", "a", {"branch": "x", "committed": True}, "a merged into main"),
    (
        "run_finished",
        None,
        {"done": 3, "failed": 0, "blocked": 0, "cost_usd": 0.1},
        "run finished: 3 done, 0 failed, 0 blocked",
    ),
    ("worker_progress", "b", {"tool": "Edit", "detail": "src/b.py"}, "b Edit: src/b.py"),
    ("worker_progress", "b", {"tool": "Bash", "detail": ""}, "b Bash"),
    ("worker_progress", "b", {"tool": "Bash"}, "b Bash"),
    ("worker_progress", "b", {"detail": "x"}, "b ?: x"),
    ("worker_progress", None, {}, "? ?"),
    (
        "task_paused",
        "b",
        {"reason": "rate limited; resets at unknown"},
        "b paused: rate limited; resets at unknown",
    ),
    ("task_paused", None, {}, "? paused: ?"),
    ("run_stopping", None, {"reason": "cap"}, "run stopping: cap"),
    ("run_stopping", None, {}, "run stopping: ?"),
    (
        "budget_exceeded",
        None,
        {"cost_usd": 1.2, "max_cost_usd": 1},
        "cost cap reached: $1.2000 of $1.0000",
    ),
    ("budget_exceeded", None, {"cost_usd": True}, "cost cap reached: ? of ?"),
    ("budget_exceeded", None, {"cost_usd": "1", "max_cost_usd": None}, "cost cap reached: ? of ?"),
    (
        "run_finished",
        None,
        {"done": 1, "failed": 0, "blocked": 0, "paused": 1},
        "run finished: 1 done, 0 failed, 0 blocked, 1 paused",
    ),
    (
        "run_finished",
        None,
        {"done": 1, "failed": 0, "blocked": 0, "paused": 0},
        "run finished: 1 done, 0 failed, 0 blocked",
    ),
    (
        "run_finished",
        None,
        {"done": 1, "failed": 0, "blocked": 0, "paused": True},
        "run finished: 1 done, 0 failed, 0 blocked",
    ),
    ("task_done", None, {}, "? done on ? after ? attempts"),
    ("task_failed", "a", {"reason": None}, "a failed: ?"),
    ("merged", "a", None, "a merged into main"),
]


@pytest.mark.parametrize(("type_", "task_id", "payload", "expected"), CASES)
def test_describe_event_texts(
    type_: str, task_id: str | None, payload: dict, expected: str
) -> None:
    assert describe_event(type_, task_id, payload) == expected


def test_describe_event_never_raises_on_odd_payloads() -> None:
    for payload in (None, [], "text", 42, {"waves": 5, "tasks": None}):
        assert isinstance(describe_event("run_started", None, payload), str)
        assert isinstance(describe_event("wave_started", "a", payload), str)


def test_models_forbid_unknown_fields() -> None:
    valid_kwargs = {
        EventView: dict(ts="t", task_id=None, type="x", text="y"),
        TierView: dict(tier="haiku", attempts=1, cost_usd=0.0),
        TaskView: dict(
            task_id="a",
            title="t",
            status="done",
            tier="haiku",
            attempt=1,
            max_attempts=None,
            escalated=False,
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            detail=None,
            activity=None,
        ),
        RunView: dict(
            run_id="r",
            stage="s",
            finished=False,
            phase="finished",
            stopped_reason=None,
            wave=0,
            waves_total=0,
            cost_usd=0.0,
            tiers=[],
            tasks=[],
            events=[],
        ),
    }
    for model, kwargs in valid_kwargs.items():
        model(**kwargs)
        with pytest.raises(ValidationError):
            model(**kwargs, bogus=1)
