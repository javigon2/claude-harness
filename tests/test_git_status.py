import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_cmd import NoRepoError
from harness.ui.git_status import read_status


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


def test_clean_repo_has_no_entries(tmp_path):
    repo = init_repo(tmp_path / "proj")
    status = read_status(repo)
    assert status.entries == []
    assert status.branch == "main"
    assert status.detached is False
    assert status.merging is False


def test_edited_tracked_file_is_unstaged(tmp_path):
    repo = init_repo(tmp_path / "proj")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    entries = read_status(repo).entries
    assert len(entries) == 1
    assert entries[0].path == "README.md"
    assert entries[0].staged == " "
    assert entries[0].unstaged == "M"
    assert entries[0].untracked is False
    assert entries[0].conflicted is False


def test_staged_file_moves_to_staged_column(tmp_path):
    repo = init_repo(tmp_path / "proj")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    entries = read_status(repo).entries
    assert len(entries) == 1
    assert entries[0].staged == "M"
    assert entries[0].unstaged == " "


def test_untracked_file_in_subfolder_has_full_path(tmp_path):
    repo = init_repo(tmp_path / "proj")
    (repo / "sub").mkdir()
    (repo / "sub" / "new.txt").write_text("new\n", encoding="utf-8")
    entries = read_status(repo).entries
    assert len(entries) == 1
    assert entries[0].path == "sub/new.txt"
    assert entries[0].untracked is True
    assert entries[0].old_path is None


def test_rename_reports_new_and_old_path(tmp_path):
    repo = init_repo(tmp_path / "proj")
    git(repo, "mv", "README.md", "docs.md")
    entries = read_status(repo).entries
    assert len(entries) == 1
    assert entries[0].staged == "R"
    assert entries[0].path == "docs.md"
    assert entries[0].old_path == "README.md"


def test_harness_folder_never_appears(tmp_path):
    repo = init_repo(tmp_path / "proj")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "state.json").write_text("{}\n", encoding="utf-8")
    (repo / "notes.txt").write_text("hi\n", encoding="utf-8")
    paths = [e.path for e in read_status(repo).entries]
    assert paths == ["notes.txt"]


def test_merge_conflict_is_reported(tmp_path):
    repo = init_repo(tmp_path / "proj")
    git(repo, "checkout", "-b", "other")
    (repo / "README.md").write_text("theirs\n", encoding="utf-8")
    git(repo, "commit", "-am", "theirs")
    git(repo, "checkout", "main")
    (repo / "README.md").write_text("ours\n", encoding="utf-8")
    git(repo, "commit", "-am", "ours")
    result = subprocess.run(
        ["git", "merge", "other"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0

    status = read_status(repo)
    assert status.merging is True
    assert status.branch == "main"
    readme = [e for e in status.entries if e.path == "README.md"]
    assert len(readme) == 1
    assert readme[0].conflicted is True
    assert readme[0].staged == "U"
    assert readme[0].unstaged == "U"


def test_detached_head_has_no_branch(tmp_path):
    repo = init_repo(tmp_path / "proj")
    git(repo, "checkout", "--detach")
    status = read_status(repo)
    assert status.branch is None
    assert status.detached is True
    assert status.merging is False


def test_plain_folder_raises_no_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NoRepoError):
        read_status(plain)
