from pathlib import Path

import pytest
from ui_fixtures import (
    CAP_REASON,
    PLAN_PATH,
    RATE_LIMIT_REASON,
    append_event,
    write_blocked_run,
    write_capped_run,
    write_happy_run,
    write_paused_run,
    write_running_run,
)

from harness.events import EVENTS_FILE, read_events
from harness.state import STATE_FILE, load_state, run_dir, runs_root, save_state
from harness.ui.view import RunView, build_view, list_runs


def _rows(view: RunView) -> list[tuple]:
    return [
        (t.task_id, t.title, t.status, t.tier, t.attempt, t.max_attempts, t.escalated)
        for t in view.tasks
    ]


def _tiers(view: RunView) -> list[tuple]:
    return [(t.tier, t.attempts, t.cost_usd) for t in view.tiers]


def _snapshot(root: Path) -> dict:
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_list_runs_order_and_missing(tmp_path: Path) -> None:
    assert list_runs(tmp_path) == []
    write_blocked_run(tmp_path)
    write_happy_run(tmp_path)
    write_running_run(tmp_path)
    (runs_root(tmp_path) / "zzz-empty").mkdir()
    assert list_runs(tmp_path) == ["running1", "happy1", "blocked1"]


def test_blocked_view(tmp_path: Path) -> None:
    write_blocked_run(tmp_path)
    view = build_view(tmp_path, "blocked1")
    assert view.finished is True
    assert view.stage == "UI fixture stage"
    assert (view.wave, view.waves_total) == (3, 3)
    assert view.cost_usd == pytest.approx(0.25)
    tiers = _tiers(view)
    assert [(t, n) for t, n, _ in tiers] == [("haiku", 1), ("sonnet", 1), ("opus", 1)]
    assert [c for _, _, c in tiers] == pytest.approx([0.01, 0.04, 0.2])
    assert _rows(view) == [
        ("a", "Store", "blocked", "opus", 3, 3, True),
        ("b", "CLI", "blocked", "haiku", 0, 3, False),
        ("c", "Docs", "blocked", "sonnet", 0, 3, False),
    ]
    a, b, c = view.tasks
    assert (a.input_tokens, a.output_tokens) == (700, 70)
    assert a.cost_usd == pytest.approx(0.25)
    state = load_state(run_dir(tmp_path, "blocked1"))
    assert a.detail == state.tasks["a"].replan_note
    assert a.detail.startswith("Task a is blocked after 3 attempts (tiers: haiku, sonnet, opus)")
    assert b.detail == "dependency a is blocked"
    assert b.cost_usd == 0
    assert c.detail == "dependency b is blocked"
    assert len(view.events) == 29
    assert view.events[0].text == "run started: 3 orders in 3 waves"
    assert view.events[-1].text == "run finished: 0 done, 0 failed, 3 blocked"
    blocked = [e for e in view.events if e.type == "task_blocked" and e.task_id == "a"]
    assert blocked[0].text == "a blocked after 3 attempts on opus"


def test_blocked_max_events(tmp_path: Path) -> None:
    write_blocked_run(tmp_path)
    view = build_view(tmp_path, "blocked1", max_events=5)
    assert [e.text for e in view.events] == [
        "wave 2 finished",
        "wave 3 started: c",
        "c blocked: dependency b is blocked",
        "wave 3 finished",
        "run finished: 0 done, 0 failed, 3 blocked",
    ]
    assert build_view(tmp_path, "blocked1", max_events=0).events == []


