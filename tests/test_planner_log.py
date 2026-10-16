import json
import threading
from pathlib import Path

from typer.testing import CliRunner

import harness.cli
from harness.models import Usage
from harness.planner import (
    PROJECT_PLAN_FILE,
    STAGE_PLAN_DIR,
    FakePlannerSession,
    PlannerTurn,
)
from harness.planner_log import (
    PLANNER_EVENTS,
    ObservedSession,
    PlannerLog,
    last_session,
    new_session_id,
    read_planner_events,
)

runner = CliRunner()


def _plan(repo: Path, status: str, body: str = "x") -> None:
    path = repo / PROJECT_PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Plan\n\nStatus: {status}\n\n{body}\n", encoding="utf-8")


def _types(repo: Path) -> list[str]:
    return [e.type for e in read_planner_events(repo)]


def test_session_id_shape():
    assert new_session_id().startswith("planner-")


def test_emit_writes_valid_event_lines(tmp_path):
    log = PlannerLog(tmp_path, "planner-1")
    log.emit("planner_started", {"stage": 1})
    log.emit("planner_progress", {"tool": "Read", "detail": "a"})
    lines = (tmp_path / PLANNER_EVENTS).read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["run_id"] == "planner-1"
    assert first["task_id"] is None
    assert first["type"] == "planner_started"
    assert first["payload"] == {"stage": 1}


def test_concurrent_emits_from_threads_stay_whole_lines(tmp_path):
    logs = [PlannerLog(tmp_path, f"planner-{n}") for n in range(4)]

    def work(log: PlannerLog) -> None:
        for i in range(50):
            log.emit("planner_progress", {"tool": "Read", "detail": "x" * 200, "i": i})

    threads = [threading.Thread(target=work, args=(log,)) for log in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / PLANNER_EVENTS).read_text().splitlines()
    assert len(lines) == 200
    assert len(read_planner_events(tmp_path)) == 200


def test_read_skips_bad_and_partial_lines(tmp_path):
    PlannerLog(tmp_path, "planner-1").emit("planner_started")
    with (tmp_path / PLANNER_EVENTS).open("a") as handle:
        handle.write("not json\n\n")
        handle.write('{"ts": "2026-01-01T00:00:00Z", "run_id": "x"')
    assert _types(tmp_path) == ["planner_started"]


def test_read_without_a_log_is_empty(tmp_path):
    assert read_planner_events(tmp_path) == []


def test_last_session_takes_only_the_last_started(tmp_path):
    a, b = PlannerLog(tmp_path, "planner-a"), PlannerLog(tmp_path, "planner-b")
    a.emit("planner_started")
    a.emit("planner_done")
    b.emit("planner_started")
    a.emit("planner_progress")  # another session's stray line is dropped
    b.emit("planner_reply")
    session = last_session(read_planner_events(tmp_path))
    assert [(e.run_id, e.type) for e in session] == [
        ("planner-b", "planner_started"),
        ("planner-b", "planner_reply"),
    ]
    assert last_session([]) == []


async def test_plan_ready_only_when_the_file_changed_during_the_turn(tmp_path):
    def writes(_: str) -> PlannerTurn:
        _plan(tmp_path, "draft", "v1")
        return PlannerTurn(text="draft written", usage=Usage(cost_usd=0.5))

    def same_content(_: str) -> PlannerTurn:
        _plan(tmp_path, "draft", "v1")
        return PlannerTurn(text="rewrote it identically")

    inner = FakePlannerSession(
        [
            PlannerTurn(text="a question?"),
            writes,
            PlannerTurn(text="still a question"),
            same_content,
        ]
    )
    log = PlannerLog(tmp_path, "planner-1")
    s = ObservedSession(inner, tmp_path, log, lambda: "project")
    t1 = await s.send("hello")
    assert t1.text == "a question?"
    assert "plan_ready" not in _types(tmp_path)
    await s.send("answer")
    assert _types(tmp_path).count("plan_ready") == 1
    assert s.cost_usd == 0.5
    await s.send("more")
    await s.send("again")  # same bytes: not a change
    events = read_planner_events(tmp_path)
    assert [e.type for e in events].count("plan_ready") == 1
    replies = [e.payload for e in events if e.type == "planner_reply"]
    assert [r["plan_changed"] for r in replies] == [False, True, False, False]
    messages = [e.payload for e in events if e.type == "planner_message"]
    assert [m["chars"] for m in messages] == [5, 6, 4, 5]
    ready = next(e for e in events if e.type == "plan_ready")
    assert ready.payload["path"] == ".harness/plans/PROJECT_PLAN.md"
    await s.close()
    assert inner.closed


