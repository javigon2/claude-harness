import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui import git_write
from harness.ui.git_write import (
    GitOpError,
    create_branch,
    delete_branch,
    merge_branch,
    switch_branch,
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


def test_create_from_head(tmp_path):
    repo = init_repo(tmp_path / "r")
    create_branch(repo, "feat", "HEAD")
    assert git(repo, "branch", "--show-current") == "feat"
    assert git(repo, "rev-parse", "feat") == git(repo, "rev-parse", "main")


def test_create_from_earlier_sha(tmp_path):
    repo = init_repo(tmp_path / "r")
    first = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "a", "a")
    create_branch(repo, "feat", first)
    assert git(repo, "rev-parse", "HEAD") == first


def test_create_keeps_changes(tmp_path):
    repo = init_repo(tmp_path / "r")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    create_branch(repo, "feat", "HEAD")
    assert git(repo, "status", "--porcelain") == "M README.md"


def test_create_errors(tmp_path):
    repo = init_repo(tmp_path / "r")
    assert err(create_branch, repo, "feat", "nope") == "unknown commit or branch: nope"
    git(repo, "branch", "feat")
    assert err(create_branch, repo, "feat", "HEAD") == "a branch named feat already exists"
    assert err(create_branch, repo, "a..b", "HEAD") == "not a valid branch name: a..b"
    assert err(create_branch, repo, "harness/x", "HEAD") == "harness/ branches belong to runs"


def test_switch(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "branch", "feat")
    switch_branch(repo, "feat")
    assert git(repo, "branch", "--show-current") == "feat"
    assert err(switch_branch, repo, "ghost") == "no local branch named ghost"
    git(repo, "branch", "harness/x")
    assert err(switch_branch, repo, "harness/x") == "harness/ branches belong to runs"


def test_switch_refused_by_local_change(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "switch", "-c", "feat")
    commit_file(repo, "README.md", "feat\n")
    git(repo, "switch", "main")
    (repo / "README.md").write_text("local\n", encoding="utf-8")
    with pytest.raises(GitOpError):
        switch_branch(repo, "feat")
    assert git(repo, "branch", "--show-current") == "main"


def feat_with_commit(repo: Path) -> None:
    git(repo, "switch", "-c", "feat")
    commit_file(repo, "f.txt", "f\n", "feat work")
    git(repo, "switch", "main")


def test_merge_local(tmp_path):
    repo = init_repo(tmp_path / "r")
    feat_with_commit(repo)
    sha = merge_branch(repo, "feat")
    assert sha == git(repo, "rev-parse", "--short", "HEAD")
    assert len(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert git(repo, "log", "-1", "--format=%s") == "Merge branch 'feat'"


def test_merge_remote_tracking(tmp_path):
    repo = init_repo(tmp_path / "r")
    feat_with_commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/up", git(repo, "rev-parse", "feat"))
    merge_branch(repo, "origin/up")
    assert git(repo, "log", "-1", "--format=%s") == "Merge remote-tracking branch 'origin/up'"


def test_merge_guards(tmp_path):
    repo = init_repo(tmp_path / "r")
    assert err(merge_branch, repo, "ghost") == "unknown branch: ghost"
    feat_with_commit(repo)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    assert err(merge_branch, repo, "feat") == "commit or stash your changes first"
    git(repo, "checkout", "--", "README.md")
    (repo / "untracked.txt").write_text("u", encoding="utf-8")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "x").write_text("x", encoding="utf-8")
    merge_branch(repo, "feat")


def test_merge_detached(tmp_path):
    repo = init_repo(tmp_path / "r")
    feat_with_commit(repo)
    git(repo, "switch", "--detach", "HEAD")
    assert err(merge_branch, repo, "feat") == "check out a branch first"


def conflict_setup(repo: Path) -> None:
    git(repo, "switch", "-c", "feat")
    commit_file(repo, "README.md", "feat\n")
    git(repo, "switch", "main")
    commit_file(repo, "README.md", "main\n")


def test_merge_conflict_aborted(tmp_path):
    repo = init_repo(tmp_path / "r")
    conflict_setup(repo)
    head = git(repo, "rev-parse", "HEAD")
    msg = err(merge_branch, repo, "feat")
    assert msg == "feat does not merge cleanly: conflict in README.md; nothing was changed"
    assert git(repo, "rev-parse", "HEAD") == head
    probe = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo, capture_output=True
    )
    assert probe.returncode != 0
    assert git(repo, "status", "--porcelain") == ""


def test_merge_abort_failure(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "r")
    conflict_setup(repo)
    real = git_write.run

    def fake(*args, **kwargs):
        if args[1:3] == ("merge", "--abort"):
            raise GitOpError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(git_write, "run", fake)
    assert (
        err(merge_branch, repo, "feat")
        == "feat does not merge cleanly and git merge --abort failed: boom"
    )


def test_delete(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "branch", "feat")
    delete_branch(repo, "feat")
    assert git(repo, "branch", "--list", "feat") == ""
    assert err(delete_branch, repo, "main") == "cannot delete the current branch"
    git(repo, "branch", "harness/x")
    assert err(delete_branch, repo, "harness/x") == "harness/ branches belong to runs"
    assert err(delete_branch, repo, "ghost") == "no local branch named ghost"


def test_delete_unmerged(tmp_path):
    repo = init_repo(tmp_path / "r")
    feat_with_commit(repo)
    assert err(delete_branch, repo, "feat") == "feat is not fully merged; merge it first"
    assert git(repo, "branch", "--list", "feat") != ""