def test_happy_view(tmp_path: Path) -> None:
    write_happy_run(tmp_path)
    view = build_view(tmp_path, "happy1")
    assert view.finished is True
    assert (view.wave, view.waves_total) == (3, 3)
    assert view.cost_usd == pytest.approx(0.12)
    tiers = _tiers(view)
    assert [(t, n) for t, n, _ in tiers] == [("haiku", 2), ("sonnet", 2)]
    assert [c for _, _, c in tiers] == pytest.approx([0.02, 0.1])
    assert _rows(view) == [
        ("a", "Store", "done", "haiku", 1, 3, False),
        ("b", "CLI", "done", "sonnet", 2, 3, True),
        ("c", "Docs", "done", "sonnet", 1, 3, False),
    ]
    b = view.tasks[1]
    assert (b.input_tokens, b.output_tokens) == (300, 30)
    assert b.cost_usd == pytest.approx(0.05)
    assert all(t.detail is None for t in view.tasks)
    assert len(view.events) == 38
    esc = [e for e in view.events if e.type == "escalated"]
    assert esc[0].text == "b escalated haiku -> sonnet"
    expected = [e.ts.isoformat() for e in read_events(run_dir(tmp_path, "happy1") / EVENTS_FILE)]
    assert [e.ts for e in view.events] == expected


