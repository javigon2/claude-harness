from pathlib import Path

from harness.models import Plan
from harness.order_check import check_plan_quality


def order(id="a", **kw):
    base = {
        "id": id,
        "title": "T",
        "goal": "G",
        "tier": "haiku",
        "depends_on": [],
        "files_owned": [f"{id}.py"],
        "files_readonly": [],
        "context": [],
        "steps": ["s"],
        "constraints": [],
        "acceptance": ["true"],
        "budget": {"max_turns": 5, "max_attempts": 1},
        "out_of_scope": [],
    }
    base.update(kw)
    return base


def plan(*orders) -> Plan:
    return Plan.model_validate({"stage": "s", "orders": list(orders)})


def test_clean(tmp_path: Path):
    assert check_plan_quality(plan(order()), tmp_path) == []


def test_missing_readonly(tmp_path: Path):
    p = plan(order(files_readonly=["x.py"]))
    assert check_plan_quality(p, tmp_path) == [
        "a: files_readonly x.py does not exist and no earlier order creates it"
    ]


def test_readonly_exists_in_repo(tmp_path: Path):
    (tmp_path / "x.py").write_text("")
    assert check_plan_quality(plan(order(files_readonly=["x.py"])), tmp_path) == []


def test_readonly_created_upstream_transitively(tmp_path: Path):
    p = plan(
        order("a", files_owned=["new.py"]),
        order("b", depends_on=["a"]),
        order("c", depends_on=["b"], files_readonly=["new.py"]),
    )
    assert check_plan_quality(p, tmp_path) == []


def test_readonly_owned_by_non_dependency(tmp_path: Path):
    p = plan(order("a", files_owned=["new.py"]), order("b", files_readonly=["new.py"]))
    assert len(check_plan_quality(p, tmp_path)) == 1


def test_bad_owned_paths(tmp_path: Path):
    p = plan(order("a", files_owned=["/abs.py", "../up.py"]))
    assert check_plan_quality(p, tmp_path) == [
        "a: files_owned /abs.py must be a relative path without ..",
        "a: files_owned ../up.py must be a relative path without ..",
    ]


def test_context_sizes(tmp_path: Path):
    big = {"label": "big", "content": "x" * 6001}
    p = plan(order(context=[big]))
    assert check_plan_quality(p, tmp_path) == ["a: context item 'big' is 6001 chars (max 6000)"]
    many = [{"label": f"c{i}", "content": "x" * 5000} for i in range(5)]
    assert check_plan_quality(plan(order(context=many)), tmp_path) == [
        "a: context totals 25000 chars (max 24000)"
    ]


def test_no_steps_and_unparsable_acceptance(tmp_path: Path):
    p = plan(order(steps=[], acceptance=["echo 'oops"]))
    assert check_plan_quality(p, tmp_path) == [
        "a: no steps",
        "a: acceptance command does not parse: echo 'oops",
    ]
