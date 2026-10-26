"""The project endpoints of a server made with a Host: browse, open, Initialize, Bring into
origin, restart, and the NO_PROJECT answers before a folder is open."""

import json
import re
import subprocess
import threading
from pathlib import Path

import pytest
from git_helpers import init_repo
from test_office import Launches
from test_ui_office_server import Site, _snapshot, state, wait_for

from harness.ui.host import NO_PROJECT, Host
from harness.ui.office import Office
from harness.ui.server import make_server

START = "/api/planner/start"


class HostSite(Site):
    """A running server with a Host and no project open."""

    def __init__(self, cwd: Path, recent: Path) -> None:
        self.repo, self.office = cwd, None
        self.launches = Launches()
        self.host = Host(
            port=0,
            cwd=cwd,
            fake=True,
            fake_delay=0,
            recent_path=recent,
            office_factory=lambda path: Office(
                path, fake=True, fake_delay=0, launcher=self.launches
            ),
        )
        self.server = make_server(cwd, 0, poll_interval=0.05, heartbeat_s=0.2, host=self.host)
        self.host.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def _make_office(self, path: Path) -> Office:
        return Office(path, fake=True, fake_delay=0, launcher=self.launches)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.host.close()


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


@pytest.fixture
def site(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    s = HostSite(cwd, tmp_path / "cfg" / "recent.json")
    yield s
    s.stop()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _open(site: HostSite, path: Path):
    return site.post("/api/project/open", {"path": str(path)})


# ---------------------------------------------------------------- before a project is open


def test_project_info_without_a_project(site, tmp_path):
    data = site.get_json("/api/project")
    assert data["project"] is None and data["recent"] == [] and data["busy"] is None
    assert data["cwd"] == str(tmp_path / "cwd") and data["home"] == str(Path.home())
    assert data["started"] == site.host.started


def test_the_page_is_served_with_a_token(site):
    status, _, page = site.request("GET", "/")
    assert status == 200
    assert re.search(rb'<meta name="harness-token" content="[^"]+">', page)


@pytest.mark.parametrize(
    "path", ["/api/office", "/api/plan", "/api/plans", "/api/runs", "/api/run/x", "/api/report/x"]
)
def test_gets_need_a_project(site, path):
    status, _, data = site.request("GET", path)
    assert status == 409 and json.loads(data) == {"error": NO_PROJECT}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (START, {"description": "x"}),
        ("/api/planner/message", {"text": "x"}),
        ("/api/planner/implement", {}),
        ("/api/planner/stage", {"stage": 1}),
        ("/api/chat/start", {"tier": "haiku", "text": "x"}),
        ("/api/run/resume", {}),
        ("/api/run/stop", {}),
        ("/api/run/launch", {"plan_path": "x"}),
        ("/api/run/options", {"review": False, "max_parallel": 4, "max_cost": None, "fake": True}),
        ("/api/orchestrator/message", {"text": "x"}),
        ("/api/project/init-preview", {}),
        ("/api/project/init", {}),
        ("/api/project/pull-origin", {}),
    ],
)
def test_posts_need_a_project(site, path, body):
    status, _, out = site.post(path, body)
    assert (status, out) == (409, {"error": NO_PROJECT})


# ---------------------------------------------------------------- browse


def test_browse(site, tmp_path):
    root = tmp_path / "root"
    for name in ("b", "A", ".hidden"):
        (root / name).mkdir(parents=True)
    init_repo(root / "gitty")

    status, _, out = site.post("/api/project/browse", {"path": str(root), "hidden": False})
    assert status == 200
    assert out["path"] == str(root) and out["parent"] == str(tmp_path)
    assert [(e["name"], e["is_git"]) for e in out["entries"]] == [
        ("A", False),
        ("b", False),
        ("gitty", True),
    ]
    assert out["truncated"] is False
    _, _, out = site.post("/api/project/browse", {"path": str(root), "hidden": True})
    assert [e["name"] for e in out["entries"]][0] == ".hidden"


