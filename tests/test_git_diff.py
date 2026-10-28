import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_cmd import NoRepoError
from harness.ui.git_diff import read_commit, read_compare, read_file_diff
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


def commit_file(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg)


def test_no_repo(tmp_path):
    with pytest.raises(NoRepoError):
        read_commit(tmp_path, "HEAD")


def test_root_commit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("x\n" * 40, encoding="utf-8")
    (repo / "b.bin").write_bytes(b"\x00\x01\x02")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "subj", "-m", "body line")
    d = read_commit(repo, "HEAD")
    assert d.parents == []
    assert d.subject == "subj"
    assert d.body == "body line"
    assert d.sha == git(repo, "rev-parse", "HEAD")
    got = [(f.path, f.status, f.added, f.deleted) for f in d.files]
    assert got == [("a.txt", "A", 40, 0), ("b.bin", "A", None, None)]
    assert "new file mode" in d.patch
    assert not d.truncated


def test_rename_binary_commit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("x\n" * 40, encoding="utf-8")
    (repo / "b.bin").write_bytes(b"\x00\x01\x02")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "one")
    git(repo, "mv", "a.txt", "c.txt")
    (repo / "b.bin").write_bytes(b"\x00\x01\x02\x03")
    (repo / "n.txt").write_text("n\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "two")
    d = read_commit(repo, "HEAD")
    got = [(f.path, f.old_path, f.status, f.added, f.deleted) for f in d.files]
    assert got == [
        ("b.bin", None, "M", None, None),
        ("c.txt", "a.txt", "R", 0, 0),
        ("n.txt", None, "A", 1, 0),
    ]
    assert "Binary files a/b.bin and b/b.bin differ" in d.patch
    assert "rename from a.txt" in d.patch
    assert len(d.parents) == 1


def test_merge_commit_first_parent(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "switch", "-c", "feat")
    commit_file(repo, "f.txt", "f\n", "feat")
    git(repo, "switch", "main")
    commit_file(repo, "m.txt", "m\n", "main work")
    git(repo, "merge", "--no-ff", "feat", "-m", "merge")
    d = read_commit(repo, "HEAD")
    assert len(d.parents) == 2
    assert [f.path for f in d.files] == ["f.txt"]


def test_truncation(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "r")
    commit_file(repo, "a.txt", "hello world\n" * 10, "more")
    monkeypatch.setattr("harness.ui.git_cmd.PATCH_MAX", 10)
    d = read_commit(repo, "HEAD")
    assert d.truncated
    assert len(d.patch) == 10


def test_compare(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "switch", "-c", "feat")
    commit_file(repo, "f1.txt", "1\n", "f1")
    commit_file(repo, "f2.txt", "2\n", "f2")
    git(repo, "switch", "main")
    commit_file(repo, "m.txt", "m\n", "m1")
    c = read_compare(repo, "main", "feat")
    assert (c.ahead, c.behind) == (2, 1)
    assert c.base == "main" and c.head == "feat"
    assert c.merge_base == git(repo, "merge-base", "main", "feat")
    assert [x.subject for x in c.commits] == ["f2", "f1"]
    assert [f.path for f in c.files] == ["f1.txt", "f2.txt"]
    assert "f1.txt" in c.patch and "m.txt" not in c.patch


def test_compare_unrelated(tmp_path):
    repo = init_repo(tmp_path / "r")
    git(repo, "switch", "--orphan", "other")
    commit_file(repo, "o.txt", "o\n", "orphan")
    c = read_compare(repo, "main", "other")
    assert c.merge_base is None
    assert c.files == []
    assert c.patch == ""
    assert not c.truncated


def test_compare_bad_rev(tmp_path):
    repo = init_repo(tmp_path / "r")
    with pytest.raises(GitOpError, match="unknown commit or branch: nope"):
        read_compare(repo, "main", "nope")


def test_file_diff_unstaged_and_staged(tmp_path):
    repo = init_repo(tmp_path / "r")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    d = read_file_diff(repo, "README.md", False)
    assert "+changed" in d.patch
    assert not d.binary and not d.staged
    assert read_file_diff(repo, "README.md", True).patch == ""
    git(repo, "add", "-A")
    assert "+changed" in read_file_diff(repo, "README.md", True).patch
    assert read_file_diff(repo, "README.md", False).patch == ""


def test_file_diff_untracked(tmp_path):
    repo = init_repo(tmp_path / "r")
    (repo / "u.txt").write_text("new\n", encoding="utf-8")
    d = read_file_diff(repo, "u.txt", False)
    assert "new file mode" in d.patch


def test_file_diff_binary(tmp_path):
    repo = init_repo(tmp_path / "r")
    (repo / "b.bin").write_bytes(b"\x00\x01")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "bin")
    (repo / "b.bin").write_bytes(b"\x00\x02\x03")
    assert read_file_diff(repo, "b.bin", False).binary


def test_file_diff_bad_path(tmp_path):
    repo = init_repo(tmp_path / "r")
    with pytest.raises(GitOpError, match="bad path: ../x"):
        read_file_diff(repo, "../x", False)
