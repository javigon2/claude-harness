from datetime import UTC, datetime
from pathlib import Path

import pytest
from ui_fixtures import (
    CAP_REASON,
    PLAN_PATH,
    RATE_LIMIT_REASON,
    RATE_LIMIT_RESETS_AT,
    STAGE,
    append_event,
    write_blocked_run,
    write_capped_run,
    write_happy_run,
    write_paused_run,
    write_plan,
    write_running_run,
)

from harness.config import MODEL_IDS
from harness.events import EVENTS_FILE, read_events
from harness.plan import compute_waves, load_plan
from harness.state import STATE_FILE, load_state, run_dir

HAPPY_TYPES = (
    "run_started wave_started task_started attempt_started session_init worker_finished "
    "verify_started verify_finished task_done merged wave_finished wave_started "
    "task_started attempt_started session_init worker_finished verify_started "
    "verify_finished escalated attempt_started session_init worker_finished "
    "verify_started verify_finished task_done merged wave_finished wave_started "
    "task_started attempt_started session_init worker_finished verify_started "
    "verify_finished task_done merged wave_finished run_finished "
).split()
BLOCKED_TYPES = (
    "run_started wave_started task_started attempt_started session_init worker_finished "
    "verify_started verify_finished escalated attempt_started session_init "
    "worker_finished verify_started verify_finished escalated attempt_started "
    "session_init worker_finished verify_started verify_finished task_blocked "
    "wave_finished wave_started task_blocked wave_finished wave_started task_blocked "
    "wave_finished run_finished "
).split()
RUNNING_TYPES = (
    "run_started wave_started task_started attempt_started session_init worker_finished "
    "verify_started verify_finished task_done merged wave_finished wave_started "
    "task_started attempt_started "
).split()

WRITERS = {
    "happy": (write_happy_run, "happy1", HAPPY_TYPES, True),
    "blocked": (write_blocked_run, "blocked1", BLOCKED_TYPES, True),
    "running": (write_running_run, "running1", RUNNING_TYPES, False),
}

# (status, attempts, tier_used, error) per task
EXPECTED = {
    "happy": {
        "a": ("done", 1, "haiku", None),
        "b": ("done", 2, "sonnet", None),
        "c": ("done", 1, "sonnet", None),
    },
    "blocked": {
        "a": ("blocked", 3, "opus", None),
        "b": ("blocked", 0, None, "dependency a is blocked"),
        "c": ("blocked", 0, None, "dependency b is blocked"),
    },
    "running": {
        "a": ("done", 1, "haiku", None),
        "b": ("running", 1, "haiku", None),
        "c": ("pending", 0, None, None),
    },
}


def test_write_plan(tmp_path: Path) -> None:
    path = write_plan(tmp_path)
    assert path == tmp_path / "plans" / "plan.json"
    plan = load_plan(path)
    assert plan.stage == STAGE
    assert [o.id for o in plan.orders] == ["a", "b", "c"]
    assert [o.title for o in plan.orders] == ["Store", "CLI", "Docs"]
    assert [o.tier for o in plan.orders] == ["haiku", "haiku", "sonnet"]
    assert [o.depends_on for o in plan.orders] == [[], ["a"], ["a", "b"]]
    assert all(o.budget.max_attempts == 3 and o.budget.max_turns == 10 for o in plan.orders)
    assert compute_waves(plan) == [["a"], ["b"], ["c"]]
    write_plan(tmp_path)


@pytest.mark.parametrize("kind", ["happy", "blocked", "running"])
def test_run_state_and_events(tmp_path: Path, kind: str) -> None:
    writer, default_id, types, finished = WRITERS[kind]
    assert writer(tmp_path) == default_id
    assert writer(tmp_path, "custom") == "custom"
    for run_id in (default_id, "custom"):
        directory = run_dir(tmp_path, run_id)
        state = load_state(directory)
        assert state.run_id == run_id
        assert state.plan_path == PLAN_PATH
        assert state.stage == STAGE
        assert state.finished is finished
        assert list(state.tasks) == ["a", "b", "c"]
        for tid, (status, attempts, tier, error) in EXPECTED[kind].items():
            task = state.tasks[tid]
            assert (task.status, task.attempts, task.tier_used, task.error) == (
                status,
                attempts,
                tier,
                error,
            )
        events = read_events(directory / EVENTS_FILE)
        assert [e.type for e in events] == types
        assert all(e.run_id == run_id for e in events)


def test_happy_details(tmp_path: Path) -> None:
    write_happy_run(tmp_path)
    state = load_state(run_dir(tmp_path, "happy1"))
    usage = state.tasks["b"].usage
    assert (usage.input_tokens, usage.output_tokens) == (300, 30)
    assert usage.cost_usd == pytest.approx(0.05)
    events = read_events(run_dir(tmp_path, "happy1") / EVENTS_FILE)
    escalated = [e for e in events if e.type == "escalated"]
    assert len(escalated) == 1
    assert escalated[0].task_id == "b"
    assert escalated[0].payload["from_tier"] == "haiku"
    assert escalated[0].payload["to_tier"] == "sonnet"
    for e in events:
        if e.type == "session_init":
            assert e.payload["apiKeySource"] == "none"
            assert e.payload["model"] == MODEL_IDS[e.payload["tier"]]


def test_blocked_details(tmp_path: Path) -> None:
    write_blocked_run(tmp_path)
    directory = run_dir(tmp_path, "blocked1")
    note = load_state(directory).tasks["a"].replan_note
    assert note is not None
    assert note.startswith("Task a is blocked after 3 attempts")
    blocked = {
        e.task_id: e.payload
        for e in read_events(directory / EVENTS_FILE)
        if e.type == "task_blocked"
    }
    assert blocked["a"] == {"attempts": 3, "tier": "opus", "replan_note": note}
    assert blocked["b"] == {"reason": "dependency a is blocked", "dependency": "a"}
    assert blocked["c"] == {"reason": "dependency b is blocked", "dependency": "b"}


