import json
import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_routes import git_get


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


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo")


def ok(repo, route, query=None):
    code, body = git_get(repo, route, query or {})
    assert code == 200, body
    json.dumps(body)
    return body


def test_graph(repo):
    body = ok(repo, "graph")
    assert len(body["commits"]) == 1
    assert body["commits"][0]["subject"] == "init"


@pytest.mark.parametrize("limit", ["0", "abc", "5001"])
def test_graph_bad_limit(repo, limit):
    assert git_get(repo, "graph", {"limit": [limit]}) == (
        400,
        {"error": "limit must be a whole number from 1 to 5000"},
    )


def test_branches_and_status(repo):
    assert ok(repo, "branches")["current"] == "main"
    assert ok(repo, "status")["entries"] == []


def test_compare(repo):
    body = ok(repo, "compare", {"base": ["main"], "head": ["main"]})
    assert body["ahead"] == 0
    assert git_get(repo, "compare", {"base": ["main"]}) == (
        400,
        {"error": "send base and head"},
    )


def test_diff(repo):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert ok(repo, "diff", {"path": ["README.md"]})["patch"]
    assert git_get(repo, "diff", {}) == (400, {"error": "send path"})


def test_commit(repo):
    assert ok(repo, "commit/main")["subject"] == "init"
    assert git_get(repo, "commit/", {}) == (400, {"error": "send a commit"})
    assert git_get(repo, "commit/nope", {}) == (
        400,
        {"error": "unknown commit or branch: nope"},
    )


def test_commit_slash_branch(repo):
    git(repo, "branch", "work/x")
    assert ok(repo, "commit/work/x")["subject"] == "init"


def test_not_found(repo):
    assert git_get(repo, "nope", {}) == (404, {"error": "not found"})


def test_no_repo(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    expected = (409, {"error": f"not a git repository: {folder}"})
    assert git_get(folder, "graph", {}) == expected
    assert git_get(folder, "branches", {}) == expected
    assert git_get(folder, "status", {}) == expected
    assert git_get(folder, "compare", {"base": ["main"], "head": ["main"]}) == expected
    assert git_get(folder, "diff", {"path": ["a.txt"]}) == expected
    assert git_get(folder, "commit/main", {}) == expected
    assert git_get(folder, "graph", {"limit": ["abc"]})[0] == 400
