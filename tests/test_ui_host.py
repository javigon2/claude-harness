import json
import os
import time
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.host import NO_PROJECT, Host, HostConflict
from harness.ui.office import CHAT_WORKING, LOCK_FILE, Office
from harness.ui.projects import load_recent


def wait_for(check, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


@pytest.fixture
def make_host(tmp_path):
    made: list[Host] = []

    def make(**kwargs) -> Host:
        kwargs.setdefault("port", 8765)
        kwargs.setdefault("cwd", tmp_path)
        kwargs.setdefault("fake", True)
        kwargs.setdefault("fake_delay", 0)
        kwargs.setdefault("recent_path", tmp_path / "cfg" / "recent.json")
        host = Host(**kwargs)
        made.append(host)
        return host

    yield make
    for host in made:
        host.close()


def _folder(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_no_project_before_open(make_host):
    host = make_host()
    assert host.current() == (None, None)
    assert host.busy() is None
    assert host.close() is None
    assert NO_PROJECT == "no project is open; pick a folder first"


def test_open_makes_an_office_and_remembers_the_folder(tmp_path, make_host):
    project = init_repo(tmp_path / "proj")
    host = make_host()
    before = time.time()

    host.open_project(project)

    path, office = host.current()
    assert path == project and isinstance(office, Office)
    assert office.repo == project.resolve()
    assert office.view().mode == "interactive"
    lock = json.loads((project / LOCK_FILE).read_text())
    assert lock["pid"] == os.getpid() and lock["port"] == 8765
    assert load_recent(tmp_path / "cfg" / "recent.json") == [str(project)]
    assert host.started <= time.time() and host.started >= before - 5
    # the repo ignores .harness/ from the start
    assert ".harness/" in (project / ".git" / "info" / "exclude").read_text()


def test_open_a_plain_folder_without_git_still_works(tmp_path, make_host):
    plain = _folder(tmp_path, "plain")
    host = make_host()
    host.open_project(plain)
    assert host.current()[0] == plain


def test_open_a_missing_folder_or_a_file(tmp_path, make_host):
    host = make_host()
    (tmp_path / "f.txt").write_text("x")
    for target in (tmp_path / "missing", tmp_path / "f.txt"):
        with pytest.raises(HostConflict) as err:
            host.open_project(target)
        assert str(err.value) == f"not a folder: {target}"
    assert host.current() == (None, None)


def test_switch_closes_the_old_office_and_orders_recents(tmp_path, make_host):
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    host = make_host()
    host.open_project(a)
    old = host.current()[1]
    host.open_project(b)

    path, office = host.current()
    assert path == b and office is not old
    assert not (a / LOCK_FILE).exists() and (b / LOCK_FILE).exists()
    host.open_project(a)
    assert load_recent(tmp_path / "cfg" / "recent.json") == [str(a), str(b)]


def test_reopening_the_same_folder_works(tmp_path, make_host):
    a = _folder(tmp_path, "a")
    host = make_host()
    host.open_project(a)
    host.open_project(a)
    assert host.current()[0] == a and (a / LOCK_FILE).exists()


def test_switch_is_refused_while_planning(tmp_path, make_host):
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    host = make_host(fake_delay=0.3)
    host.open_project(a)
    path, office = host.current()
    office.start("a demo")
    assert host.busy() == "a planning session is open; finish it first"

    with pytest.raises(HostConflict, match="^a planning session is open; finish it first$"):
        host.open_project(b)
    assert host.current() == (path, office)
    assert not (b / LOCK_FILE).exists()
    with pytest.raises(HostConflict, match="planning session"):
        host.request_restart()
    assert host.restart_requested is False


def test_switch_is_refused_while_a_chat_works(tmp_path, make_host):
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    host = make_host(fake_delay=0.3)
    host.open_project(a)
    _, office = host.current()
    office.chats.start("haiku", "do a thing")
    wait_for(lambda: office.chats.working())

    assert host.busy() == CHAT_WORKING
    with pytest.raises(HostConflict, match=CHAT_WORKING):
        host.open_project(b)
    with pytest.raises(HostConflict, match=CHAT_WORKING):
        host.request_restart()
    wait_for(lambda: host.busy() is None, timeout=10)
    host.open_project(b)  # idle again
    assert host.current()[0] == b


def test_switch_is_refused_while_the_orchestrator_routes(tmp_path, make_host):
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    host = make_host(fake_delay=0.4)
    host.open_project(a)
    _, office = host.current()
    office.orchestrator.message("haiku please fix a typo")
    wait_for(lambda: office.orchestrator.view().state == "routing")

    assert host.busy() == "the orchestrator is routing a message"
    with pytest.raises(HostConflict, match="orchestrator is routing"):
        host.open_project(b)


def test_a_folder_owned_by_another_ui_is_refused_and_the_old_project_stays(tmp_path, make_host):
    a, b = _folder(tmp_path, "a"), _folder(tmp_path, "b")
    (b / ".harness").mkdir()
    (b / LOCK_FILE).write_text(json.dumps({"pid": os.getpid(), "port": 9999}))  # a live owner
    host = make_host()
    host.open_project(a)

    with pytest.raises(HostConflict, match="^Another harness ui .* owns this repo"):
        host.open_project(b)

    path, office = host.current()
    assert path == a and office is not None
    assert (a / LOCK_FILE).exists()  # reopened
    assert load_recent(tmp_path / "cfg" / "recent.json") == [str(a)]


def test_a_stale_lock_is_taken_over(tmp_path, make_host):
    a = _folder(tmp_path, "a")
    (a / ".harness").mkdir()
    (a / LOCK_FILE).write_text(json.dumps({"pid": 2**22 + 777, "port": 1}))
    host = make_host()
    host.open_project(a)
    assert host.current()[0] == a


def test_request_restart_when_idle(tmp_path, make_host):
    host = make_host()
    host.request_restart()  # no project open is idle too
    assert host.restart_requested is True


def test_office_factory_builds_the_offices(tmp_path, make_host):
    seen: list[Path] = []

    def factory(path: Path) -> Office:
        seen.append(path)
        return Office(path, fake=True, fake_delay=0)

    a = _folder(tmp_path, "a")
    host = make_host(office_factory=factory)
    host.open_project(a)
    assert seen == [a]


def test_close_removes_the_lock_and_forgets_the_project(tmp_path, make_host):
    a = _folder(tmp_path, "a")
    host = make_host()
    host.open_project(a)
    assert host.close() is None
    assert not (a / LOCK_FILE).exists()
    assert host.current() == (None, None)
