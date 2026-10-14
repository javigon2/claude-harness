import json
from pathlib import Path

import pytest

from harness.models import Plan
from harness.plan import PlanError, compute_waves, load_plan, validate_plan


def _order(id: str, deps: list[str] | None = None, owned: list[str] | None = None) -> dict:
    return {
        "id": id,
        "title": id,
        "goal": "g",
        "tier": "haiku",
        "depends_on": deps or [],
        "files_owned": owned if owned is not None else [f"{id}.py"],
        "files_readonly": [],
        "context": [],
        "steps": [],
        "constraints": [],
        "acceptance": ["true"],
        "budget": {"max_turns": 5, "max_attempts": 2},
        "out_of_scope": [],
    }


def _plan(*orders: dict) -> Plan:
    return Plan(stage="s", orders=list(orders))  # type: ignore[arg-type]


def test_diamond_waves():
    plan = _plan(_order("a"), _order("b", ["a"]), _order("c", ["a"]), _order("d", ["b", "c"]))
    assert compute_waves(plan) == [["a"], ["b", "c"], ["d"]]


def test_independent_single_wave():
    plan = _plan(_order("c"), _order("a"), _order("b"))
    assert compute_waves(plan) == [["a", "b", "c"]]


def test_duplicate_id():
    with pytest.raises(PlanError, match="duplicate id: a"):
        validate_plan(_plan(_order("a"), _order("a")))


def test_unknown_dependency():
    with pytest.raises(PlanError, match="unknown dependency: a depends on zz"):
        validate_plan(_plan(_order("a", ["zz"])))


def test_blank_acceptance():
    order = _order("a")
    order["acceptance"] = [" "]
    with pytest.raises(PlanError, match="empty acceptance: a"):
        validate_plan(_plan(order))


def test_two_cycle():
    with pytest.raises(PlanError, match="cycle"):
        validate_plan(_plan(_order("a", ["b"]), _order("b", ["a"])))


def test_three_cycle():
    plan = _plan(_order("a", ["c"]), _order("b", ["a"]), _order("c", ["b"]))
    with pytest.raises(PlanError, match="cycle"):
        validate_plan(plan)


def test_overlap_in_same_wave():
    plan = _plan(
        _order("a"),
        _order("b", ["a"], ["x.py"]),
        _order("c", ["a"], ["./x.py"]),
    )
    with pytest.raises(PlanError, match=r"overlapping files_owned in wave 2: x\.py \(b, c\)"):
        validate_plan(plan)


def test_same_file_in_different_waves_allowed():
    plan = _plan(_order("a", owned=["x.py"]), _order("b", ["a"], ["x.py"]))
    assert validate_plan(plan) == [["a"], ["b"]]


def test_load_plan_valid(tmp_path: Path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"stage": "s", "orders": [_order("a")]}), encoding="utf-8")
    assert isinstance(load_plan(path), Plan)


def test_load_plan_invalid_json(tmp_path: Path):
    path = tmp_path / "plan.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(PlanError, match="invalid JSON"):
        load_plan(path)


def test_load_plan_missing_stage(tmp_path: Path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"orders": [_order("a")]}), encoding="utf-8")
    with pytest.raises(PlanError, match="invalid plan"):
        load_plan(path)
