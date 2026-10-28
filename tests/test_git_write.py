import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_cmd import NoRepoError
from harness.ui.git_write import commit, stage, unstage
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


def status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def unborn(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    return path


def test_stage_new_file(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "new.txt").write_text("x\n")
    stage(repo, ["new.txt"])
    assert status(repo) == "A  new.txt\n"


def test_stage_modified_file(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed\n")
    stage(repo, ["README.md"])
    assert status(repo) == "M  README.md\n"


def test_stage_deleted_file(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").unlink()
    stage(repo, ["README.md"])
    assert status(repo) == "D  README.md\n"


def test_stage_dot_skips_harness(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "x").write_text("x\n")
    (repo / "b.txt").write_text("b\n")
    stage(repo, ["."])
    assert status(repo) == "A  b.txt\n?? .harness/\n"


def test_stage_dot_when_harness_is_ignored(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".harness/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore harness")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "x").write_text("x\n")
    (repo / "b.txt").write_text("b\n")
    stage(repo, ["."])
    assert status(repo) == "A  b.txt\n"


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        ([], "choose at least one file"),
        (["../x"], "bad path: ../x"),
        ([".harness/x"], "bad path: .harness/x"),
        ([":(top)a"], "bad path: :(top)a"),
    ],
)
def test_stage_refusals(tmp_path, paths, message):
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitOpError) as info:
        stage(repo, paths)
    assert str(info.value) == message


def test_stage_missing_file_raises(tmp_path):
    repo = init_repo(tmp_path / "repo")
    before = status(repo)
    with pytest.raises(GitOpError):
        stage(repo, ["missing"])
    assert status(repo) == before


def test_unstage_after_commit(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed\n")
    stage(repo, ["README.md"])
    unstage(repo, ["README.md"])
    assert status(repo) == " M README.md\n"


def test_unstage_before_first_commit(tmp_path):
    repo = unborn(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    stage(repo, ["a.txt"])
    assert status(repo) == "A  a.txt\n"
    unstage(repo, ["a.txt"])
    assert status(repo) == "?? a.txt\n"


def test_commit_returns_short_sha(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    stage(repo, ["a.txt"])
    sha = commit(repo, "add a")
    assert sha == git(repo, "rev-parse", "--short", "HEAD")
    assert git(repo, "log", "-1", "--format=%s") == "add a"


def test_commit_message_starting_with_dash(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    stage(repo, ["a.txt"])
    commit(repo, "-dash")
    assert git(repo, "log", "-1", "--format=%s") == "-dash"


def test_commit_nothing_staged(tmp_path):
    repo = init_repo(tmp_path / "repo")
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(GitOpError) as info:
        commit(repo, "msg")
    assert str(info.value) == "nothing staged to commit"
    assert git(repo, "rev-parse", "HEAD") == head


def test_commit_blank_message(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    stage(repo, ["a.txt"])
    with pytest.raises(GitOpError) as info:
        commit(repo, "   ")
    assert str(info.value) == "write a commit message"


def test_first_commit_in_unborn_repo(tmp_path):
    repo = unborn(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n")
    stage(repo, ["a.txt"])
    sha = commit(repo, "first")
    assert sha == git(repo, "rev-parse", "--short", "HEAD")


@pytest.mark.parametrize(
    "action",
    [
        lambda p: stage(p, ["a"]),
        lambda p: unstage(p, ["a"]),
        lambda p: commit(p, "m"),
    ],
)
def test_not_a_repo(tmp_path, action):
    folder = tmp_path / "plain"
    folder.mkdir()
    with pytest.raises(NoRepoError) as info:
        action(folder)
    assert str(info.value) == f"not a git repository: {folder}"
