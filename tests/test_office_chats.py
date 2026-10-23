import asyncio
import threading
import time
from pathlib import Path

import pytest
from git_helpers import init_repo
from ui_fixtures import write_running_run

from harness.planner import PlannerTurn
from harness.ui.chat import ChatConflict, chats_from_log
from harness.ui.office import STAGING_BLOCK, Office, OfficeConflict, office_view_from_log


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def state_is(office: Office, state: str):
    return lambda: office.view().state == state


class FakePopen:
    def __init__(self, argv, **kwargs) -> None:
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4242
        self.code: int | None = None

    def poll(self):
        return self.code


class Launches:
    def __init__(self) -> None:
        self.made: list[FakePopen] = []

    def __call__(self, argv, **kwargs) -> FakePopen:
        p = FakePopen(argv, **kwargs)
        self.made.append(p)
        return p


def _to_ready(office: Office) -> None:
    office.start("a demo")
    wait_for(state_is(office, "asking"))
    office.message("answers")
    wait_for(state_is(office, "ready"))


class GatedChat:
    def __init__(self, repo: Path, gate: threading.Event) -> None:
        self.repo, self.gate = repo, gate

    async def send(self, text: str) -> PlannerTurn:
        await asyncio.to_thread(self.gate.wait, 5)
        return PlannerTurn(text="ok")

    async def close(self) -> None:
        pass


@pytest.fixture
def make_office(tmp_path):
    init_repo(tmp_path)
    made: list[Office] = []

    def make(**kwargs) -> Office:
        kwargs.setdefault("fake", True)
        kwargs.setdefault("fake_delay", 0)
        office = Office(tmp_path, **kwargs)
        made.append(office)
        return office

    yield make
    for office in made:
        office.close()


def test_view_lists_chats(make_office):
    office = make_office()
    assert office.view().chats == []
    office.chats.start("haiku", "note")
    chats = office.view().chats
    assert len(chats) == 1 and chats[0].tier == "haiku"
    wait_for(lambda: office.view().chats[0].state == "waiting")


def test_chat_refused_during_run(make_office, tmp_path):
    write_running_run(tmp_path)
    office = make_office()
    with pytest.raises(ChatConflict, match=r"a run is active \(running1\); wait for it to finish"):
        office.chats.start("haiku", "x")


def test_chat_refused_while_staging(make_office):
    office = make_office(fake_delay=0.3, launcher=Launches())
    _to_ready(office)
    office.implement()
    wait_for(state_is(office, "staging"))
    with pytest.raises(ChatConflict, match=STAGING_BLOCK):
        office.chats.start("haiku", "x")


def test_implement_refused_while_chat_works(make_office):
    gate = threading.Event()
    launches = Launches()
    office = make_office(
        launcher=launches, chat_session_factory=lambda repo, tier, progress: GatedChat(repo, gate)
    )
    _to_ready(office)
    office.chats.start("haiku", "x")
    wait_for(office.chats.working)
    with pytest.raises(OfficeConflict, match="an agent chat is working; wait for its turn to end"):
        office.implement()
    gate.set()
    wait_for(lambda: not office.chats.working())
    office.implement()
    wait_for(lambda: launches.made)


def test_view_from_log_lists_chats(make_office, tmp_path):
    office = make_office()
    office.chats.start("haiku", "note")
    wait_for(lambda: office.view().chats[0].state == "waiting")
    view = office_view_from_log(tmp_path)
    assert len(view.chats) == 1


def test_close_closes_chats(make_office, tmp_path):
    office = make_office()
    office.chats.start("haiku", "note")
    wait_for(lambda: office.view().chats[0].state == "waiting")
    office.close()
    assert [c.state for c in chats_from_log(tmp_path)] == ["closed"]
