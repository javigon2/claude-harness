from typing import Any

import pytest
from pydantic import ValidationError

from harness.models import AttemptRecord, Event, Plan, TaskState, Usage, WorkOrder


def _order(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "t1",
        "title": "Title",
        "goal": "Goal",
        "tier": "haiku",
        "depends_on": [],
        "files_owned": ["a.py"],
        "files_readonly": [],
        "context": [{"label": "l", "content": "c"}],
        "steps": [],
        "constraints": [],
        "acceptance": ["true"],
        "budget": {"max_turns": 5, "max_attempts": 2},
        "out_of_scope": [],
    }
    data.update(overrides)
    return data


def test_valid_dict_parses():
    order = WorkOrder.model_validate(_order())
    assert order.id == "t1"
    assert order.tier == "haiku"


def test_missing_goal_raises():
    data = _order()
    del data["goal"]
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(data)


def test_unknown_field_raises():
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(_order(priority=1))


def test_id_is_lowercased():
    assert WorkOrder.model_validate(_order(id="Cli-Done")).id == "cli-done"


def test_id_with_space_raises():
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(_order(id="cli done"))


def test_empty_acceptance_raises():
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(_order(acceptance=[]))


def test_empty_files_owned_raises():
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(_order(files_owned=[]))


def test_zero_max_turns_raises():
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(_order(budget={"max_turns": 0, "max_attempts": 1}))


def test_context_item_with_extra_key_raises():
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(_order(context=[{"label": "l", "content": "c", "x": 1}]))


def test_plan_parses_two_orders():
    plan = Plan.model_validate({"stage": "s", "orders": [_order(id="a"), _order(id="b")]})
    assert plan.stage == "s"
    assert [o.id for o in plan.orders] == ["a", "b"]


def test_task_state_defaults():
    state = TaskState(task_id="t1")
    assert state.status == "pending"
    assert state.attempts == 0
    assert state.tier_used is None
    assert state.usage.input_tokens == 0
    assert state.usage.output_tokens == 0
    assert state.usage.cache_read_tokens == 0
    assert state.usage.cache_creation_tokens == 0
    assert state.usage.cost_usd == 0.0


def test_event_json_round_trip_and_aware_ts():
    event = Event(run_id="r1", task_id="t1", type="task_started", payload={"tier": "haiku"})
    assert Event.model_validate_json(event.model_dump_json()) == event
    assert event.ts.tzinfo is not None
    assert event.ts.utcoffset() is not None


def test_usage_plus_sums_every_field_and_leaves_inputs_unchanged():
    a = Usage(
        input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_creation_tokens=4, cost_usd=0.5
    )
    b = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_creation_tokens=40,
        cost_usd=0.25,
    )
    total = a.plus(b)
    assert total == Usage(
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_creation_tokens=44,
        cost_usd=0.75,
    )
    assert a.input_tokens == 1 and a.cost_usd == 0.5
    assert b.input_tokens == 10 and b.cost_usd == 0.25


def test_attempt_record_rejects_attempt_zero():
    with pytest.raises(ValidationError):
        AttemptRecord(attempt=0, tier="haiku", worker_status="done", passed=True)


def test_attempt_record_rejects_unknown_worker_status():
    with pytest.raises(ValidationError):
        AttemptRecord(attempt=1, tier="haiku", worker_status="weird", passed=True)


def test_task_state_new_field_defaults():
    state = TaskState(task_id="t1")
    assert state.history == []
    assert state.replan_note is None
    assert state.error is None


def test_task_state_with_history_round_trips():
    record = AttemptRecord(
        attempt=1,
        tier="sonnet",
        worker_status="failed",
        passed=False,
        usage=Usage(input_tokens=5, cost_usd=0.1),
        summary="s",
        feedback="fb",
    )
    state = TaskState(task_id="t1", status="failed", attempts=1, history=[record], error="e")
    assert TaskState.model_validate_json(state.model_dump_json()) == state
