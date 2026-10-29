"""read_repo_file and GET /api/file: a project file for the page's reader."""

from pathlib import Path

import pytest

from harness.ui.repofile import read_repo_file
from harness.ui.uploads import MAX_TEXT_BYTES


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "PLAN.md").write_text("# Plan\n\nbody\n", encoding="utf-8")
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    return repo


def test_markdown_file_comes_with_blocks(tmp_path: Path) -> None:
    doc = read_repo_file(_repo(tmp_path), "docs/PLAN.md")
    assert doc is not None
    assert (doc["id"], doc["name"], doc["ext"]) == ("docs/PLAN.md", "PLAN.md", "md")
    assert doc["blocks"][0]["type"] == "heading"


def test_code_file_has_no_blocks_and_absolute_path_inside_repo_works(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    doc = read_repo_file(repo, str(repo / "a.py"))
    assert doc is not None and doc["blocks"] is None and doc["id"] == "a.py"
    assert read_repo_file(repo, "./a.py") is not None


@pytest.mark.parametrize(
    "raw",
    ["../outside.md", "/etc/hosts", "missing.md", "docs", "", " ", None, 5, "a\0.md", "a.exe"],
)
def test_refused_paths(tmp_path: Path, raw: object) -> None:
    repo = _repo(tmp_path)
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    assert read_repo_file(repo, raw) is None


def test_symlink_out_of_repo_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    (repo / "link.md").symlink_to(tmp_path / "outside.md")
    assert read_repo_file(repo, "link.md") is None


def test_hidden_areas_refused_except_plans(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for rel in (".git/notes.md", ".harness/uploads/x.md", ".harness/plans/s/plan.md", ".env.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x", encoding="utf-8")
    assert read_repo_file(repo, ".git/notes.md") is None
    assert read_repo_file(repo, ".harness/uploads/x.md") is None
    assert read_repo_file(repo, ".env.md") is None
    assert read_repo_file(repo, ".harness/plans/s/plan.md") is not None


def test_binary_and_oversized_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "bin.md").write_bytes(b"ok\0no")
    (repo / "latin.md").write_bytes(b"\xff\xfe")
    (repo / "big.md").write_text("x" * (MAX_TEXT_BYTES + 1), encoding="utf-8")
    for name in ("bin.md", "latin.md", "big.md"):
        assert read_repo_file(repo, name) is None
