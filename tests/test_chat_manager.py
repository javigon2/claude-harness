import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.models import TaskState, Usage
from harness.planner import PlannerTurn
from harness.state import RunState, run_dir, save_state
from harness.ui.chat import (
    CHAT_JOIN_TIMEOUT_S,
    ChatConflict,
    ChatManager,
    chats_from_log,
    read_chat_events,
)


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


class Scripted:
    def __init__(self, repo, progress, gate=None, error=None):
        self.repo = repo
        self.progress = progress
        self.gate = gate
        self.error = error
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text):
        self.sent.append(text)
        n = len(self.sent)
        await self.progress({"tool": "Edit", "detail": f"{n}.txt"})
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 5)
        (Path(self.repo) / f"{n}.txt").write_text("x\n", encoding="utf-8")
        return PlannerTurn(text=f"did {n}", usage=Usage(cost_usd=0.01 * n), error=self.error)

    async def close(self):
        self.closed = True


def make(repo, gate=None, error=None, **kw):
    sessions: list[Scripted] = []

    def factory(r, tier, progress):
        s = Scripted(r, progress, gate, error)
        sessions.append(s)
        return s

    return ChatManager(repo, session_factory=factory, **kw), sessions


def view(mgr, chat_id):
    return next(v for v in mgr.views() if v.chat_id == chat_id)


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_lifecycle(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr, sessions = make(repo)
    try:
        cid = mgr.start("haiku", "  add one  ")
        wait_for(lambda: view(mgr, cid).state == "waiting")
        v = view(mgr, cid)
        assert [e.role for e in v.transcript] == ["user", "agent"]
        assert v.transcript[0].text == "add one"
        assert len(v.commits) == 1 and v.commits[0].endswith("chat haiku: add one")
        assert git(repo, "log", "-1", "--format=%s") == "chat haiku: add one"
        mgr.message(cid, "again")
        wait_for(lambda: len(view(mgr, cid).commits) == 2 and view(mgr, cid).state == "waiting")
        assert view(mgr, cid).cost_usd == pytest.approx(0.02)
        with pytest.raises(ChatConflict, match="a haiku chat is already open"):
            mgr.start("haiku", "x")
        mgr.close_chat(cid)
        wait_for(lambda: sessions[0].closed)
        assert view(mgr, cid).state == "closed"
        with pytest.raises(ChatConflict, match="the chat is closed"):
            mgr.message(cid, "hi")
        with pytest.raises(ChatConflict, match="unknown chat"):
            mgr.message("nope", "hi")
    finally:
        mgr.close()


def test_conflicts(tmp_path):
    repo = init_repo(tmp_path / "r")
    busy = {"v": None}
    mgr, _ = make(repo, busy_fn=lambda: busy["v"])
    try:
        with pytest.raises(ChatConflict, match="no opus chat agent; use haiku or sonnet"):
            mgr.start("opus", "x")
        with pytest.raises(ChatConflict, match="the message is empty"):
            mgr.start("haiku", "   ")
        with pytest.raises(ChatConflict, match="the message is too long"):
            mgr.start("haiku", "x" * 20_001)
        with pytest.raises(ChatConflict, match="send both run_id and task_id"):
            mgr.start("haiku", "x", run_id="r1")
        with pytest.raises(ChatConflict, match="unknown task t1 in run r1"):
            mgr.start("haiku", "x", run_id="r1", task_id="t1")
        sentence = "a run is active (r1); wait for it to finish"
        busy["v"] = sentence
        with pytest.raises(ChatConflict, match=r"a run is active \(r1\)"):
            mgr.start("haiku", "x")
        busy["v"] = None
        cid = mgr.start("haiku", "x")
        wait_for(lambda: view(mgr, cid).state == "waiting")
        busy["v"] = sentence
        with pytest.raises(ChatConflict, match=r"a run is active \(r1\)"):
            mgr.message(cid, "y")
        with pytest.raises(ChatConflict, match="the message is empty"):
            mgr.message(cid, " ")
    finally:
        mgr.close()


def test_working_chat(tmp_path):
    repo = init_repo(tmp_path / "r")
    gate = threading.Event()
    mgr, sessions = make(repo, gate=gate)
    try:
        cid = mgr.start("haiku", "go")
        wait_for(lambda: view(mgr, cid).progress == ["Editing 1.txt"])
        assert mgr.working()
        assert view(mgr, cid).state == "working"
        with pytest.raises(ChatConflict, match="the agent is still working"):
            mgr.message(cid, "more")
        mgr.close_chat(cid)
        gate.set()
        wait_for(lambda: sessions[0].closed)
        assert not mgr.working()
        assert read_chat_events(repo)[-1].type == "chat_closed"
    finally:
        gate.set()
        mgr.close()


def test_error_turn_fails_chat(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr, _ = make(repo, error="boom")
    try:
        cid = mgr.start("haiku", "go")
        wait_for(lambda: view(mgr, cid).state == "failed")
        assert view(mgr, cid).error == "agent session failed: boom"
        mgr.error = None
        cid2 = mgr.start("haiku", "again")
        assert cid2 != cid
    finally:
        mgr.close()


def test_non_git_directory(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    mgr, _ = make(repo)
    try:
        cid = mgr.start("sonnet", "go")
        wait_for(lambda: view(mgr, cid).state == "waiting")
        v = view(mgr, cid)
        assert v.error is not None and v.error.startswith("commit failed: ")
        assert v.transcript[-1].text == "did 1"
    finally:
        mgr.close()


def test_close_with_working_chat(tmp_path):
    repo = init_repo(tmp_path / "r")
    gate = threading.Event()
    mgr, _ = make(repo, gate=gate)
    cid = mgr.start("haiku", "go")
    wait_for(lambda: view(mgr, cid).progress)
    threading.Timer(0.3, gate.set).start()
    mgr.close()
    gate.set()
    assert CHAT_JOIN_TIMEOUT_S > 0
    closed = [e for e in read_chat_events(repo) if e.type == "chat_closed"]
    assert closed[-1].payload == {"reason": "server stopped"}
    assert chats_from_log(repo)[0].state == "closed"


def test_fake_mode(tmp_path):
    repo = init_repo(tmp_path / "r")
    mgr = ChatManager(repo, fake=True, fake_delay=0)
    try:
        cid = mgr.start("haiku", "note this")
        wait_for(lambda: view(mgr, cid).state == "waiting")
        assert (repo / "notes.md").read_text(encoding="utf-8") == "- note this\n"
        assert git(repo, "status", "--porcelain", "--", "notes.md") == ""
        assert len(view(mgr, cid).commits) == 1
    finally:
        mgr.close()


def test_seeded_start(tmp_path):
    repo = init_repo(tmp_path / "r")
    save_state(
        run_dir(repo, "r1"),
        RunState(
            run_id="r1",
            plan_path="plan.json",
            stage="s1",
            tasks={"t1": TaskState(task_id="t1", status="failed")},
        ),
    )
    mgr, sessions = make(repo)
    try:
        cid = mgr.start("sonnet", "fix it", run_id="r1", task_id="t1")
        wait_for(lambda: sessions and sessions[0].sent)
        first = sessions[0].sent[0]
        assert first.startswith("You are picking up task t1")
        assert first.endswith("\n\nThe user says:\nfix it")
        wait_for(lambda: view(mgr, cid).state == "waiting")
        assert [e.text for e in view(mgr, cid).transcript if e.role == "user"] == ["fix it"]
        started = read_chat_events(repo)[0]
        assert started.type == "chat_started"
        assert started.payload["run_id"] == "r1" and started.payload["task_id"] == "t1"
    finally:
        mgr.close()
