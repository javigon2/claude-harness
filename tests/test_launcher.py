import socket
import sys
from pathlib import Path

import pytest
from git_helpers import init_repo
from typer.testing import CliRunner

from harness import launcher
from harness.launcher import (
    LauncherError,
    app,
    pick_port,
    restart_argv,
    startup_project,
)
from harness.planner import PlannerError
from harness.ui.host import Host

runner = CliRunner()


# ---------------------------------------------------------------- helpers


def _free_pair() -> int:
    """A port p with p and p + 1 both free."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if launcher._port_free(p) and launcher._port_free(p + 1):
            return p
    raise AssertionError("no free port pair")


def test_pick_port_uses_the_preferred_port_as_is():
    assert pick_port(9123) == 9123


def test_pick_port_takes_the_first_free_port_in_the_range(monkeypatch):
    p = _free_pair()
    monkeypatch.setattr(launcher, "PORT_RANGE", range(p, p + 2))
    assert pick_port(None) == p
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", p))
        busy.listen()
        assert pick_port(None) == p + 1
        with socket.socket() as busy2:
            busy2.bind(("127.0.0.1", p + 1))
            busy2.listen()
            with pytest.raises(LauncherError) as err:
                pick_port(None)
    assert str(err.value) == f"No free port in {p}-{p + 1}"


def test_the_default_range_is_8765_to_8775():
    assert list(launcher.PORT_RANGE) == list(range(8765, 8776))


def test_startup_project(tmp_path):
    repo = init_repo(tmp_path / "repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert startup_project(None, repo) == repo
    assert startup_project(None, plain) is None
    assert startup_project(repo, plain) == repo
    assert startup_project(Path("repo"), tmp_path) == repo
    assert startup_project(Path("../repo"), plain) == repo
    assert startup_project(Path("nope"), tmp_path) == tmp_path / "nope"  # open fails later


def test_restart_argv(tmp_path):
    assert restart_argv(8766, None, False, 2.0, None) == [
        sys.executable, "-m", "harness.launcher", "--port", "8766", "--no-browser",
    ]  # fmt: skip
    assert restart_argv(8766, tmp_path, True, 0.5, 2.5) == [
        sys.executable, "-m", "harness.launcher", "--port", "8766", "--no-browser",
        "--project", str(tmp_path), "--fake", "--fake-delay", "0.5", "--max-cost", "2.5",
    ]  # fmt: skip


# ---------------------------------------------------------------- main


class FakeServer:
    def __init__(self, interrupt: bool = False, on_serve=None) -> None:
        self.interrupt, self.on_serve = interrupt, on_serve
        self.served = self.closed = False
        self.kwargs: dict = {}

    def serve_forever(self) -> None:
        self.served = True
        if self.on_serve is not None:
            self.on_serve()
        if self.interrupt:
            raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Patched browser, execv, server and skills check; cwd is a plain folder."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = {"opened": [], "exec": [], "server": FakeServer(), "made": [], "skills": 0}

    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: state["opened"].append(url))
    monkeypatch.setattr(launcher.os, "execv", lambda exe, argv: state["exec"].append((exe, argv)))

    def make_server(repo, port, **kwargs):
        state["made"].append((repo, port, kwargs))
        return state["server"]

    monkeypatch.setattr(launcher, "make_server", make_server)
    monkeypatch.setattr(
        launcher, "check_skills", lambda: state.__setitem__("skills", state["skills"] + 1)
    )
    state["cwd"] = cwd
    return state


def test_serves_a_repo_cwd_and_opens_the_browser(env, tmp_path):
    repo = init_repo(env["cwd"])
    result = runner.invoke(app, ["--fake", "--fake-delay", "0", "--port", "8799"])

    assert result.exit_code == 0, result.output
    assert f"Serving http://127.0.0.1:8799/ (project: {repo}; Ctrl-C to stop)" in result.output
    assert env["opened"] == ["http://127.0.0.1:8799/"]
    (repo_arg, port, kwargs) = env["made"][0]
    assert (repo_arg, port) == (repo, 8799)
    assert isinstance(kwargs["host"], Host) and kwargs["token"]
    assert env["server"].served and env["server"].closed
    assert not (repo / ".harness" / "office.lock").exists()  # the Host was closed
    assert env["exec"] == [] and env["skills"] == 0  # fake: no skills check


def test_no_project_in_a_plain_folder_and_no_browser_flag(env):
    result = runner.invoke(app, ["--fake", "--no-browser", "--port", "8799"])
    assert result.exit_code == 0, result.output
    assert "Serving http://127.0.0.1:8799/ (project: none; Ctrl-C to stop)" in result.output
    assert env["opened"] == []


def test_project_flag_opens_that_folder(env, tmp_path):
    other = init_repo(tmp_path / "other")
    result = runner.invoke(
        app, ["--fake", "--no-browser", "--port", "8799", "--project", str(other)]
    )
    assert result.exit_code == 0, result.output
    assert f"(project: {other}; Ctrl-C to stop)" in result.output
    host = env["made"][0][2]["host"]
    assert host.fake is True


def test_a_project_that_cannot_be_opened_is_reported_and_none_is_used(env, tmp_path):
    result = runner.invoke(
        app, ["--fake", "--no-browser", "--port", "8799", "--project", str(tmp_path / "nope")]
    )
    assert result.exit_code == 0, result.output
    assert f"Cannot open {tmp_path / 'nope'}: not a folder: {tmp_path / 'nope'}" in result.output
    assert "(project: none; Ctrl-C to stop)" in result.output


def test_default_port_is_the_first_free_one(env, monkeypatch):
    monkeypatch.setattr(
        launcher, "pick_port", lambda preferred: 8770 if preferred is None else preferred
    )
    result = runner.invoke(app, ["--fake", "--no-browser"])
    assert "Serving http://127.0.0.1:8770/" in result.output


def test_no_free_port_exits_1(env, monkeypatch):
    def none_free(preferred):
        raise LauncherError("No free port in 8765-8775")

    monkeypatch.setattr(launcher, "pick_port", none_free)
    result = runner.invoke(app, ["--fake", "--no-browser"])
    assert result.exit_code == 1
    assert "No free port in 8765-8775" in result.output
    assert env["made"] == []


def test_a_port_that_cannot_be_bound_exits_1(env, monkeypatch):
    def refuse(repo, port, **kwargs):
        raise OSError("Address already in use")

    monkeypatch.setattr(launcher, "make_server", refuse)
    result = runner.invoke(app, ["--fake", "--no-browser", "--port", "8799"])
    assert result.exit_code == 1
    assert "Cannot listen on 127.0.0.1:8799: Address already in use" in result.output


def test_real_mode_checks_the_skills_and_stops_without_them(env, monkeypatch):
    result = runner.invoke(app, ["--no-browser", "--port", "8799"])
    assert result.exit_code == 0 and env["skills"] == 1

    def missing():
        raise PlannerError("planner skills not found under /x/.claude/skills")

    monkeypatch.setattr(launcher, "check_skills", missing)
    result = runner.invoke(app, ["--no-browser", "--port", "8799"])
    assert result.exit_code == 1
    assert "planner skills not found" in result.output


def test_flag_checks(env):
    r = runner.invoke(app, ["--fake-delay", "1"])
    assert r.exit_code == 1 and "--fake-delay requires --fake" in r.output
    r = runner.invoke(app, ["--fake", "--max-cost", "0"])
    assert r.exit_code == 1 and "--max-cost must be greater than 0" in r.output
    assert env["made"] == []


def test_ctrl_c_with_a_live_run_prints_the_hint(env, monkeypatch, tmp_path):
    repo = init_repo(env["cwd"])
    env["server"] = FakeServer(interrupt=True)
    monkeypatch.setattr(Host, "close", lambda self: "r1")
    result = runner.invoke(app, ["--fake", "--no-browser", "--port", "8799"])
    assert result.exit_code == 0, result.output
    assert (
        f"Run r1 keeps running; check it in the page or with: harness status --repo {repo} "
        "--run-id r1"
    ) in result.output


def test_ctrl_c_without_a_run_says_nothing_more(env):
    init_repo(env["cwd"])
    env["server"] = FakeServer(interrupt=True)
    result = runner.invoke(app, ["--fake", "--no-browser", "--port", "8799"])
    assert result.exit_code == 0 and "keeps running" not in result.output


def test_a_requested_restart_execs_the_launcher_again(env):
    repo = init_repo(env["cwd"])
    host_box: list[Host] = []

    def ask_restart():
        host_box.append(env["made"][0][2]["host"])
        host_box[0].request_restart()

    env["server"] = FakeServer(on_serve=ask_restart)
    result = runner.invoke(
        app, ["--fake", "--fake-delay", "0.5", "--no-browser", "--port", "8799", "--max-cost", "3"]
    )

    assert result.exit_code == 0, result.output
    assert env["exec"] == [
        (
            sys.executable,
            restart_argv(8799, repo, True, 0.5, 3.0),
        )
    ]
    assert "--project" in env["exec"][0][1] and "--no-browser" in env["exec"][0][1]


def test_main_runs_the_app(env, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["agent-orchestrator", "--fake", "--no-browser", "--port", "8799"]
    )
    with pytest.raises(SystemExit) as stop:
        launcher.main()
    assert stop.value.code == 0
    assert env["server"].served


def test_the_script_is_registered():
    text = (Path(launcher.__file__).parents[2] / "pyproject.toml").read_text()
    assert 'agent-orchestrator = "harness.launcher:main"' in text
    assert 'harness = "harness.cli:app"' in text
