from pathlib import Path

from harness.planner_log import PlannerLog
from harness.planning_report import build_reports


def _session(repo: Path, sid: str, stage: int | None = 1) -> PlannerLog:
    log = PlannerLog(repo, sid)
    log.emit("planner_started", {"stage": stage})
    log.emit("planner_triage", {"route": "quick"})
    return log


def _turn(log: PlannerLog, duration: int | None, cost: float, phase: str = "project") -> None:
    log.emit("planner_message", {"phase": phase})
    reply = {"phase": phase, "cost_usd": cost}
    if duration is not None:
        reply["duration_ms"] = duration
    log.emit("planner_reply", reply)


def test_missing_log(tmp_path):
    assert build_reports(tmp_path) == []


def test_turns_phases_cost_and_wait(tmp_path):
    log = _session(tmp_path, "planner-a")
    _turn(log, 4000, 0.5)
    _turn(log, 1000, 0.75, phase="stage")
    (r,) = build_reports(tmp_path)
    assert (r.stage, r.route) == (1, "quick")
    assert r.phase_ms == {"project": 4000, "stage": 1000}
    assert r.model_ms == 5000 and r.cost_usd == 0.75
    assert r.turns[0].wait_before_ms is None
    assert r.turns[1].wait_before_ms is not None and r.turns[1].wait_before_ms >= 0


def test_old_turn_without_duration(tmp_path):
    log = _session(tmp_path, "planner-a")
    _turn(log, None, 0.1)
    (r,) = build_reports(tmp_path)
    assert r.turns[0].duration_ms is None and r.model_ms == 0


def test_tools_and_unfinished_tool(tmp_path):
    log = _session(tmp_path, "planner-a")
    log.emit("planner_tool_finished", {"tool": "Read", "duration_ms": 100, "status": "ok"})
    log.emit("planner_tool_finished", {"tool": "Read", "duration_ms": 300, "status": "ok"})
    log.emit(
        "planner_tool_finished", {"tool": "Bash", "duration_ms": 9000, "status": "missing_result"}
    )
    (r,) = build_reports(tmp_path)
    tools = {t.tool: t for t in r.tools}
    assert (tools["Read"].count, tools["Read"].total_ms, tools["Read"].max_ms) == (2, 400, 300)
    assert (tools["Bash"].total_ms, tools["Bash"].missing_result) == (0, 1)


def test_last_and_session_id(tmp_path):
    for sid in ("planner-a", "planner-b", "planner-c"):
        _turn(_session(tmp_path, sid), 1000, 0.1)
    assert [r.session_id for r in build_reports(tmp_path, last=2)] == ["planner-b", "planner-c"]
    assert [r.session_id for r in build_reports(tmp_path, session_id="planner-a")] == ["planner-a"]
    assert build_reports(tmp_path, session_id="nope") == []
