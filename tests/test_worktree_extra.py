import subprocess
from pathlib import Path

from harness.worktree import WorktreeManager, ensure_excluded


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


def _make_dev(repo: Path) -> None:
    _out(repo, "checkout", "-b", "dev")
    (repo / "dev.txt").write_text("dev\n")
    _out(repo, "add", "-A")
    _out(repo, "commit", "-m", "dev")
    _out(repo, "checkout", "main")


def _exclude_lines(repo: Path) -> list[str]:
    common = Path(_out(repo, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = repo / common
    return (common / "info" / "exclude").read_text().splitlines()


def test_branch_and_path_naming(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    assert mgr.branch("t1") == "harness/r1/t1"
    assert mgr.path("t1") == repo / ".harness" / "worktrees" / "r1" / "t1"


def test_create_with_custom_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _make_dev(repo)
    mgr = WorktreeManager(repo, "r1")
    from_dev = mgr.create("a", base="dev")
    from_main = mgr.create("b")
    assert (from_dev / "dev.txt").exists()
    assert not (from_main / "dev.txt").exists()


def test_default_base_is_integration_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _make_dev(repo)
    wt = WorktreeManager(repo, "r2", integration_branch="dev").create("x")
    assert (wt / "dev.txt").exists()


def test_commit_message_and_noop(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    assert mgr.commit("t1", "nothing") is False
    (wt / "a.txt").write_text("a\n")
    assert mgr.commit("t1", "my message") is True
    assert _out(wt, "log", "-1", "--format=%s").strip() == "my message"


def test_merge_conflict_leaves_clean_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    (wt / "README.md").write_text("task change\n")
    assert mgr.commit("t1", "task") is True
    (repo / "README.md").write_text("main change\n")
    _out(repo, "add", "-A")
    _out(repo, "commit", "-m", "main")
    before = _out(repo, "rev-parse", mgr.branch("t1"))
    assert mgr.merge("t1") == "conflict"
    assert _out(repo, "rev-parse", mgr.branch("t1")) == before
    assert _out(wt, "status", "--porcelain") == ""


def test_ensure_excluded_idempotent_from_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    mgr = WorktreeManager(repo, "r1")
    wt = mgr.create("t1")
    ensure_excluded(repo)
    ensure_excluded(wt)
    assert _exclude_lines(repo).count(".harness/") == 1
    assert _out(repo, "status", "--porcelain") == ""


def test_cleanup_empty_run_does_not_raise(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    WorktreeManager(repo, "r1").cleanup()


def test_cleanup_leaves_other_run_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    m1 = WorktreeManager(repo, "r1")
    m2 = WorktreeManager(repo, "r2")
    m1.create("t1")
    m2.create("x")
    m1.cleanup()
    assert _out(repo, "branch", "--list", "harness/r1/t1") == ""
    assert "harness/r2/x" in _out(repo, "branch", "--list", "harness/r2/x")