def test_running_view_and_append(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    view = build_view(tmp_path, "running1")
    assert view.finished is False
    assert (view.wave, view.waves_total) == (2, 3)
    assert view.cost_usd == pytest.approx(0.01)
    assert _tiers(view)[0][:2] == ("haiku", 1)
    assert len(view.tiers) == 1
    assert _rows(view) == [
        ("a", "Store", "done", "haiku", 1, 3, False),
        ("b", "CLI", "running", "haiku", 1, 3, False),
        ("c", "Docs", "pending", "sonnet", 0, 3, False),
    ]
    assert len(view.events) == 14
    assert view.events[-1].text == "b attempt 1 on haiku"
    append_event(tmp_path, "running1", "task_done", "b", {"attempts": 1, "tier": "haiku"})
    view = build_view(tmp_path, "running1")
    assert len(view.events) == 15
    assert view.events[-1].text == "b done on haiku after 1 attempts"


def test_corrupt_event_lines(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    events_path = run_dir(tmp_path, "running1") / EVENTS_FILE
    with events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026')
    assert len(build_view(tmp_path, "running1").events) == 14
    lines = events_path.read_text(encoding="utf-8").split("\n")
    lines.insert(5, "not json")
    events_path.write_text("\n".join(lines), encoding="utf-8")
    assert len(build_view(tmp_path, "running1").events) == 14


def test_missing_and_invalid_plan(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    expected = [("a", None, "haiku"), ("b", None, "haiku"), ("c", None, None)]
    (tmp_path / PLAN_PATH).unlink()
    view = build_view(tmp_path, "running1")
    assert [(t.title, t.max_attempts, t.tier) for t in view.tasks] == [
        (tid, m, tier) for tid, m, tier in expected
    ]
    (tmp_path / PLAN_PATH).write_text("{", encoding="utf-8")
    view = build_view(tmp_path, "running1")
    assert [(t.title, t.max_attempts, t.tier) for t in view.tasks] == expected


def test_no_events_file(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    (run_dir(tmp_path, "running1") / EVENTS_FILE).unlink()
    view = build_view(tmp_path, "running1")
    assert view.events == []
    assert (view.wave, view.waves_total) == (0, 0)
    assert len(view.tasks) == 3


def test_failed_detail(tmp_path: Path) -> None:
    write_happy_run(tmp_path)
    directory = run_dir(tmp_path, "happy1")
    state = load_state(directory)
    state.tasks["b"].status = "failed"
    state.tasks["b"].history[-1].feedback = "boom"
    save_state(directory, state)
    assert build_view(tmp_path, "happy1").tasks[1].detail == "boom"
    err = "budget exhausted after 2 attempts; last tier sonnet"
    state.tasks["b"].error = err
    save_state(directory, state)
    assert build_view(tmp_path, "happy1").tasks[1].detail == err


def test_missing_run(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as exc:
        build_view(tmp_path, "nope")
    assert str(exc.value) == f"No run state at {run_dir(tmp_path, 'nope') / STATE_FILE}"


def test_read_only_and_round_trip(tmp_path: Path) -> None:
    write_blocked_run(tmp_path)
    write_happy_run(tmp_path)
    write_running_run(tmp_path)
    before = _snapshot(tmp_path)
    list_runs(tmp_path)
    for rid in ("blocked1", "happy1", "running1"):
        build_view(tmp_path, rid)
    assert _snapshot(tmp_path) == before
    view = build_view(tmp_path, "happy1")
    assert RunView.model_validate_json(view.model_dump_json()) == view


def test_paused_view(tmp_path: Path) -> None:
    write_paused_run(tmp_path)
    view = build_view(tmp_path, "paused1")
    assert view.phase == "stopped"
    assert view.stopped_reason == RATE_LIMIT_REASON
    b = view.tasks[1]
    assert (b.task_id, b.status, b.attempt) == ("b", "paused", 0)
    assert b.detail == RATE_LIMIT_REASON
    assert b.activity is None
    assert (view.wave, view.waves_total) == (2, 3)
    assert len(view.events) == 18
    assert all(e.type != "worker_progress" for e in view.events)
    assert view.events[-1].text == "run finished: 1 done, 0 failed, 0 blocked, 1 paused"
    texts = [e.text for e in view.events]
    assert f"run stopping: {RATE_LIMIT_REASON}" in texts
    assert f"b paused: {RATE_LIMIT_REASON}" in texts


def test_capped_view(tmp_path: Path) -> None:
    write_capped_run(tmp_path)
    view = build_view(tmp_path, "capped1")
    assert view.phase == "stopped"
    assert view.stopped_reason == CAP_REASON
    assert [t.status for t in view.tasks] == ["done", "pending", "pending"]
    assert view.cost_usd == pytest.approx(1.2)
    assert "cost cap reached: $1.2000 of $1.0000" in [e.text for e in view.events]


def test_finished_views_have_no_stop_or_activity(tmp_path: Path) -> None:
    write_happy_run(tmp_path)
    write_blocked_run(tmp_path)
    for run_id in ("happy1", "blocked1"):
        view = build_view(tmp_path, run_id)
        assert view.phase == "finished"
        assert view.stopped_reason is None
        assert all(t.activity is None for t in view.tasks)


def test_running_activity(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    view = build_view(tmp_path, "running1")
    assert view.phase == "running"
    assert view.stopped_reason is None
    prog = {"attempt": 1, "tier": "haiku", "tool": "Read", "detail": "src/b.py"}
    append_event(tmp_path, "running1", "worker_progress", "b", prog)
    append_event(
        tmp_path, "running1", "worker_progress", "b", {**prog, "tool": "Bash", "detail": ""}
    )
    view = build_view(tmp_path, "running1")
    assert view.tasks[1].activity == "Bash"
    assert len(view.events) == 14
    assert all(e.type != "worker_progress" for e in view.events)
    append_event(tmp_path, "running1", "worker_progress", "a", prog)
    assert build_view(tmp_path, "running1").tasks[0].activity is None
    append_event(tmp_path, "running1", "attempt_started", "b", {"attempt": 2, "tier": "haiku"})
    assert build_view(tmp_path, "running1").tasks[1].activity is None


def test_feed_not_crowded_by_progress(tmp_path: Path) -> None:
    write_running_run(tmp_path)
    prog = {"attempt": 1, "tier": "haiku", "tool": "Read", "detail": "x"}
    for _ in range(120):
        append_event(tmp_path, "running1", "worker_progress", "b", prog)
    full = build_view(tmp_path, "running1", max_events=50)
    assert len(full.events) == 14
    assert all(e.type != "worker_progress" for e in full.events)
    assert build_view(tmp_path, "running1", max_events=5).events == full.events[-5:]


def test_resumed_run_clears_stopped_reason(tmp_path: Path) -> None:
    write_paused_run(tmp_path)
    append_event(
        tmp_path, "paused1", "run_started", None, {"orders": 3, "waves": [["a"], ["b"], ["c"]]}
    )
    view = build_view(tmp_path, "paused1")
    assert view.stopped_reason is None
    assert view.phase == "stopped"


def test_new_views_are_read_only(tmp_path: Path) -> None:
    write_paused_run(tmp_path)
    write_capped_run(tmp_path)
    before = _snapshot(tmp_path / ".harness")
    build_view(tmp_path, "paused1")
    build_view(tmp_path, "capped1")
    assert _snapshot(tmp_path / ".harness") == before
