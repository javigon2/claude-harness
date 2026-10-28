"""The POST /api/git/* routes of a server: the body, token and project checks, and the
409 answers while a run is active (or, with a Host, while no project is open)."""

import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo
from test_ui_office_server import Site
from test_ui_project_server import HostSite

from harness.ui.host import NO_PROJECT
from harness.ui.office import Office


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


@pytest.fixture
def site(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    s = Site(repo, Office(repo, fake=True, fake_delay=0))
    yield s
    s.stop()


@pytest.fixture
def host_site(tmp_path: Path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    s = HostSite(cwd, tmp_path / "cfg" / "recent.json")
    yield s
    s.stop()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_stage_then_commit_then_nothing_left_to_commit(site):
    (site.repo / "a.txt").write_text("a\n")
    status, _, out = site.post("/api/git/stage", {"paths": ["a.txt"]})
    assert status == 200
    assert out == {"ok": True}

    status, _, out = site.post("/api/git/commit", {"message": "add a"})
    assert status == 200
    assert out["ok"] is True
    assert out["sha"]

    status, _, out = site.post("/api/git/commit", {"message": "add a"})
    assert status == 409
    assert out == {"error": "nothing staged to commit"}


def test_bad_requests_are_refused(site):
    status, _, out = site.post("/api/git/commit", {"msg": "x"})
    assert status == 400
    assert out == {"error": "send exactly: message"}

    status, _, _ = site.post("/api/git/nope", {})
    assert status == 404

    status, _, _ = site.post("/api/git/commit", {"message": "m"}, token="bad")
    assert status == 403

    status, _, out = site.post("/api/git/commit", raw=b"{")
    assert status == 400
    assert out == {"error": "bad JSON"}


def test_writes_answer_409_while_a_run_is_active(site, monkeypatch):
    monkeypatch.setattr("harness.ui.server.live_run", lambda repo: "20261028-000000")

    status, _, out = site.post("/api/git/branch", {"name": "feat", "start": "HEAD"})
    assert status == 409
    assert out == {"error": "a run is active (20261028-000000); wait for it to finish"}
    assert _git(site.repo, "branch", "--list", "feat") == ""

    # fetch is not busy-guarded, so it reaches the git operation
    status, _, out = site.post("/api/git/fetch", {"remote": "origin"})
    assert status == 409
    assert out == {"error": "unknown remote: origin"}


def test_no_project_open_answers_409(host_site):
    status, _, out = host_site.post("/api/git/commit", {"message": "m"})
    assert status == 409
    assert out == {"error": NO_PROJECT}
