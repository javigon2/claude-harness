import subprocess
import sys
import time

import pytest
from git_helpers import init_repo

from harness.planner_log import PlannerLog, read_planner_events
from harness.ui.office import (
    Office,
    _OfficeOpusDesk,
    office_view_from_log,
)
from harness.ui.orchestrator import OPUS_HOLDS, HandoffRefused


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def _dead_pid() -> int:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    return int(done.stdout)


@pytest.fixture
def office(tmp_path):
    init_repo(tmp_path)
    o = Office(tmp_path, fake=True, fake_delay=0)
    yield o
    o.close()


def test_haiku_then_opus_handoff(tmp_path, office):
    office.orchestrator.message("haiku fix the typo")
    wait_for(lambda: any(c.state == "waiting" for c in office.view().chats))
    wait_for(lambda: office.view().orchestrator.state == "idle")
    assert "- haiku fix the typo" in (tmp_path / "notes.md").read_text(encoding="utf-8")
    v = office.view()
    assert v.orchestrator.holder == "haiku"
    assert v.orchestrator.cost_usd == pytest.approx(0.01)
    assert v.total_cost_usd == pytest.approx(0.012)
    assert office_view_from_log(tmp_path).total_cost_usd == pytest.approx(0.012)

    office.orchestrator.message("opus quick redo this across modules")
    wait_for(lambda: office.view().state == "ready" and office.view().plan_kind == "quick")
    wait_for(lambda: office.view().orchestrator.holder == "opus")
    v = office.view()
    assert all(c.state != "waiting" for c in v.chats)
    assert v.orchestrator.holder_id == v.session_id
    users = [e.payload["text"] for e in read_planner_events(tmp_path) if e.type == "planner_user"]
    assert users[-1].startswith("You are taking over this work from haiku")
    assert v.orchestrator.transcript[-1].who == "opus"

    wait_for(lambda: office.view().orchestrator.state == "idle")
    office.orchestrator.message("one more thing")
    events = read_planner_events(tmp_path)
    assert [e for e in events if e.type == "planner_user" and e.payload["text"] == "one more thing"]
    turns = [e for e in events if e.type == "orchestrator_turn"]
    assert turns[-1].payload["action"] == "forward"
    assert turns[-1].payload["reason"] == OPUS_HOLDS


def test_old_auto_session_roles(tmp_path):
    def write(sid, route):
        log = PlannerLog(tmp_path, sid)
        log.emit("planner_started", {"route": route, "pid": 1})
        log.emit("planner_user", {"text": "do it"})
        log.emit("planner_reply", {"text": "triage says hi", "cost_usd": 0.1})
        log.emit("planner_triage", {"route": "quick", "reason": "r"})
        log.emit("planner_reply", {"text": "opus plan", "cost_usd": 0.2})
        log.emit("planner_done", {"cost_usd": 0.2})

    write("planner-a", "auto")
    roles = [m.role for m in office_view_from_log(tmp_path).transcript]
    assert roles == ["user", "orchestrator", "opus"]
    write("planner-b", "project")
    roles = [m.role for m in office_view_from_log(tmp_path).transcript]
    assert roles == ["user", "opus", "opus"]


def test_restore_closes_interrupted_orchestrator_turn(tmp_path):
    init_repo(tmp_path)
    log = PlannerLog(tmp_path, "orch-20260101-000000")
    log.emit("orchestrator_started", {"pid": _dead_pid()})
    log.emit("orchestrator_user", {"text": "haiku hi"})
    o = Office(tmp_path, fake=True, fake_delay=0)
    try:
        o.open(0)
        v = o.view().orchestrator
        assert v.state == "failed"
        assert v.error == "interrupted: server restarted"
    finally:
        o.close()


def test_opus_desk_refuses_and_reports(office):
    desk = _OfficeOpusDesk(office)
    with pytest.raises(HandoffRefused, match="the planner is not waiting for a message"):
        desk.message("x")
    assert desk.live("other") is False
    assert desk.waiting("other") is False