async def test_no_plan_ready_once_approved_or_outside_project_phase(tmp_path):
    def approve(_: str) -> PlannerTurn:
        _plan(tmp_path, "approved 2026-10-16", "v2")
        return PlannerTurn(text="approved")

    log = PlannerLog(tmp_path, "planner-1")
    s = ObservedSession(FakePlannerSession([approve]), tmp_path, log, lambda: "project")
    await s.send("approve")
    assert "plan_ready" not in _types(tmp_path)

    def edit(_: str) -> PlannerTurn:
        _plan(tmp_path, "draft", "v3")
        return PlannerTurn(text="edited")

    s2 = ObservedSession(FakePlannerSession([edit]), tmp_path, log, lambda: "staging")
    await s2.send("go")
    assert "plan_ready" not in _types(tmp_path)
    assert read_planner_events(tmp_path)[-1].payload["plan_changed"] is True


def test_harness_plan_logs_a_terminal_session(tmp_path, monkeypatch):
    project_plan = tmp_path / PROJECT_PLAN_FILE

    def draft(_: str) -> PlannerTurn:
        project_plan.parent.mkdir(parents=True, exist_ok=True)
        project_plan.write_text("# Plan\n\nStatus: draft\n", encoding="utf-8")
        return PlannerTurn(text="Draft. Approve?", usage=Usage(cost_usd=0.25))

    def approve(_: str) -> PlannerTurn:
        project_plan.write_text("# Plan\n\nStatus: approved 2026-10-16\n", encoding="utf-8")
        return PlannerTurn(text="Approved.")

    def stage(_: str) -> PlannerTurn:
        d = tmp_path / STAGE_PLAN_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan.json").write_text(json.dumps(_one_order_plan()), encoding="utf-8")
        return PlannerTurn(text="Stage plan written.", usage=Usage(cost_usd=0.4))

    session = FakePlannerSession([draft, approve, stage])
    monkeypatch.setattr(harness.cli, "check_skills", lambda *a, **k: None)
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(
        harness.cli.app, ["plan", "a todo app", "--repo", str(tmp_path)], input="approve\n"
    )
    assert result.exit_code == 0, result.output
    session_events = last_session(read_planner_events(tmp_path))
    types = [e.type for e in session_events]
    assert types[0] == "planner_started"
    assert types[-1] == "planner_done"
    started = session_events[0].payload
    assert started["source"] == "terminal"
    assert started["stage"] == 1
    assert started["description"] == "a todo app"
    assert started["resumed_from_draft"] is False
    assert isinstance(started["pid"], int)
    assert types.count("plan_ready") == 1
    done = session_events[-1].payload
    assert done["plan_path"] == ".harness/plans/stage-1/plan.json"
    assert done["cost_usd"] == 0.4


def test_harness_plan_failure_is_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.cli, "check_skills", lambda *a, **k: None)
    session = FakePlannerSession([PlannerTurn(text="a question?")])
    monkeypatch.setattr(harness.cli, "_make_planner_session", lambda repo: session)
    result = runner.invoke(
        harness.cli.app, ["plan", "a todo app", "--repo", str(tmp_path)], input=""
    )
    assert result.exit_code == 1
    last = last_session(read_planner_events(tmp_path))[-1]
    assert last.type == "planner_failed"
    assert "aborted: no answer" in last.payload["reason"]


def _one_order_plan() -> dict:
    return {
        "stage": "s",
        "orders": [
            {
                "id": "a",
                "title": "T",
                "goal": "G",
                "tier": "haiku",
                "depends_on": [],
                "files_owned": ["a.py"],
                "files_readonly": [],
                "context": [],
                "steps": ["s"],
                "constraints": [],
                "acceptance": ["true"],
                "budget": {"max_turns": 5, "max_attempts": 1},
                "out_of_scope": [],
            }
        ],
    }
