import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui import git_cmd
from harness.ui.git_cmd import (
    NAME_MAX,
    PATCH_MAX,
    GitOpError,
    NoRepoError,
    cap,
    check_branch_name,
    check_path,
    out,
    require_repo,
    resolve,
    run,
)
from harness.ui.gitops import GitOpError as BaseGitOpError


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


def head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_out_returns_stdout(tmp_path):
    repo = init_repo(tmp_path / "repo")
    assert out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main\n"


def test_run_failure_raises_with_git_message(tmp_path):
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitOpError) as info:
        run(repo, "rev-parse", "--verify", "nope")
    assert str(info.value)


def test_run_accepts_listed_exit_code(tmp_path):
    repo = init_repo(tmp_path / "repo")
    proc = run(repo, "rev-parse", "--verify", "nope", ok=(0, 128))
    assert proc.returncode == 128


def test_run_timeout_message(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(git_cmd.subprocess, "run", fake_run)
    with pytest.raises(GitOpError) as info:
        run(repo, "status", timeout=2.0)
    assert str(info.value) == "git timed out after 2 s"


def test_run_cannot_start_message(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no git")

    monkeypatch.setattr(git_cmd.subprocess, "run", fake_run)
    with pytest.raises(GitOpError) as info:
        run(repo, "status")
    assert str(info.value).startswith("cannot run git: ")


def test_require_repo_passes_on_repo(tmp_path):
    repo = init_repo(tmp_path / "repo")
    require_repo(repo)


def test_require_repo_refuses_plain_folder(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    with pytest.raises(NoRepoError) as info:
        require_repo(folder)
    assert str(info.value) == f"not a git repository: {folder}"
    assert issubclass(NoRepoError, BaseGitOpError)


def test_check_branch_name_accepts_valid_names():
    assert check_branch_name("work/x") == "work/x"
    assert check_branch_name("feature-1") == "feature-1"


@pytest.mark.parametrize("name", ["", "-x", "a b", "bad..name", "x" * (NAME_MAX + 1)])
def test_check_branch_name_refuses(name):
    with pytest.raises(GitOpError) as info:
        check_branch_name(name)
    assert str(info.value) == f"not a valid branch name: {name}"


def test_resolve_branch_and_short_sha(tmp_path):
    repo = init_repo(tmp_path / "repo")
    full = head_sha(repo)
    assert len(full) == 40
    assert resolve(repo, "main") == full
    assert resolve(repo, full[:7]) == full


@pytest.mark.parametrize("rev", ["nope", "-x", "a b", ""])
def test_resolve_refuses(tmp_path, rev):
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitOpError) as info:
        resolve(repo, rev)
    assert str(info.value) == f"unknown commit or branch: {rev}"


@pytest.mark.parametrize("path", ["a.txt", "sub/b.txt"])
def test_check_path_accepts(path):
    assert check_path(path) == path


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../x", "a/../b", ".harness", ".harness/x", "a\0b"],
)
def test_check_path_refuses(path):
    with pytest.raises(GitOpError) as info:
        check_path(path)
    assert str(info.value) == f"bad path: {path}"


def test_cap_truncates_long_text():
    assert cap("abc") == ("abc", False)
    assert cap("x" * (PATCH_MAX + 5)) == ("x" * PATCH_MAX, True)
