import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_routes import BadGitBody, check_git_body, git_post


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


PATHS_MSG = "paths must be a list of 1 to 500 file paths"
MSG_MSG = "message must be a non-empty string of at most 5000 characters"


def field_msg(key):
    return f"{key} must be a non-empty string of at most 200 characters"


BAD = [
    ("stage", [], "body must be a JSON object"),
    ("commit", {"msg": "x"}, "send exactly: message"),
    ("stage", {"path": ["a"]}, "send exactly: paths"),
    ("unstage", {"paths": ["a"], "x": 1}, "send exactly: paths"),
    ("branch", {"name": "x"}, "send exactly: name, start"),
    ("switch", {}, "send exactly: name"),
    ("merge", {"branch": "x"}, "send exactly: name"),
    ("delete-branch", {"name": "x", "y": 1}, "send exactly: name"),
    ("fetch", {"name": "o"}, "send exactly: remote"),
    ("push", {"remote": "o"}, "send exactly: remote, branch"),
    ("stage", {"paths": []}, PATHS_MSG),
    ("stage", {"paths": ["a"] * 501}, PATHS_MSG),
    ("unstage", {"paths": [""]}, PATHS_MSG),
    ("stage", {"paths": "a"}, PATHS_MSG),
    ("commit", {"message": "  "}, MSG_MSG),
    ("commit", {"message": "x" * 5001}, MSG_MSG),
    ("switch", {"name": "x" * 201}, field_msg("name")),
    ("switch", {"name": 5}, field_msg("name")),
    ("branch", {"name": "a", "start": " "}, field_msg("start")),
    ("push", {"remote": "o", "branch": 1}, field_msg("branch")),
    ("fetch", {"remote": ""}, field_msg("remote")),
]


@pytest.mark.parametrize(("route", "body", "message"), BAD)
def test_bad_bodies(route, body, message):
    with pytest.raises(BadGitBody) as err:
        check_git_body(route, body)
    assert str(err.value) == message


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("stage", {"paths": ["a"]}),
        ("commit", {"message": "x" * 5000}),
        ("branch", {"name": "f", "start": "HEAD"}),
        ("push", {"remote": "origin", "branch": "main"}),
    ],
)
def test_good_bodies(route, body):
    assert check_git_body(route, body) is body


def test_write_flow(repo):
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    assert git_post(repo, "stage", {"paths": ["new.txt"]}, None) == (200, {"ok": True})
    assert git(repo, "diff", "--cached", "--name-only") == "new.txt"
    assert git_post(repo, "unstage", {"paths": ["new.txt"]}, None) == (200, {"ok": True})
    assert git(repo, "diff", "--cached", "--name-only") == ""
    git_post(repo, "stage", {"paths": ["new.txt"]}, None)
    code, body = git_post(repo, "commit", {"message": "add"}, None)
    assert code == 200
    assert body == {"ok": True, "sha": git(repo, "rev-parse", "--short", "HEAD")}
    assert git_post(repo, "commit", {"message": "again"}, None) == (
        409,
        {"error": "nothing staged to commit"},
    )
    assert git_post(repo, "branch", {"name": "feat", "start": "HEAD"}, None) == (200, {"ok": True})
    assert git(repo, "branch", "--show-current") == "feat"
    (repo / "f.txt").write_text("f\n", encoding="utf-8")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-m", "f")
    assert git_post(repo, "switch", {"name": "main"}, None) == (200, {"ok": True})
    assert git(repo, "branch", "--show-current") == "main"
    code, body = git_post(repo, "merge", {"name": "feat"}, None)
    assert code == 200
    assert body["sha"] == git(repo, "rev-parse", "--short", "HEAD")
    assert git_post(repo, "delete-branch", {"name": "feat"}, None) == (200, {"ok": True})
    assert "feat" not in git(repo, "branch", "--format=%(refname:short)")


def test_fetch_and_push(repo, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    code, body = git_post(repo, "push", {"remote": "origin", "branch": "main"}, None)
    assert code == 200 and isinstance(body["output"], str)
    code, body = git_post(repo, "fetch", {"remote": "origin"}, None)
    assert code == 200 and isinstance(body["output"], str)
    assert git_post(repo, "fetch", {"remote": "nope"}, None) == (
        409,
        {"error": "unknown remote: nope"},
    )


def test_busy(repo):
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    before = git(repo, "status", "--porcelain")
    bodies = {
        "commit": {"message": "m"},
        "stage": {"paths": ["new.txt"]},
        "unstage": {"paths": ["new.txt"]},
        "branch": {"name": "b", "start": "HEAD"},
        "switch": {"name": "main"},
        "merge": {"name": "main"},
        "delete-branch": {"name": "b"},
    }
    for route, body in bodies.items():
        assert git_post(repo, route, body, "a chat is working") == (
            409,
            {"error": "a chat is working"},
        )
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "branch", "--format=%(refname:short)") == "main"
    assert git_post(repo, "fetch", {"remote": "nope"}, "a chat is working") == (
        409,
        {"error": "unknown remote: nope"},
    )


def test_unknown_route_and_no_repo(tmp_path):
    assert git_post(tmp_path, "nope", {}, None) == (404, {"error": "not found"})
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_post(plain, "commit", {"message": "m"}, None) == (
        409,
        {"error": f"not a git repository: {plain}"},
    )
