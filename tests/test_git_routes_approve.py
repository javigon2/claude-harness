import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_routes import BadGitBody, check_git_body, git_post


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


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo")


def work_branch_with_commit(repo: Path) -> None:
    """Cut work/x from main, record main as its base, and commit a new file on it."""
    git(repo, "switch", "-c", "work/x")
    git(repo, "config", "branch.work/x.harnessbase", "main")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-m", "work")


def test_check_body_accepts_name():
    assert check_git_body("approve", {"name": "work/x"}) == {"name": "work/x"}


def test_check_body_rejects_bad_shapes():
    with pytest.raises(BadGitBody, match="^send exactly: name$"):
        check_git_body("approve", {})
    with pytest.raises(BadGitBody) as err:
        check_git_body("approve", {"name": ""})
    assert str(err.value) == "name must be a non-empty string of at most 200 characters"


def test_approve_merges_into_base(repo):
    work_branch_with_commit(repo)
    git(repo, "switch", "main")
    assert git_post(repo, "approve", {"name": "work/x"}, None) == (
        200,
        {"ok": True, "sha": git(repo, "rev-parse", "--short", "HEAD")},
    )
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "new.txt").exists()


def test_approve_refused_while_busy(repo):
    work_branch_with_commit(repo)
    git(repo, "switch", "main")
    main_before = git(repo, "rev-parse", "main")
    assert git_post(repo, "approve", {"name": "work/x"}, "a chat is working") == (
        409,
        {"error": "a chat is working"},
    )
    assert git(repo, "rev-parse", "main") == main_before
    assert git(repo, "branch", "--show-current") == "main"


def test_approve_refuses_non_work_branch(repo):
    assert git_post(repo, "approve", {"name": "main"}, None) == (
        409,
        {"error": "main is not a work branch"},
    )
