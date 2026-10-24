import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from git_helpers import init_repo

from harness.models import Usage
from harness.planner import FakePlannerSession, PlannerTurn
from harness.planner_log import PlannerLog, read_planner_events
from harness.ui.chat import ChatManager
from harness.ui.orchestrator import (
    ROUTING_BUSY,
    Orchestrator,
    OrchestratorConflict,
    new_thread_id,
)


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def route(to, plan="project", reason="because", cost=0.02, text="ok"):
    body = json.dumps({"to": to, "plan": plan, "reason": reason})
    return PlannerTurn(text=f"{text}\n\n```json\n{body}\n```", usage=Usage(cost_usd=cost))


class StubOpus:
    def start(self, text, plan):
        return "planner-1"

    def message(self, text):
        pass

    def live(self, session_id):
        return False

    def waiting(self, session_id):
        return False


def _dead_pid() -> int:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    return int(done.stdout)


def types(repo, type_=None):
    evs = [e for e in read_planner_events(repo) if e.run_id.startswith("orch-")]
    return [e for e in evs if type_ is None or e.type == type_]


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path)


@pytest.fixture
def made(repo):
    items = []

    def make(steps=None, session_factory=None):
        chats = ChatManager(repo, fake=True, fake_delay=0)
        steps = steps if steps is not None else []

        def factory(r, p):
            return FakePlannerSession([steps.pop(0)])

        orch = Orchestrator(repo, chats, StubOpus(), session_factory=session_factory or factory)
        items.append((orch, chats))
        return orch, steps

    yield make
    for orch, chats in items:
        orch.close()
        chats.close()


def test_new_thread_closes_and_restarts(repo, made):
    orch, steps = made()
    steps.append(route("haiku"))
    orch.message("one")
    wait_for(lambda: orch.view().state == "idle")
    old = orch.view()
    assert old.thread_id and old.cost_usd == 0.02
    orch.new_thread()
    (closed,) = types(repo, "orchestrator_closed")
    assert closed.payload == {"reason": "new request"}
    assert closed.run_id == old.thread_id
    view = orch.view()
    assert view.thread_id is None and view.transcript == []
    assert view.holder is None and view.state == "idle"
    assert view.cost_usd == 0.02
    wait_for(lambda: new_thread_id() != old.thread_id, timeout=3.0)
    steps.append(route("sonnet"))
    orch.message("two")
    wait_for(lambda: orch.view().state == "idle")
    kinds = [e.type for e in types(repo)]
    assert kinds.index("orchestrator_closed") < len(kinds) - 1
    assert kinds[kinds.index("orchestrator_closed") + 1] == "orchestrator_started"
    assert orch.view().thread_id is not None


def test_new_thread_while_routing_and_without_thread(repo, made):
    gate = threading.Event()

    class Gated:
        async def send(self, text):
            await asyncio.to_thread(gate.wait, 3.0)
            return route("haiku")

        async def close(self):
            pass

    orch, _ = made(session_factory=lambda r, p: Gated())
    orch.new_thread()
    assert types(repo) == []
    orch.message("go")
    try:
        with pytest.raises(OrchestratorConflict, match=ROUTING_BUSY):
            orch.new_thread()
    finally:
        gate.set()
    wait_for(lambda: orch.view().state == "idle")


def test_restore_closes_interrupted_turn_once(repo, made):
    log = PlannerLog(repo, "orch-20260101-000000")
    log.emit("orchestrator_started", {"pid": _dead_pid()})
    log.emit("orchestrator_user", {"text": "hi"})
    orch, _ = made()
    orch.restore()
    (failed,) = types(repo, "orchestrator_failed")
    assert failed.payload == {"reason": "interrupted: server restarted", "cost_usd": 0.0}
    view = orch.view()
    assert view.state == "failed" and view.error == "interrupted: server restarted"
    orch.restore()
    orch2, _ = made()
    orch2.restore()
    assert len(types(repo, "orchestrator_failed")) == 1


def test_restore_live_thread_resumes(repo, made):
    tid = "orch-20260101-000000"
    log = PlannerLog(repo, tid)
    log.emit("orchestrator_started", {"pid": os.getpid()})
    log.emit("orchestrator_user", {"text": "hi"})
    log.emit(
        "orchestrator_turn",
        {
            "action": "start",
            "to": "haiku",
            "plan": "quick",
            "reason": "r",
            "target_kind": "chat",
            "target_id": "chat-x",
            "cost_usd": 0.01,
            "text": "Sent to haiku: r",
        },
    )
    orch, steps = made()
    orch.restore()
    assert orch.view().state == "idle" and orch.view().thread_id == tid
    assert types(repo, "orchestrator_failed") == []
    steps.append(route("haiku"))
    orch.message("again")
    wait_for(lambda: orch.view().state != "routing")
    evs = types(repo)
    assert all(e.run_id == tid for e in evs)
    kinds = [e.type for e in evs]
    assert kinds[3:5] == ["orchestrator_started", "orchestrator_user"]
    assert evs[3].payload == {"pid": os.getpid()}


def test_restore_without_log_or_closed_thread(repo, made):
    orch, _ = made()
    orch.restore()
    view = orch.view()
    assert view.thread_id is None and view.state == "idle"
    log = PlannerLog(repo, "orch-20260101-000000")
    log.emit("orchestrator_started", {"pid": os.getpid()})
    log.emit("orchestrator_user", {"text": "hi"})
    log.emit("orchestrator_closed", {"reason": "new request"})
    orch2, _ = made()
    orch2.restore()
    view = orch2.view()
    assert view.thread_id is None and view.state == "idle"
    assert types(repo, "orchestrator_failed") == []
