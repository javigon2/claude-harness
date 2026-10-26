import os
from pathlib import Path

from harness.models import Budget, Plan, TaskState, WorkOrder
from harness.state import RunState, run_dir, save_state
from harness.ui.runs import (
    RUN_OPTIONS_FILE,
    RunOptions,
    list_plans,
    load_run_options,
    options_summary,
    save_run_options,
    unstaged_stages,
)


def _order(oid: str, deps: tuple[str, ...] = ()) -> WorkOrder:
    return WorkOrder(
        id=oid,
        title=oid,
        goal="g",
        tier="haiku",
        depends_on=list(deps),
        files_owned=[f"{oid}.txt"],
        files_readonly=[],
        context=[],
        steps=[],
        constraints=[],
        acceptance=["true"],
        budget=Budget(max_turns=5, max_attempts=1),
        out_of_scope=[],
    )


def _write_plan(repo: Path, name: str, plan: Plan | str) -> str:
    d = repo / ".harness" / "plans" / name
    d.mkdir(parents=True)
    text = plan if isinstance(plan, str) else plan.model_dump_json()
    (d / "plan.json").write_text(text, encoding="utf-8")
    return f".harness/plans/{name}/plan.json"


def _two_waves() -> Plan:
    return Plan(stage="s", orders=[_order("a"), _order("b", ("a",))])


def _run(
    repo: Path,
    run_id: str,
    plan_path: str,
    statuses: tuple[str, ...],
    finished: bool,
    pid: int | None = None,
) -> None:
    tasks = {f"t{i}": TaskState(task_id=f"t{i}", status=s) for i, s in enumerate(statuses)}  # type: ignore[arg-type]
    state = RunState(
        run_id=run_id, plan_path=plan_path, stage="s", finished=finished, tasks=tasks, pid=pid
    )
    save_state(run_dir(repo, run_id), state)


def test_list_plans_missing_dir(tmp_path: Path) -> None:
    assert list_plans(tmp_path) == []


def test_list_plans_order_counts_and_errors(tmp_path: Path) -> None:
    _write_plan(tmp_path, "quick-20261001-100000", _two_waves())
    _write_plan(tmp_path, "stage-10", _two_waves())
    _write_plan(tmp_path, "stage-2", Plan(stage="s", orders=[_order("a")]))
    _write_plan(tmp_path, "quick-20261002-100000", "{not json")
    _write_plan(tmp_path, "stage-1", Plan(stage="s", orders=[_order("a", ("zz",))]))
    (tmp_path / ".harness" / "plans" / "stage-3").mkdir()  # no plan.json: not listed
    (tmp_path / ".harness" / "plans" / "other").mkdir()
    (tmp_path / ".harness" / "plans" / "other" / "plan.json").write_text("{}")

    entries = list_plans(tmp_path)

    assert [(e.kind, e.stage, e.path) for e in entries] == [
        ("stage", 1, ".harness/plans/stage-1/plan.json"),
        ("stage", 2, ".harness/plans/stage-2/plan.json"),
        ("stage", 10, ".harness/plans/stage-10/plan.json"),
        ("quick", None, ".harness/plans/quick-20261002-100000/plan.json"),
        ("quick", None, ".harness/plans/quick-20261001-100000/plan.json"),
    ]
    assert entries[0].error == "unknown dependency: a depends on zz"
    assert (entries[0].orders, entries[0].waves) == (0, 0)
    assert (entries[1].orders, entries[1].waves, entries[1].error) == (1, 1, None)
    assert (entries[2].orders, entries[2].waves) == (2, 2)
    assert entries[3].error is not None and "invalid JSON" in entries[3].error
    assert entries[4].error is None


def test_list_plans_last_run_and_status(tmp_path: Path) -> None:
    p1 = _write_plan(tmp_path, "stage-1", _two_waves())
    p2 = _write_plan(tmp_path, "stage-2", _two_waves())
    p3 = _write_plan(tmp_path, "stage-3", _two_waves())
    p4 = _write_plan(tmp_path, "stage-4", _two_waves())
    _run(tmp_path, "20261001-000001", p1, ("failed",), True)
    _run(tmp_path, "20261001-000002", p1, ("done", "done"), True)  # newest of p1
    _run(tmp_path, "20261001-000003", p2, ("done", "paused"), True)
    _run(tmp_path, "20261001-000004", p3, ("running",), False, pid=os.getpid())
    _run(tmp_path, "20261001-000005", p4, ("running",), False, pid=2**22 + 12345)

    by_path = {e.path: e for e in list_plans(tmp_path)}

    assert (by_path[p1].last_run, by_path[p1].last_status) == ("20261001-000002", "done")
    assert (by_path[p2].last_run, by_path[p2].last_status) == ("20261001-000003", "stopped")
    assert (by_path[p3].last_run, by_path[p3].last_status) == ("20261001-000004", "running")
    assert by_path[p4].last_status == "stopped"


def test_list_plans_without_runs_has_no_last_run(tmp_path: Path) -> None:
    _write_plan(tmp_path, "stage-1", _two_waves())
    entry = list_plans(tmp_path)[0]
    assert (entry.last_run, entry.last_status) == (None, None)


PROJECT = "# P\n\nStatus: {status}\n\n## Stage 1: One\n\n## Stage 2: Two\n\n## Stage 3: Three\n"


def _project_plan(repo: Path, status: str) -> None:
    d = repo / ".harness" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    (d / "PROJECT_PLAN.md").write_text(PROJECT.format(status=status), encoding="utf-8")


def test_unstaged_stages(tmp_path: Path) -> None:
    assert unstaged_stages(tmp_path) == []  # no plan
    _project_plan(tmp_path, "draft")
    assert unstaged_stages(tmp_path) == []
    _project_plan(tmp_path, "approved 2026-10-23")
    assert unstaged_stages(tmp_path) == [1, 2, 3]
    _write_plan(tmp_path, "stage-2", _two_waves())
    assert unstaged_stages(tmp_path) == [1, 3]


def test_run_options_file(tmp_path: Path) -> None:
    default = RunOptions(fake=True, max_cost=2.5)
    assert load_run_options(tmp_path, default) == default

    saved = RunOptions(review=True, max_parallel=2, max_cost=1.0, fake=False)
    save_run_options(tmp_path, saved)
    assert (tmp_path / RUN_OPTIONS_FILE).is_file()
    assert load_run_options(tmp_path, default) == saved

    (tmp_path / RUN_OPTIONS_FILE).write_text('{"max_parallel": 99}')
    assert load_run_options(tmp_path, default) == default
    (tmp_path / RUN_OPTIONS_FILE).write_text("nope")
    assert load_run_options(tmp_path, default) == default


def test_options_summary() -> None:
    assert options_summary(RunOptions()) == "review off · 4 parallel · no cost cap · real workers"
    assert (
        options_summary(RunOptions(review=True, max_parallel=2, max_cost=1.5, fake=True))
        == "review on · 2 parallel · cap $1.50 · fake workers"
    )
