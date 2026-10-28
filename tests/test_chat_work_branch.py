import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.models import Usage
from harness.planner import PlannerTurn
from harness.ui.chat import ChatManager
from harness.ui.work_branch import base_of

CLOCK = 1767225600.0
LABEL = "chat-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(CLOCK))


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


def run_turn(mgr, text="go", chat_id=None):
    if chat_id is None:
        chat_id = mgr.start("haiku", text)
    else:
        mgr.message(chat_id, text)
    wait_for(lambda: view(mgr, chat_id).state == "waiting")
    return chat_id


@pytest.fixture
def managers():
    made = []
    yield made
    for mgr in made:
        mgr.close()


def new_mgr(tmp_path, live, managers):
    repo = init_repo(tmp_path / "repo")
    mgr, _ = make(repo, clock=lambda: CLOCK, live_fn=live)
    managers.append(mgr)
    return repo, mgr


def test_chat_commits_on_new_work_branch(tmp_path, managers):
    repo, mgr = new_mgr(tmp_path, lambda: None, managers)
    main_sha = git(repo, "rev-parse", "main")
    run_turn(mgr)
    assert git(repo, "branch", "--show-current") == f"work/{LABEL}"
    assert base_of(repo, f"work/{LABEL}") == "main"
    assert git(repo, "log", "-1", "--format=%s") == "chat haiku: go"
    assert git(repo, "rev-parse", "main") == main_sha


def test_second_message_same_branch(tmp_path, managers):
    repo, mgr = new_mgr(tmp_path, lambda: None, managers)
    cid = run_turn(mgr)
    run_turn(mgr, "more", cid)
    wait_for(lambda: git(repo, "ls-files", "2.txt"))
    assert len(git(repo, "branch", "--list", "work/*").splitlines()) == 1


def test_existing_work_branch_kept(tmp_path, managers):
    repo, mgr = new_mgr(tmp_path, lambda: None, managers)
    git(repo, "switch", "-q", "-c", "work/x")
    run_turn(mgr)
    assert git(repo, "branch", "--show-current") == "work/x"
    assert git(repo, "log", "-1", "--format=%s") == "chat haiku: go"
    assert git(repo, "branch", "--list", "work/*").split() == ["*", "work/x"]


def test_live_run_on_main_not_committed(tmp_path, managers):
    repo, mgr = new_mgr(tmp_path, lambda: "r1", managers)
    sha = git(repo, "rev-parse", "HEAD")
    cid = run_turn(mgr)
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == sha
    assert git(repo, "ls-files", "1.txt") == ""
    assert view(mgr, cid).error == "a run is active on main; chat changes are not committed"


def test_live_run_on_work_branch_commits(tmp_path, managers):
    repo, mgr = new_mgr(tmp_path, lambda: "r1", managers)
    git(repo, "switch", "-q", "-c", "work/x")
    run_turn(mgr)
    assert git(repo, "branch", "--show-current") == "work/x"
    assert git(repo, "log", "-1", "--format=%s") == "chat haiku: go"
