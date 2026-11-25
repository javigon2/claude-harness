import json
import subprocess
from pathlib import Path

from harness.repo_digest import DIGEST_FILE, MAX_TREE_LINES, load_or_build


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "README.md").write_text("# X\nhello\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "first")
    return tmp_path


def test_build_then_reuse(tmp_path: Path):
    repo = make_repo(tmp_path)
    d1 = load_or_build(repo)
    assert not d1.reused
    assert d1.head != "no-commit"
    for part in ("## Tree", "pyproject.toml", "## Manifest", "## README", "first"):
        assert part in d1.text
    assert load_or_build(repo).reused


def test_rebuild_after_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    load_or_build(repo)
    (repo / "b.py").write_text("")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "second")
    d = load_or_build(repo)
    assert not d.reused and "b.py" in d.text


def test_rebuild_after_uncommitted_edit(tmp_path: Path):
    repo = make_repo(tmp_path)
    load_or_build(repo)
    (repo / "README.md").write_text("changed\n")
    assert not load_or_build(repo).reused
    assert load_or_build(repo).reused


def test_corrupt_cache_rebuilds(tmp_path: Path):
    repo = make_repo(tmp_path)
    load_or_build(repo)
    (repo / DIGEST_FILE).write_text("not json")
    assert not load_or_build(repo).reused
    (repo / DIGEST_FILE).write_text(json.dumps({"head": "x"}))
    assert not load_or_build(repo).reused


def test_no_commit(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("hi\n")
    d = load_or_build(tmp_path)
    assert d.head == "no-commit" and "## README" in d.text
    assert "## Tree" not in d.text and "## Recent commits" not in d.text


def test_not_a_git_repo(tmp_path: Path):
    d = load_or_build(tmp_path)
    assert d.head == "no-commit" and d.text == ""


def test_tree_cap(tmp_path: Path):
    repo = make_repo(tmp_path)
    for i in range(MAX_TREE_LINES + 5):
        (repo / f"f{i:04}.txt").write_text("")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "many")
    d = load_or_build(repo)
    assert "... 7 more files" in d.text  # 207 files total (2 + 205), cap 200


def test_omitted_sections(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("")
    git(tmp_path, "add", "-A")
    text = load_or_build(tmp_path).text
    assert "## Tree" in text
    assert "## Manifest" not in text and "## README" not in text
