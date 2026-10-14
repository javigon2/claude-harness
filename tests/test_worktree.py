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
    assert path == repo / ".harness" / "worktrees" / "r1" / "t1"
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


def test_cleanup_keeps_other_runs(tmp_path: Path):
    repo = _repo(tmp_path)
    m1 = WorktreeManager(repo, "r1")
    m2 = WorktreeManager(repo, "r2")
    m10 = WorktreeManager(repo, "r10")
    wa = m1.create("a")
    wb = m2.create("b")
    wc = m10.create("c")
    m1.cleanup()
    assert not wa.exists()
    assert wb.exists()
    assert wc.exists()
    assert _out(repo, "branch", "--list", "harness/r1/*") == ""
    assert "harness/r2/b" in _out(repo, "branch", "--list", "harness/r2/*")
    assert "harness/r10/c" in _out(repo, "branch", "--list", "harness/r10/*")
    listing = _out(repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 3
    m2.cleanup()
    assert not wb.exists()
    assert wc.exists()


def test_cleanup_keeps_detached_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    det = repo / ".harness" / "worktrees" / "det"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(det), "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    mgr = WorktreeManager(repo, "r1")
    wa = mgr.create("a")
    mgr.cleanup()
    assert det.exists()
    assert not wa.exists()


def test_discard_after_create(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    mgr.discard("t1")
    assert not wt.exists()
    assert _out(repo, "branch", "--list", "harness/r1/t1") == ""
    listing = _out(repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 1


def test_discard_with_uncommitted_changes(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    (wt / "a.txt").write_text("a\n")
    (wt / "README.md").write_text("changed\n")
    mgr.discard("t1")
    assert not wt.exists()
    assert _out(repo, "branch", "--list", "harness/r1/t1") == ""


def test_discard_missing_task_does_not_raise(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    mgr.discard("never")
    assert not mgr.path("never").exists()


def test_create_after_discard(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    mgr.create("t1")
    mgr.discard("t1")
    wt = mgr.create("t1")
    assert wt.is_dir()
    assert "harness/r1/t1" in _out(repo, "branch", "--list", "harness/r1/t1")


def test_discard_plain_directory(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    path = mgr.path("t1")
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("junk\n")
    mgr.discard("t1")
    assert not path.exists()


def test_same_task_id_in_two_runs(tmp_path: Path):
    repo = _repo(tmp_path)
    r1 = WorktreeManager(repo, "r1")
    r2 = WorktreeManager(repo, "r2")
    p1 = r1.create("t")
    p2 = r2.create("t")
    assert p1 != p2
    assert p1.is_dir()
    assert p2.is_dir()
    r1.cleanup()
    assert not p1.exists()
    assert p2.is_dir()
    assert "harness/r2/t" in _out(repo, "branch", "--list", "harness/r2/t")
    r2.discard("t")
    assert not p2.exists()
    assert _out(repo, "branch", "--list", "harness/r2/t") == ""
