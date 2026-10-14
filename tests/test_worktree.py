import subprocess
from pathlib import Path

import pytest

from harness.worktree import WorktreeError, WorktreeManager


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("line one\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def test_create(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    path = mgr.create("t1")
    assert path == repo / ".harness" / "worktrees" / "t1"
    assert path.is_dir()
    assert "harness/r1/t1" in _out(repo, "branch", "--list", "harness/r1/t1")
    assert _out(repo, "status", "--porcelain") == ""
    with pytest.raises(WorktreeError):
        mgr.create("t1")


def test_commit(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    (wt / "a.txt").write_text("a\n")
    assert mgr.commit("t1", "add a") is True
    assert mgr.commit("t1", "nothing") is False


def test_merge_simple(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    (wt / "a.txt").write_text("a\n")
    mgr.commit("t1", "add a")
    assert mgr.merge("t1") == "merged"
    assert (repo / "a.txt").exists()


def test_merge_after_main_advances(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    (repo / "other.txt").write_text("o\n")
    _out(repo, "add", "-A")
    _out(repo, "commit", "-m", "other")
    (wt / "b.txt").write_text("b\n")
    mgr.commit("t1", "add b")
    assert mgr.merge("t1") == "merged"
    assert (repo / "b.txt").exists()
    assert (repo / "other.txt").exists()


def test_merge_uncommitted_raises(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    (wt / "a.txt").write_text("a\n")
    with pytest.raises(WorktreeError, match="uncommitted changes in t1"):
        mgr.merge("t1")


def test_merge_conflict(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    w1 = mgr.create("t1")
    w2 = mgr.create("t2")
    (w1 / "README.md").write_text("from t1\n")
    (w2 / "README.md").write_text("from t2\n")
    mgr.commit("t1", "t1 change")
    mgr.commit("t2", "t2 change")
    assert mgr.merge("t1") == "merged"
    assert mgr.merge("t2") == "conflict"
    assert w2.is_dir()
    assert "rebase" not in _out(w2, "status")
    assert (repo / "README.md").read_text() == "from t1\n"


def test_cleanup(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    w1 = mgr.create("t1")
    w2 = mgr.create("t2")
    (w1 / "a.txt").write_text("a\n")
    mgr.commit("t1", "add a")
    assert mgr.merge("t1") == "merged"
    mgr.cleanup()
    assert not w1.exists()
    assert not w2.exists()
    assert _out(repo, "branch", "--list", "harness/r1/*") == ""
    listing = _out(repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 1
