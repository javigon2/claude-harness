"""The on-demand test run: TestRunner and POST /api/tests/run, GET /api/tests."""

import time

import pytest
from git_helpers import init_repo
from test_ui_office_server import Site

from harness.ui.office import Office
from harness.ui.testrun import NO_TESTS, TestRunConflict, TestRunner, suite_command


def _wait(runner: TestRunner, status: str) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        view = runner.view()
        if view["status"] == status:
            return view
        time.sleep(0.05)
    raise AssertionError(f"never reached {status}: {runner.view()}")


def test_command_needs_a_pyproject(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TEST_COMMAND", raising=False)
    assert suite_command(tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert suite_command(tmp_path) == "uv run pytest -q"
    (tmp_path / "pyproject.toml").write_text('dev = ["pytest-xdist"]\n')
    assert suite_command(tmp_path) == "uv run pytest -q -n auto"


def test_no_test_command_is_a_conflict(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TEST_COMMAND", raising=False)
    with pytest.raises(TestRunConflict, match=NO_TESTS):
        TestRunner(tmp_path).start()


def test_passing_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_COMMAND", "echo one; echo 3 passed in 0.1s")
    runner = TestRunner(tmp_path)
    assert runner.view()["status"] == "idle"
    assert runner.start()["status"] == "running"
    view = _wait(runner, "passed")
    assert view["exit_code"] == 0
    assert view["summary"] == "3 passed in 0.1s"
    assert view["output"] == "one\n3 passed in 0.1s"


def test_failing_run_and_a_second_start_while_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_COMMAND", "sleep 0.5; echo boom; exit 3")
    runner = TestRunner(tmp_path)
    runner.start()
    with pytest.raises(TestRunConflict, match="already running"):
        runner.start()
    view = _wait(runner, "failed")
    assert view["exit_code"] == 3
    assert "boom" in view["output"]
    runner.start()  # allowed again once finished
    _wait(runner, "failed")


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_COMMAND", "echo 1 passed")
    init_repo(tmp_path)
    site = Site(tmp_path, Office(tmp_path, fake=True, fake_delay=0))
    try:
        assert site.get_json("/api/tests")["status"] == "idle"
        status, _, out = site.post("/api/tests/run", {}, token="bad")
        assert status == 403
        status, _, out = site.post("/api/tests/run", {"x": 1})
        assert status == 400
        status, _, out = site.post("/api/tests/run", {})
        assert status == 200 and out["tests"]["status"] in ("running", "passed")
        deadline = time.monotonic() + 20
        while site.get_json("/api/tests")["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert site.get_json("/api/tests")["status"] == "passed"
    finally:
        site.stop()
