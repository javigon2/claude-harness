import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.chat_git import changed_paths, chat_commit_message, commit_paths, dirty_snapshot
from harness.worktree import WorktreeError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_snapshot_modified_and_untracked(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "new.txt").write_text("new\n")
    snap = dirty_snapshot(repo)
    assert set(snap) == {"README.md", "sub/new.txt"}
    assert all(v is not None and len(v) == 64 for v in snap.values())


def test_snapshot_deleted(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").unlink()
    assert dirty_snapshot(repo) == {"README.md": None}


def test_turn_commits_only_its_changes(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "keep.txt").write_text("keep\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "keep")
    (repo / "user.txt").write_text("mine\n")
    before = dirty_snapshot(repo)
    (repo / "agent.txt").write_text("agent\n")
    (repo / "README.md").write_text("edited\n")
    (repo / "keep.txt").unlink()
    paths = changed_paths(before, dirty_snapshot(repo))
    assert paths == ["README.md", "agent.txt", "keep.txt"]
    sha = commit_paths(repo, paths, "chat haiku: do it")
    assert sha == _git(repo, "rev-parse", "--short", "HEAD").strip()
    assert _git(repo, "log", "-1", "--format=%s").strip() == "chat haiku: do it"
    shown = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(shown) == ["README.md", "agent.txt", "keep.txt"]
    assert _git(repo, "status", "--porcelain") == "?? user.txt\n"


def test_moved_file(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    before = dirty_snapshot(repo)
    (repo / "README.md").rename(repo / "MOVED.md")
    paths = changed_paths(before, dirty_snapshot(repo))
    assert paths == sorted(["MOVED.md", "README.md"])
    assert commit_paths(repo, paths, "move") is not None
    assert _git(repo, "status", "--porcelain") == ""


def test_already_dirty_edited_again(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").write_text("one\n")
    before = dirty_snapshot(repo)
    (repo / "README.md").write_text("two\n")
    assert changed_paths(before, dirty_snapshot(repo)) == ["README.md"]


def test_empty_paths_and_no_change(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    assert commit_paths(repo, [], "x") is None
    s = dirty_snapshot(repo)
    assert changed_paths(s, s) == []


def test_not_a_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError):
        dirty_snapshot(plain)
    with pytest.raises(WorktreeError):
        commit_paths(plain, ["a.txt"], "x")


def test_chat_commit_message():
    assert chat_commit_message("haiku", "\n  fix the typo  \nmore") == "chat haiku: fix the typo"
    assert chat_commit_message("sonnet", "x" * 80) == "chat sonnet: " + "x" * 60
    assert chat_commit_message("haiku", "  \n ") == "chat haiku: changes"
