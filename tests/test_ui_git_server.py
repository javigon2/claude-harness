import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.server import make_server


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


def _start(repo: Path):
    srv = make_server(repo, 0, poll_interval=0.05, heartbeat_s=0.2)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", t


def _get(url: str):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "demo")


@pytest.fixture
def server(repo):
    srv, base, t = _start(repo)
    yield srv, base
    srv.shutdown()
    srv.server_close()
    t.join(5)


def test_status_reports_branch(server):
    _, base = server
    code, body = _get(f"{base}/api/git/status")
    assert code == 200
    assert body["branch"] == "main"


def test_graph_returns_commit_and_limit(server):
    _, base = server
    code, body = _get(f"{base}/api/git/graph?limit=5")
    assert code == 200
    assert body["limit"] == 5
    assert len(body["commits"]) == 1


def test_commit_by_branch_name(server):
    _, base = server
    code, body = _get(f"{base}/api/git/commit/main")
    assert code == 200
    assert body["subject"] == "init"


def test_commit_with_encoded_slash_in_branch_name(repo, server):
    subprocess.run(["git", "branch", "work/x"], cwd=repo, check=True, capture_output=True)
    _, base = server
    code, _ = _get(f"{base}/api/git/commit/work%2Fx")
    assert code == 200


def test_compare_same_branch(server):
    _, base = server
    code, _ = _get(f"{base}/api/git/compare?base=main&head=main")
    assert code == 200


def test_graph_bad_limit_is_400(server):
    _, base = server
    code, body = _get(f"{base}/api/git/graph?limit=abc")
    assert code == 400
    assert "error" in body


def test_unknown_git_route_is_404(server):
    _, base = server
    code, body = _get(f"{base}/api/git/nope")
    assert code == 404
    assert body == {"error": "not found"}


def test_plain_folder_is_409(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    srv, base, t = _start(plain)
    try:
        code, body = _get(f"{base}/api/git/status")
        assert code == 409
        assert "not a git repository" in body["error"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