def test_browse_expands_the_home_folder(site, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    status, _, out = site.post("/api/project/browse", {"path": "~", "hidden": False})
    assert status == 200 and out["path"] == str(tmp_path)


def test_browse_errors_are_400(site, tmp_path):
    (tmp_path / "f.txt").write_text("x")
    for path in (tmp_path / "missing", tmp_path / "f.txt"):
        status, _, out = site.post("/api/project/browse", {"path": str(path), "hidden": False})
        assert status == 400 and out == {"error": f"not a folder: {path}"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"path": "relative/dir", "hidden": False}, "path must be an absolute folder path"),
        ({"path": "", "hidden": False}, "path must be a non-empty string"),
        ({"path": "/" + "a" * 4096, "hidden": False}, "path must be a non-empty string"),
        ({"path": 5, "hidden": False}, "path must be a non-empty string"),
        ({"path": "/tmp", "hidden": "no"}, "hidden must be true or false"),
        ({"path": "/tmp"}, "send exactly"),
        ({"path": "/tmp", "hidden": False, "extra": 1}, "send exactly"),
        ([], "body must be a JSON object"),
    ],
)
def test_browse_bad_bodies_are_400(site, body, message):
    status, _, out = site.post("/api/project/browse", body)
    assert status == 400 and out["error"].startswith(message)


# ---------------------------------------------------------------- open


def test_open_a_project_and_work_in_it(site, tmp_path):
    project = init_repo(tmp_path / "proj")

    status, _, out = _open(site, project)

    assert status == 200 and out["ok"] is True
    assert out["project"]["name"] == "proj" and out["project"]["on_main"] is True
    info = site.get_json("/api/project")
    assert info["project"]["path"] == str(project) and info["recent"] == [str(project)]
    assert site.get_json("/api/office")["mode"] == "interactive"
    assert site.post(START, {"description": "a demo"})[0] == 200
    wait_for(lambda: state(site) == "asking")
    assert site.get_json("/api/project")["busy"] == "a planning session is open; finish it first"


def test_open_is_refused_while_planning(site, tmp_path):
    a, b = init_repo(tmp_path / "a"), init_repo(tmp_path / "b")
    _open(site, a)
    site.post(START, {"description": "a demo"})
    wait_for(lambda: state(site) == "asking")

    status, _, out = _open(site, b)

    assert (status, out) == (409, {"error": "a planning session is open; finish it first"})
    assert site.get_json("/api/project")["project"]["path"] == str(a)


def test_open_a_missing_folder_is_409_and_relative_is_400(site, tmp_path):
    status, _, out = _open(site, tmp_path / "nope")
    assert status == 409 and out["error"] == f"not a folder: {tmp_path / 'nope'}"
    assert site.post("/api/project/open", {"path": "rel"})[0] == 400
    assert site.post("/api/project/open", {"path": str(tmp_path), "hidden": False})[0] == 400


def test_switching_projects_serves_the_new_one(site, tmp_path):
    a, b = init_repo(tmp_path / "a"), init_repo(tmp_path / "b")
    (b / "marker.txt").write_text("m")
    _open(site, a)
    _open(site, b)
    assert site.get_json("/api/project")["recent"] == [str(b), str(a)]
    assert (b / ".harness" / "office.lock").exists()
    assert not (a / ".harness" / "office.lock").exists()


# ---------------------------------------------------------------- Initialize


def test_init_preview_and_init(site, tmp_path):
    plain = tmp_path / "plain"
    (plain / "sub").mkdir(parents=True)
    (plain / "a.txt").write_text("a")
    (plain / "sub" / "b.txt").write_text("b")
    _open(site, plain)
    assert site.get_json("/api/project")["project"]["is_git"] is False
    before = _snapshot(plain)

    status, _, out = site.post("/api/project/init-preview", {})
    assert (status, out) == (200, {"files": ["a.txt", "sub/b.txt"], "truncated": False})
    assert {p.name for p in plain.iterdir() if p.name != ".harness"} == {"a.txt", "sub"}
    assert not (plain / ".git").exists()
    assert {k: v for k, v in _snapshot(plain).items() if ".harness" not in k.parts} == {
        k: v for k, v in before.items() if ".harness" not in k.parts
    }

    status, _, out = site.post("/api/project/init", {})

    assert status == 200 and out["ok"] is True
    assert out["sha"] == _git(plain, "rev-parse", "--short", "HEAD")
    project = out["project"]
    assert (project["is_git"], project["has_commit"], project["on_main"]) == (True, True, True)
    assert _git(plain, "ls-files").splitlines() == ["a.txt", "sub/b.txt"]


def test_init_again_has_nothing_to_do(site, tmp_path):
    repo = init_repo(tmp_path / "repo")
    _open(site, repo)
    status, _, out = site.post("/api/project/init", {})
    assert status == 409 and out["error"].startswith("nothing to initialize")


