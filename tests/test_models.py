from typing import Any

import pytest
from pydantic import ValidationError

from harness.models import Event, Plan, TaskState, WorkOrder


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
