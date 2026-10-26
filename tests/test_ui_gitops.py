import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui import gitops
from harness.ui.gitops import (
    INIT_MESSAGE,
    PREVIEW_MAX,
    GitOpError,
    init_preview,
    init_project,
    pull_into_origin,
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


def _folder(tmp_path: Path, name: str = "proj") -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / "a.txt").write_text("a\n")
    (path / "sub").mkdir()
    (path / "sub" / "b.txt").write_text("b\n")
    return path


# ---------------------------------------------------------------- init_preview


def test_preview_of_a_folder_without_a_repo_changes_nothing(tmp_path: Path) -> None:
    path = _folder(tmp_path)
    (path / ".gitignore").write_text("ignored.txt\n")
    (path / "ignored.txt").write_text("x")
    (path / ".harness").mkdir()
    (path / ".harness" / "state.json").write_text("{}")

    files, truncated = init_preview(path)

    assert files == [".gitignore", "a.txt", "sub/b.txt"]
    assert truncated is False
    assert not (path / ".git").exists()
    assert sorted(p.name for p in path.iterdir()) == [
        ".gitignore",
        ".harness",
        "a.txt",
        "ignored.txt",
        "sub",
    ]


def test_preview_in_an_existing_repo_lists_tracked_and_untracked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "new.txt").write_text("n")
    files, _ = init_preview(repo)
    assert files == ["README.md", "new.txt"]


def test_preview_is_capped(tmp_path: Path) -> None:
    path = tmp_path / "many"
    path.mkdir()
    for i in range(PREVIEW_MAX + 5):
        (path / f"f{i:05d}").write_text("")
    files, truncated = init_preview(path)
    assert len(files) == PREVIEW_MAX and truncated is True


# ---------------------------------------------------------------- init_project


def test_init_a_plain_folder(tmp_path: Path) -> None:
    path = _folder(tmp_path)
    (path / ".harness").mkdir()
    (path / ".harness" / "x").write_text("x")

    sha = init_project(path)

    assert sha == git(path, "rev-parse", "--short", "HEAD")
    assert git(path, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(path, "log", "-1", "--format=%s") == INIT_MESSAGE
    assert git(path, "ls-files").splitlines() == ["a.txt", "sub/b.txt"]
    assert git(path, "status", "--porcelain") == ""


def test_init_an_empty_folder_makes_an_empty_commit(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.mkdir()
    init_project(path)
    assert git(path, "rev-list", "--count", "HEAD") == "1"
    assert git(path, "ls-files") == ""


def test_init_a_repo_without_a_commit_commits_the_files(tmp_path: Path) -> None:
    path = _folder(tmp_path)
    git(path, "init", "-b", "trunk")
    init_project(path)
    assert git(path, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(path, "ls-files").splitlines() == ["a.txt", "sub/b.txt"]


def test_init_a_repo_on_another_branch_creates_main(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    git(repo, "branch", "-m", "trunk")
    (repo / "dirty.txt").write_text("d")  # uncommitted changes come along

    sha = init_project(repo)

    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert sha == git(repo, "rev-parse", "--short", "HEAD")
    assert (repo / "dirty.txt").exists()


def test_init_switches_to_an_existing_main_when_clean(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    git(repo, "switch", "-c", "feature")
    init_project(repo)
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_init_refuses_a_dirty_tree_off_main(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    git(repo, "switch", "-c", "feature")
    (repo / "README.md").write_text("changed\n")
    with pytest.raises(GitOpError) as err:
        init_project(repo)
    assert str(err.value) == "commit or stash your changes on feature first"
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "feature"


def test_init_with_nothing_to_do(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitOpError, match="^nothing to initialize"):
        init_project(repo)


def test_init_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(GitOpError, match="^not a folder: "):
        init_project(tmp_path / "missing")


def test_init_excludes_the_harness_dir(tmp_path: Path) -> None:
    path = _folder(tmp_path)
    init_project(path)
    (path / ".harness").mkdir()
    (path / ".harness" / "x").write_text("x")
    assert git(path, "status", "--porcelain") == ""


def test_init_reports_a_failing_commit(tmp_path: Path, monkeypatch) -> None:
    path = _folder(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.delenv(f"GIT_{var}_NAME")
        monkeypatch.delenv(f"GIT_{var}_EMAIL")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    with pytest.raises(GitOpError) as err:
        init_project(path)
    assert str(err.value)  # git's own sentence about the missing identity


# ---------------------------------------------------------------- pull_into_origin


def _origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = init_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    return origin, clone


def test_pull_fast_forwards_origin(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    (clone / "feature.txt").write_text("f\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "feature")

    out = pull_into_origin(clone)

    assert out
    assert git(origin, "rev-parse", "main") == git(clone, "rev-parse", "main")
    assert (origin / "feature.txt").read_text() == "f\n"


def test_pull_when_already_up_to_date(tmp_path: Path) -> None:
    _, clone = _origin_and_clone(tmp_path)
    assert "up to date" in pull_into_origin(clone).lower()


def test_pull_refuses_a_diverged_origin(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    (origin / "o.txt").write_text("o\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "origin only")
    (clone / "c.txt").write_text("c\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "clone only")
    before = git(origin, "rev-parse", "main")

    with pytest.raises(GitOpError) as err:
        pull_into_origin(clone)

    assert str(err.value)
    assert git(origin, "rev-parse", "main") == before


def test_pull_refuses_origin_off_main(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    git(origin, "switch", "-c", "feature")
    with pytest.raises(GitOpError) as err:
        pull_into_origin(clone)
    assert str(err.value) == "origin is on feature, not main"


def test_pull_needs_a_local_origin(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    with pytest.raises(GitOpError) as err:
        pull_into_origin(repo)
    assert str(err.value) == "origin is not a local folder"
    git(repo, "remote", "add", "origin", "https://example.com/x.git")
    with pytest.raises(GitOpError, match="^origin is not a local folder$"):
        pull_into_origin(repo)


def test_git_missing_is_a_sentence(monkeypatch, tmp_path: Path) -> None:
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(gitops.subprocess, "run", boom)
    with pytest.raises(GitOpError, match="^cannot run git"):
        init_preview(tmp_path)
