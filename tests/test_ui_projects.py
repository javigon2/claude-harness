import json
import os
import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.projects import (
    MAX_ENTRIES,
    RECENT_MAX,
    ProjectError,
    default_recent_path,
    list_dir,
    load_recent,
    local_origin,
    remember,
    repo_info,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# ---------------------------------------------------------------- list_dir


def test_list_dir_sorts_case_insensitively_and_hides_dot_names(tmp_path: Path) -> None:
    for name in ("banana", "Apple", "cherry", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "a-file.txt").write_text("x")

    listing = list_dir(tmp_path)

    assert listing.path == str(tmp_path)
    assert listing.parent == str(tmp_path.parent)
    assert [e.name for e in listing.entries] == ["Apple", "banana", "cherry"]
    assert listing.entries[0].path == str(tmp_path / "Apple")
    assert listing.truncated is False
    shown = list_dir(tmp_path, show_hidden=True)
    assert [e.name for e in shown.entries] == [".hidden", "Apple", "banana", "cherry"]


def test_list_dir_marks_git_folders_and_follows_symlinks(tmp_path: Path) -> None:
    init_repo(tmp_path / "repo")
    (tmp_path / "plain").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "plain")
    (tmp_path / "file-link").symlink_to(tmp_path / "repo" / "README.md")
    (tmp_path / "broken").symlink_to(tmp_path / "missing")

    entries = {e.name: e for e in list_dir(tmp_path).entries}

    assert sorted(entries) == ["link", "plain", "repo"]
    assert entries["repo"].is_git is True
    assert entries["plain"].is_git is False and entries["link"].is_git is False


def test_list_dir_truncates(tmp_path: Path) -> None:
    for i in range(MAX_ENTRIES + 3):
        (tmp_path / f"d{i:04d}").mkdir()
    listing = list_dir(tmp_path)
    assert len(listing.entries) == MAX_ENTRIES and listing.truncated is True
    assert listing.entries[0].name == "d0000"


def test_list_dir_root_has_no_parent_and_dotdot_is_normalised(tmp_path: Path) -> None:
    assert list_dir(Path("/")).parent is None
    (tmp_path / "a").mkdir()
    assert list_dir(tmp_path / "a" / "..").path == str(tmp_path)


def test_list_dir_errors(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(ProjectError) as err:
        list_dir(tmp_path / "f.txt")
    assert str(err.value) == f"not a folder: {tmp_path / 'f.txt'}"
    with pytest.raises(ProjectError, match="^not a folder: "):
        list_dir(tmp_path / "missing")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_list_dir_unreadable_folder(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(ProjectError) as err:
            list_dir(locked)
    finally:
        locked.chmod(0o755)
    assert str(err.value).startswith(f"cannot read {locked}: ")


# ---------------------------------------------------------------- repo_info


def test_repo_info_for_a_missing_or_plain_folder(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    for target in (plain, tmp_path / "nope"):
        info = repo_info(target)
        assert (info.is_git, info.has_commit, info.on_main) == (False, False, False)
        assert info.branch is None
        assert info.dirty is False and info.origin_path is None
        assert info.name == target.name and info.path == str(target)


def test_repo_info_inside_someone_elses_repo_is_not_a_repo(tmp_path: Path) -> None:
    init_repo(tmp_path / "outer")
    inner = tmp_path / "outer" / "inner"
    inner.mkdir()
    assert repo_info(inner).is_git is False


def test_repo_info_clean_repo_on_main(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "proj")
    info = repo_info(repo)
    assert (info.is_git, info.has_commit, info.branch) == (True, True, "main")
    assert (info.on_main, info.dirty, info.origin_path) == (True, False, None)
    assert info.name == "proj"


def test_repo_info_dirty(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "proj")
    (repo / "new.txt").write_text("x")
    assert repo_info(repo).dirty is True


def test_repo_info_without_a_commit(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    info = repo_info(repo)
    assert (info.is_git, info.has_commit, info.branch, info.on_main) == (True, False, "main", True)


def test_repo_info_other_branch_and_detached_head(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "proj")
    _git(repo, "switch", "-c", "feature")
    info = repo_info(repo)
    assert (info.branch, info.on_main) == ("feature", False)
    _git(repo, "checkout", "--detach")
    info = repo_info(repo)
    assert (info.branch, info.on_main, info.has_commit) == (None, False, True)


def test_repo_info_with_a_local_origin(tmp_path: Path) -> None:
    origin = init_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    assert repo_info(clone).origin_path == str(origin)


# ---------------------------------------------------------------- local_origin


def test_local_origin_variants(tmp_path: Path) -> None:
    origin = init_repo(tmp_path / "origin")
    repo = init_repo(tmp_path / "repo")
    assert local_origin(repo) is None
    _git(repo, "remote", "add", "origin", str(origin))
    assert local_origin(repo) == origin
    _git(repo, "remote", "set-url", "origin", origin.as_uri())
    assert local_origin(repo) == origin
    _git(repo, "remote", "set-url", "origin", "../origin")
    assert local_origin(repo) == origin
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone"))
    assert local_origin(repo) is None
    _git(repo, "remote", "set-url", "origin", "https://example.com/x/y.git")
    assert local_origin(repo) is None
    _git(repo, "remote", "set-url", "origin", "git@example.com:x/y.git")
    assert local_origin(repo) is None


def test_local_origin_of_a_non_repo(tmp_path: Path) -> None:
    assert local_origin(tmp_path) is None


# ---------------------------------------------------------------- recents


def test_default_recent_path() -> None:
    assert default_recent_path() == Path.home() / ".config" / "agent-orchestrator" / "recent.json"


def test_remember_moves_to_front_and_caps(tmp_path: Path) -> None:
    recent = tmp_path / "cfg" / "recent.json"
    dirs = []
    for i in range(RECENT_MAX + 2):
        d = tmp_path / f"p{i}"
        d.mkdir()
        dirs.append(d)
        remember(recent, d)

    assert load_recent(recent) == [str(d) for d in reversed(dirs)][:RECENT_MAX]
    remember(recent, dirs[-3])
    got = load_recent(recent)
    assert got[0] == str(dirs[-3]) and len(got) == RECENT_MAX
    assert got.count(str(dirs[-3])) == 1


def test_load_recent_drops_missing_folders_and_bad_json(tmp_path: Path) -> None:
    recent = tmp_path / "recent.json"
    assert load_recent(recent) == []
    keep = tmp_path / "keep"
    keep.mkdir()
    recent.write_text(json.dumps([str(keep), str(tmp_path / "gone"), 5, None]))
    assert load_recent(recent) == [str(keep)]
    recent.write_text("not json")
    assert load_recent(recent) == []
    recent.write_text('{"a": 1}')
    assert load_recent(recent) == []


def test_remember_ignores_an_unwritable_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    remember(blocker / "recent.json", tmp_path)  # parent is a file: must not raise