def test_running_details(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    directory = run_dir(tmp_path, "running1")
    events = read_events(directory / EVENTS_FILE)
    assert "run_finished" not in [e.type for e in events]
    assert load_state(directory).tasks["c"].status == "pending"


def test_append_event(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    directory = run_dir(tmp_path, "running1")
    state_bytes = (directory / STATE_FILE).read_bytes()
    before = len((directory / EVENTS_FILE).read_text(encoding="utf-8").splitlines())

    append_event(tmp_path, "running1", "worker_progress", "b", {"x": 1})
    append_event(tmp_path, "running1", "note")

    events = read_events(directory / EVENTS_FILE)
    assert len(events) == before + 2
    assert (events[-2].type, events[-2].task_id, events[-2].payload) == (
        "worker_progress",
        "b",
        {"x": 1},
    )
    assert (events[-1].type, events[-1].task_id, events[-1].payload) == ("note", None, {})
    assert (directory / STATE_FILE).read_bytes() == state_bytes


def test_append_event_creates_log(tmp_path: Path) -> None:
    append_event(tmp_path, "fresh", "note")
    path = run_dir(tmp_path, "fresh") / EVENTS_FILE
    assert [e.type for e in read_events(path)] == ["note"]


PAUSED_TYPES = (
    "run_started wave_started task_started attempt_started session_init worker_finished "
    "verify_started verify_finished task_done merged wave_finished wave_started "
    "task_started attempt_started worker_progress worker_progress worker_progress "
    "task_paused wave_finished run_stopping run_finished"
).split()
CAPPED_TYPES = (
    "run_started wave_started task_started attempt_started session_init worker_finished "
    "verify_started verify_finished task_done budget_exceeded merged wave_finished "
    "run_stopping run_finished"
).split()


def test_paused_run(tmp_path: Path) -> None:
    assert write_paused_run(tmp_path) == "paused1"
    assert write_paused_run(tmp_path, "p2") == "p2"
    assert run_dir(tmp_path, "p2").is_dir()
    directory = run_dir(tmp_path, "paused1")
    state = load_state(directory)
    assert state.finished is True
    a, b, c = (state.tasks[t] for t in "abc")
    assert (a.status, a.attempts, a.tier_used, a.error) == ("done", 1, "haiku", None)
    assert (b.status, b.attempts, b.tier_used, b.error) == (
        "paused",
        0,
        "haiku",
        RATE_LIMIT_REASON,
    )
    assert (c.status, c.attempts, c.tier_used) == ("pending", 0, None)
    events = read_events(directory / EVENTS_FILE)
    assert [e.type for e in events] == PAUSED_TYPES
    assert len(events) == 21
    progress = [e.payload for e in events if e.type == "worker_progress"]
    assert progress == [
        {"attempt": 1, "tier": "haiku", "tool": "Read", "detail": "src/b.py"},
        {"attempt": 1, "tier": "haiku", "tool": "Edit", "detail": "src/b.py"},
        {"attempt": 1, "tier": "haiku", "tool": "Bash", "detail": "uv run pytest -q"},
    ]
    paused = next(e for e in events if e.type == "task_paused")
    assert paused.task_id == "b"
    assert paused.payload == {
        "attempt": 1,
        "tier": "haiku",
        "reason": RATE_LIMIT_REASON,
        "resets_at": RATE_LIMIT_RESETS_AT,
    }
    stopping = next(e for e in events if e.type == "run_stopping")
    assert stopping.payload == {"reason": RATE_LIMIT_REASON}
    assert events[-1].payload == {
        "done": 1,
        "failed": 0,
        "blocked": 0,
        "paused": 1,
        "stopped_reason": RATE_LIMIT_REASON,
        "cost_usd": 0.015,
    }


def test_rate_limit_constants() -> None:
    iso = datetime.fromtimestamp(RATE_LIMIT_RESETS_AT, UTC).isoformat()
    assert RATE_LIMIT_REASON == f"rate limited; resets at {iso}"


def test_capped_run(tmp_path: Path) -> None:
    assert write_capped_run(tmp_path) == "capped1"
    assert write_capped_run(tmp_path, "c2") == "c2"
    assert run_dir(tmp_path, "c2").is_dir()
    directory = run_dir(tmp_path, "capped1")
    state = load_state(directory)
    assert state.finished is True
    a, b, c = (state.tasks[t] for t in "abc")
    assert (a.status, a.attempts, a.tier_used, a.error) == ("done", 1, "haiku", None)
    assert a.usage.cost_usd == pytest.approx(1.2)
    assert (b.status, b.attempts) == ("pending", 0)
    assert (c.status, c.attempts) == ("pending", 0)
    events = read_events(directory / EVENTS_FILE)
    assert [e.type for e in events] == CAPPED_TYPES
    assert len(events) == 14
    exceeded = next(e for e in events if e.type == "budget_exceeded")
    assert exceeded.task_id is None
    assert exceeded.payload == {"cost_usd": 1.2, "max_cost_usd": 1.0}
    stopping = next(e for e in events if e.type == "run_stopping")
    assert stopping.payload == {"reason": CAP_REASON}
    assert events[-1].payload == {
        "done": 1,
        "failed": 0,
        "blocked": 0,
        "paused": 0,
        "stopped_reason": CAP_REASON,
        "cost_usd": 1.2,
    }
