import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_cmd import NoRepoError
from harness.ui.work_branch import (
    GitOpError,
    approve,
    base_of,
    current_branch,
    ensure_work_branch,
)


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_file(repo: Path, name: str, text: str, msg: str = "c") -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg)


def err(fn, *args) -> str:
    with pytest.raises(GitOpError) as info:
        fn(*args)
    return str(info.value)


def test_ensure_creates_from_main(tmp_path):
    repo = init_repo(tmp_path / "r")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    assert ensure_work_branch(repo, "r1") == "work/r1"
    assert git(repo, "branch", "--show-current") == "work/r1"
    assert base_of(repo, "work/r1") == "main"
    assert (repo / "README.md").read_text(encoding="utf-8") == "edited\n"


def test_ensure_keeps_work_branch(tmp_path):
    repo = init_repo(tmp_path / "r")
    ensure_work_branch(repo, "r1")
    assert ensure_work_branch(repo, "r2") == "work/r1"
    assert git(repo, "branch", "--list", "work/r2") == ""


def test_ensure_suffix(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "branch", "work/r1")
    git(repo, "branch", "work/r1-2")
    assert ensure_work_branch(repo, "r1") == "work/r1-3"


def test_ensure_errors_and_helpers(tmp_path):
    repo = init_repo(tmp_path / "r")
    assert current_branch(repo) == "main"
    assert base_of(repo, "nope") is None
    assert err(ensure_work_branch, repo, "a..b") == "not a valid branch name: work/a..b"
    git(repo, "checkout", "-q", "--detach")
    assert current_branch(repo) is None
    assert err(ensure_work_branch, repo, "x") == "check out a branch first"

    unborn = tmp_path / "u"
    unborn.mkdir()
    git(unborn, "init", "-b", "main")
    assert (
        err(ensure_work_branch, unborn, "x") == "the project has no commit yet; initialize it first"
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NoRepoError):
        ensure_work_branch(plain, "x")


def make_work(repo: Path) -> None:
    ensure_work_branch(repo, "r1")
    commit_file(repo, "a.txt", "a\n", "work")


def test_approve_from_work(tmp_path):
    repo = init_repo(tmp_path / "r")
    make_work(repo)
    sha = approve(repo, "work/r1")
    assert sha == git(repo, "rev-parse", "--short", "HEAD")
    assert git(repo, "branch", "--show-current") == "main"
    assert len(git(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert git(repo, "log", "-1", "--format=%s") == "Merge work/r1 into main"
    assert (
        err(approve, repo, "work/r1")
        == "nothing to approve: work/r1 has no commits that main lacks"
    )


def test_approve_from_main(tmp_path):
    repo = init_repo(tmp_path / "r")
    make_work(repo)
    git(repo, "switch", "-q", "main")
    sha = approve(repo, "work/r1")
    assert sha == git(repo, "rev-parse", "--short", "HEAD")
    assert git(repo, "branch", "--show-current") == "main"
    assert len(git(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert git(repo, "log", "-1", "--format=%s") == "Merge work/r1 into main"


def test_approve_refusals(tmp_path):
    repo = init_repo(tmp_path / "r")
    assert err(approve, repo, "main") == "main is not a work branch"
    assert err(approve, repo, "work/none") == "work/none is not a work branch"
    git(repo, "branch", "work/plain")
    assert err(approve, repo, "work/plain") == "work/plain has no base branch recorded"
    git(repo, "branch", "work/x")
    git(repo, "config", "branch.work/x.harnessbase", "gone")
    assert err(approve, repo, "work/x") == "no local branch named gone"

    make_work(repo)
    git(repo, "checkout", "-q", "--detach")
    assert err(approve, repo, "work/r1") == "check out a branch first"
    git(repo, "switch", "-q", "main")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    assert err(approve, repo, "work/r1") == "commit or stash your changes first"


def test_approve_conflict(tmp_path):
    repo = init_repo(tmp_path / "r")
    ensure_work_branch(repo, "r1")
    commit_file(repo, "README.md", "work side\n")
    git(repo, "switch", "-q", "main")
    commit_file(repo, "README.md", "main side\n")
    before = git(repo, "rev-parse", "main")
    git(repo, "switch", "-q", "work/r1")
    msg = err(approve, repo, "work/r1")
    assert msg == (
        "work/r1 does not merge cleanly into main: conflict in README.md; nothing was changed"
    )
    assert git(repo, "branch", "--show-current") == "work/r1"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "main") == before
