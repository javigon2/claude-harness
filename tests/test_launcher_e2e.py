"""The launcher's loop over HTTP, with real `harness run` subprocesses on fake workers: browse,
open a plain folder, Initialize, launch a two-wave plan, stop it, resume it, read its report,
list the plans, restart. No model is called."""

import json
import threading
import time
from pathlib import Path

import pytest
from test_ui_office_server import Site

from harness.state import load_state, run_dir
from harness.ui.demo import demo_stage_plan
from harness.ui.host import Host
from harness.ui.server import make_server

STAGE1 = ".harness/plans/stage-1/plan.json"


class LauncherSite(Site):
    """The launcher's server: a Host with fake workers (0.5 s a step) and no project open."""

    def __init__(self, cwd: Path, recent: Path) -> None:
        self.repo, self.office = cwd, None
        self.host = Host(port=0, cwd=cwd, fake=True, fake_delay=0.5, recent_path=recent)
        self.server = make_server(cwd, 0, poll_interval=0.05, heartbeat_s=0.2, host=self.host)
        self.host.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.host.close()


def wait_for(check, timeout: float = 60.0, what: str = "the condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def site(tmp_path, monkeypatch):
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "T")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    s = LauncherSite(cwd, tmp_path / "cfg" / "recent.json")
    yield s
    s.stop()


def test_the_whole_loop(site, tmp_path):
    # no project yet
    assert site.get_json("/api/project")["project"] is None

    # browse to the folder and open it
    project = tmp_path / "work" / "proj"
    project.mkdir(parents=True)
    (project / "a.txt").write_text("a\n")
    status, _, listing = site.post("/api/project/browse", {"path": str(tmp_path), "hidden": False})
    assert status == 200 and "work" in [e["name"] for e in listing["entries"]]
    status, _, listing = site.post(
        "/api/project/browse", {"path": str(project.parent), "hidden": False}
    )
    assert [e["path"] for e in listing["entries"]] == [str(project)]
    status, _, out = site.post("/api/project/open", {"path": str(project)})
    assert status == 200 and out["project"]["is_git"] is False

    # Initialize: files first, then the commit
    status, _, out = site.post("/api/project/init-preview", {})
    assert (status, out["files"]) == (200, ["a.txt"])
    status, _, out = site.post("/api/project/init", {})
    assert status == 200 and out["project"]["on_main"] is True and out["project"]["has_commit"]
    assert site.get_json("/api/project")["recent"] == [str(project)]

    # a two-wave plan
    plan = project / STAGE1
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps(demo_stage_plan(1)))
    data = site.get_json("/api/plans")
    (entry,) = data["plans"]
    assert (entry["path"], entry["orders"], entry["waves"]) == (STAGE1, 3, 2)
    assert entry["last_run"] is None

    # launch, then stop while a task is running
    assert site.post("/api/run/launch", {"plan_path": STAGE1})[0] == 200
    run_id = wait_for(lambda: site.get_json("/api/office")["run_id"], what="the run id")

    def running() -> bool:
        status, _, body = site.request("GET", f"/api/run/{run_id}")  # 404 until state.json exists
        return status == 200 and any(t["status"] == "running" for t in json.loads(body)["tasks"])

    wait_for(running, what="a running task")
    assert site.get_json("/api/office")["stoppable_run"] == run_id
    assert site.post("/api/run/stop", {})[0] == 200
    wait_for(lambda: load_state(run_dir(project, run_id)).finished, what="the run to stop")
    state = load_state(run_dir(project, run_id))
    assert any(t.status == "paused" for t in state.tasks.values())
    assert state.tasks["demo-c"].status == "pending"
    office = wait_for(
        lambda: (o := site.get_json("/api/office"))["state"] == "idle" and o,
        what="the office to go idle",
    )
    assert office["resumable_run"] == run_id and office["stoppable_run"] is None
    assert site.get_json("/api/plans")["plans"][0]["last_status"] == "stopped"

    # resume to the end
    assert site.post("/api/run/resume", {})[0] == 200
    wait_for(
        lambda: (
            all(t.status == "done" for t in load_state(run_dir(project, run_id)).tasks.values())
            and load_state(run_dir(project, run_id)).finished
        ),
        what="the resumed run to finish",
    )

    # the report and the plans list
    report = site.get_json(f"/api/report/{run_id}")
    assert report["run_id"] == run_id and "demo-c" in report["status"] and "$" in report["report"]
    wait_for(lambda: site.get_json("/api/plans")["plans"][0]["last_status"] == "done")
    assert site.get_json("/api/plans")["plans"][0]["last_run"] == run_id

    # restart ends serve_forever once the office is idle
    wait_for(lambda: site.get_json("/api/project")["busy"] is None, what="an idle host")
    assert site.post("/api/server/restart", {})[0] == 200
    site.thread.join(10)
    assert not site.thread.is_alive() and site.host.restart_requested is True
