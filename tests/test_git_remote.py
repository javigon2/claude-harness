import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui import git_cmd, git_remote
from harness.ui.git_cmd import NoRepoError
from harness.ui.git_remote import fetch, push
from harness.ui.gitops import GitOpError


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
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True, capture_output=True
    )
    repo = init_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", str(bare))
    return repo, bare


def diverge(tmp_path: Path, repo: Path, bare: Path) -> Path:
    """Push a commit to the bare repo from a second clone, commit in `repo`."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "O")
    (other / "o.txt").write_text("o\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "other")
    git(other, "push", "-q", "origin", "main")
    (repo / "r.txt").write_text("r\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mine")
    return other


def test_push(origin):
    repo, bare = origin
    assert "main -> main" in push(repo, "origin", "main")
    assert git(bare, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert push(repo, "origin", "main") == "Everything up-to-date"


def test_rejected_push(origin, tmp_path):
    repo, bare = origin
    push(repo, "origin", "main")
    other = diverge(tmp_path, repo, bare)
    with pytest.raises(GitOpError) as info:
        push(repo, "origin", "main")
    assert (
        str(info.value) == "origin has commits that main lacks; fetch and merge origin/main first"
    )
    assert git(bare, "rev-parse", "main") == git(other, "rev-parse", "HEAD")


def test_fetch(origin, tmp_path):
    repo, bare = origin
    push(repo, "origin", "main")
    other = diverge(tmp_path, repo, bare)
    assert fetch(repo, "origin").endswith("-> origin/main")
    assert git(repo, "rev-parse", "origin/main") == git(other, "rev-parse", "HEAD")
    assert fetch(repo, "origin") == "origin is up to date"


def test_fetch_prunes(origin):
    repo, bare = origin
    git(repo, "branch", "feat")
    push(repo, "origin", "feat")
    fetch(repo, "origin")
    git(repo, "rev-parse", "--verify", "refs/remotes/origin/feat")
    git(bare, "branch", "-D", "feat")
    fetch(repo, "origin")
    gone = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "refs/remotes/origin/feat"],
        cwd=repo,
        capture_output=True,
    )
    assert gone.returncode != 0


def test_refusals(origin, tmp_path):
    repo, _ = origin
    with pytest.raises(GitOpError) as info:
        fetch(repo, "nope")
    assert str(info.value) == "unknown remote: nope"
    with pytest.raises(GitOpError) as info:
        push(repo, "nope", "main")
    assert str(info.value) == "unknown remote: nope"
    with pytest.raises(GitOpError) as info:
        push(repo, "origin", "ghost")
    assert str(info.value) == "no local branch named ghost"
    with pytest.raises(GitOpError) as info:
        push(repo, "origin", "-x")
    assert str(info.value) == "not a valid branch name: -x"
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NoRepoError) as info:
        fetch(plain, "origin")
    assert str(info.value) == f"not a git repository: {plain}"


def test_network_timeout_passed(origin, monkeypatch):
    repo, _ = origin
    seen = []
    real = git_cmd.run

    def wrapper(*args, **kwargs):
        if args[1] in ("fetch", "push"):
            seen.append(kwargs.get("timeout"))
        return real(*args, **kwargs)

    monkeypatch.setattr(git_remote, "run", wrapper)
    push(repo, "origin", "main")
    fetch(repo, "origin")
    assert seen == [git_cmd.NETWORK_TIMEOUT_S, git_cmd.NETWORK_TIMEOUT_S]