def test_init_is_refused_while_planning(site, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _open(site, plain)
    site.post(START, {"description": "a demo"})
    wait_for(lambda: state(site) == "asking")
    status, _, out = site.post("/api/project/init", {})
    assert status == 409 and "planning session" in out["error"]
    assert not (plain / ".git").exists()


def test_init_bodies_take_no_fields(site):
    for route in ("init-preview", "init", "pull-origin"):
        status, _, out = site.post(f"/api/project/{route}", {"x": 1})
        assert status == 400 and "no fields" in out["error"]


# ---------------------------------------------------------------- Bring into origin


def test_pull_origin(site, tmp_path):
    origin = init_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "x.txt").write_text("x")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "x")
    _open(site, clone)
    assert site.get_json("/api/project")["project"]["origin_path"] == str(origin)

    status, _, out = site.post("/api/project/pull-origin", {})

    assert status == 200 and out["ok"] is True and out["output"]
    assert _git(origin, "rev-parse", "main") == _git(clone, "rev-parse", "main")


def test_pull_origin_refusals_are_409(site, tmp_path):
    repo = init_repo(tmp_path / "repo")
    _open(site, repo)
    status, _, out = site.post("/api/project/pull-origin", {})
    assert (status, out) == (409, {"error": "origin is not a local folder"})


# ---------------------------------------------------------------- restart


def test_restart_answers_then_stops_serving(site, tmp_path):
    _open(site, init_repo(tmp_path / "proj"))
    status, _, out = site.post("/api/server/restart", {})
    assert (status, out) == (200, {"ok": True})
    site.thread.join(5)
    assert not site.thread.is_alive()
    assert site.host.restart_requested is True


def test_restart_without_a_project_is_fine(site):
    assert site.post("/api/server/restart", {})[0] == 200
    site.thread.join(5)
    assert site.host.restart_requested is True


def test_restart_is_refused_while_busy(site, tmp_path):
    _open(site, init_repo(tmp_path / "proj"))
    site.post(START, {"description": "a demo"})
    wait_for(lambda: state(site) == "asking")
    status, _, out = site.post("/api/server/restart", {})
    assert status == 409 and "planning session" in out["error"]
    assert site.thread.is_alive() and site.host.restart_requested is False
    assert site.get_json("/api/project")["project"] is not None


# ---------------------------------------------------------------- safety


PROJECT_POSTS = [
    ("/api/project/browse", {"path": "/tmp", "hidden": False}),
    ("/api/project/open", {"path": "/tmp"}),
    ("/api/project/init-preview", {}),
    ("/api/project/init", {}),
    ("/api/project/pull-origin", {}),
    ("/api/server/restart", {}),
]


@pytest.mark.parametrize(("path", "body"), PROJECT_POSTS)
def test_token_origin_content_type_and_json_are_checked(site, tmp_path, path, body):
    _open(site, init_repo(tmp_path / "proj"))
    before = _snapshot(tmp_path / "proj")
    assert site.post(path, body, token="wrong")[0] == 403
    assert site.post(path, body, token=None)[0] == 403
    assert site.post(path, body, origin="http://evil.example")[0] == 403
    assert site.post(path, body, origin=None)[0] == 403
    assert site.post(path, body, ctype="text/plain")[0] == 415
    assert site.post(path, None, raw=b"{nope")[0] == 400
    assert site.thread.is_alive()
    assert _snapshot(tmp_path / "proj") == before


def test_a_big_body_is_413(site):
    assert site.post("/api/project/browse", None, raw=b"x" * 70000)[0] == 413


def test_the_host_header_is_checked(site):
    status, _, _ = site.request("GET", "/api/project", headers={"Host": "evil.example"})
    assert status == 403


def test_get_on_a_post_route_is_not_served(site, tmp_path):
    _open(site, init_repo(tmp_path / "proj"))
    assert site.request("GET", "/api/project/browse")[0] == 404


# ---------------------------------------------------------------- without a Host


def test_without_a_host_the_project_routes_do_not_exist(tmp_path):
    office = Office(tmp_path, fake=True, fake_delay=0)
    plain = Site(tmp_path, office)
    try:
        assert plain.request("GET", "/api/project")[0] == 404
        for path, body in PROJECT_POSTS:
            assert plain.post(path, body)[0] == 404
    finally:
        plain.stop()
