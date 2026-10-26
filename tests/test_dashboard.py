from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.models import AttemptRecord, Event, TaskState, Usage
from harness.state import RunState, run_dir, save_state
from harness.ui.metrics import build_dashboard

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def _usage(inp: int, out: int, cost: float) -> Usage:
    return Usage(input_tokens=inp, output_tokens=out, cost_usd=cost)


def _write_run(
    repo: Path,
    run_id: str,
    *,
    cost: float,
    start: datetime = T0,
    worker_s: float = 60.0,
    latency_s: float = 3.0,
    verify_s: float = 30.0,
    summary: str = "did it",
    finished: bool = True,
) -> None:
    """A one-task run: one attempt on haiku that passes first time."""
    directory = run_dir(repo, run_id)
    record = AttemptRecord(
        attempt=1,
        tier="haiku",
        worker_status="done",
        passed=True,
        usage=_usage(1000, 500, cost),
        summary=summary,
    )
    task = TaskState(
        task_id="t1",
        status="done",
        attempts=1,
        tier_used="haiku",
        usage=_usage(1000, 500, cost),
        history=[record],
    )
    save_state(
        directory,
        RunState(run_id=run_id, plan_path="p", stage="1", finished=finished, tasks={"t1": task}),
    )

    def at(sec: float) -> datetime:
        return start + timedelta(seconds=sec)

    rows = [
        ("run_started", None, 0.0, {}),
        ("attempt_started", "t1", 1.0, {"attempt": 1, "tier": "haiku"}),
        ("worker_progress", "t1", 1.0 + latency_s, {"attempt": 1, "tool": "Read", "detail": "x"}),
        (
            "worker_finished",
            "t1",
            1.0 + worker_s,
            {"attempt": 1, "status": "done", "summary": summary},
        ),
        ("verify_started", "t1", 1.0 + worker_s, {"attempt": 1}),
        ("verify_finished", "t1", 1.0 + worker_s + verify_s, {"attempt": 1, "passed": True}),
    ]
    if finished:
        rows.append(("run_finished", None, 2.0 + worker_s + verify_s, {"cost_usd": cost}))
    lines = [
        Event(ts=at(sec), run_id=run_id, task_id=task_id, type=type_, payload=payload)
        for type_, task_id, sec, payload in rows
    ]
    (directory / "events.jsonl").write_text(
        "\n".join(e.model_dump_json() for e in lines) + "\n", encoding="utf-8"
    )


def _metric(d, key):
    return next(m for m in d.metrics if m.key == key)


def test_one_run_numbers(tmp_path):
    _write_run(tmp_path, "r1", cost=0.5, worker_s=60, latency_s=3, verify_s=30)
    d = build_dashboard(tmp_path, "r1")
    assert _metric(d, "cost_usd").value == pytest.approx(0.5)
    assert _metric(d, "active_s").value == pytest.approx(60.0)  # attempt start to worker end
    assert _metric(d, "verify_s").value == pytest.approx(30.0)
    assert _metric(d, "avg_latency_s").value == pytest.approx(3.0)
    assert _metric(d, "wall_s").value == pytest.approx(92.0)
    assert _metric(d, "first_try_pct").value == pytest.approx(100.0)
    assert _metric(d, "input_tokens").value == 1000
    assert d.tiers[0].tier == "haiku" and d.tiers[0].attempts == 1
    assert d.tasks[0].task_id == "t1" and d.tasks[0].latency_s == pytest.approx(3.0)


def test_cold_start_has_no_averages(tmp_path):
    _write_run(tmp_path, "r1", cost=0.5)
    d = build_dashboard(tmp_path, "r1")
    assert d.baseline_runs == 0
    assert all(m.average is None for m in d.metrics)


def test_average_over_the_other_real_runs(tmp_path):
    _write_run(tmp_path, "r1", cost=0.2, latency_s=2)
    _write_run(tmp_path, "r2", cost=0.4, latency_s=4)
    _write_run(tmp_path, "r3", cost=0.9, latency_s=9)
    d = build_dashboard(tmp_path, "r3")
    assert d.baseline_runs == 2
    assert _metric(d, "cost_usd").average == pytest.approx(0.3)
    assert _metric(d, "avg_latency_s").average == pytest.approx(3.0)
    assert _metric(d, "cost_usd").value == pytest.approx(0.9)


def test_fake_and_unfinished_runs_stay_out_of_the_average(tmp_path):
    _write_run(tmp_path, "r1", cost=0.2)
    _write_run(tmp_path, "r2", cost=0.0, summary="fake worker: no changes")
    _write_run(tmp_path, "r3", cost=5.0, finished=False)
    _write_run(tmp_path, "r4", cost=0.6)
    d = build_dashboard(tmp_path, "r4")
    assert d.baseline_runs == 1
    assert _metric(d, "cost_usd").average == pytest.approx(0.2)
    assert build_dashboard(tmp_path, "r2").fake is True


def test_unknown_run(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_dashboard(tmp_path, "nope")
