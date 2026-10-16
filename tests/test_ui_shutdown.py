"""Ctrl-C on a real `harness ui --fake` process: it exits cleanly, closes the planner session,
frees the lock, and leaves a launched run going."""

import http.client
import json
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from git_helpers import init_repo

from harness.planner_log import read_planner_events
from harness.state import load_state, run_dir
from harness.worktree import ensure_excluded


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Ui:
    def __init__(self, repo: Path, *extra: str) -> None:
        self.repo = repo
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "harness",
                "ui",
                "--repo",
                str(repo),
                "--fake",
                "--port",
                str(self.port),
                *extra,
            ],  # fmt: skip
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.token = ""
        end = time.time() + 20
        while time.time() < end:
            try:
                _, page = self.request("GET", "/")
                self.token = re.search(r'name="harness-token" content="([^"]+)"', page).group(1)
                return
            except (OSError, AttributeError):
                time.sleep(0.1)
        self.proc.kill()
        raise AssertionError("harness ui did not start")

    def request(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if body is not None:
            headers = {
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-Harness-Token": self.token,
            }
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r.status, data

    def office(self) -> dict:
        return json.loads(self.request("GET", "/api/office")[1])

    def wait_state(self, state: str, timeout: float = 30) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            o = self.office()
            if o["state"] == state:
                return o
            time.sleep(0.1)
        raise AssertionError(f"never reached {state}; last {self.office()['state']}")

    def interrupt(self) -> str:
        self.proc.send_signal(signal.SIGINT)
        out, _ = self.proc.communicate(timeout=15)
        return out


def test_ctrl_c_while_asking_closes_the_session_and_frees_the_lock(tmp_path):
    ui = Ui(tmp_path, "--fake-delay", "0")
    try:
        assert (tmp_path / ".harness" / "office.lock").is_file()
        assert ui.request("POST", "/api/planner/start", {"description": "x"})[0] == 200
        ui.wait_state("asking")
        out = ui.interrupt()
    finally:
        if ui.proc.poll() is None:
            ui.proc.kill()
    assert ui.proc.returncode == 0, out
    assert f"Serving http://127.0.0.1:{ui.port}/ (interactive; Ctrl-C to stop)" in out
    assert "keeps running" not in out
    events = read_planner_events(tmp_path)
    assert events[-1].type == "planner_failed"
    assert events[-1].payload["reason"] == "interrupted: server stopped"
    assert not (tmp_path / ".harness" / "office.lock").exists()


def test_ctrl_c_mid_run_leaves_the_run_going_and_prints_a_hint(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_excluded(repo)
    ui = Ui(repo, "--fake-delay", "1")
    try:
        ui.request("POST", "/api/planner/start", {"description": "x"})
        ui.wait_state("asking")
        ui.request("POST", "/api/planner/message", {"text": "answers"})
        ui.wait_state("ready")
        assert ui.request("POST", "/api/planner/implement", {})[0] == 200
        run_id = ui.wait_state("supervising", 60)["run_id"]
        out = ui.interrupt()
    finally:
        if ui.proc.poll() is None:
            ui.proc.kill()
    assert ui.proc.returncode == 0, out
    assert f"Run {run_id} keeps running; check it with: harness status --repo {repo}" in out
    assert f"--run-id {run_id}" in out
    assert not (repo / ".harness" / "office.lock").exists()
    end = time.time() + 90
    state = None
    while time.time() < end:
        try:
            state = load_state(run_dir(repo, run_id))
        except Exception:
            state = None
        if state is not None and state.finished:
            break
        time.sleep(0.5)
    assert state is not None and state.finished
    assert {t.status for t in state.tasks.values()} == {"done"}
