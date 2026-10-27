"""An on-demand run of the open project's test suite, started from the page's Run tests
button. Agents never run the whole suite; this is the only place it runs."""

import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

TAIL_LINES = 200
TIMEOUT_S = 1800.0
NO_TESTS = "this project has no pyproject.toml, so there is no test command to run"


class TestRunConflict(RuntimeError):
    """A test run cannot start. The message says why."""

    __test__ = False


def suite_command(repo: Path) -> str | None:
    """The command that runs the whole suite of the project at `repo`, or None if there is
    none. `HARNESS_TEST_COMMAND` overrides it; otherwise a project with a pyproject.toml gets
    `uv run pytest -q`, with `-n auto` when pytest-xdist is listed there."""
    override = os.environ.get("HARNESS_TEST_COMMAND", "").strip()
    if override:
        return override
    pyproject = repo / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    return "uv run pytest -q -n auto" if "pytest-xdist" in text else "uv run pytest -q"


class TestRunner:
    """One test run at a time for one repo. start() returns at once; view() is the state."""

    __test__ = False

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)
        self._lock = threading.Lock()
        self._state: dict = {"status": "idle", "command": None, "exit_code": None}
        self._tail: deque[str] = deque(maxlen=TAIL_LINES)
        self._started = 0.0
        self._duration_s: float | None = None

    def view(self) -> dict:
        with self._lock:
            running = self._state["status"] == "running"
            duration = time.monotonic() - self._started if running else self._duration_s
            lines = list(self._tail)
            summary = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
            return {
                **self._state,
                "duration_s": None if duration is None else round(duration, 1),
                "summary": summary,
                "output": "\n".join(lines),
            }

    def start(self) -> dict:
        """Start the suite in the background. Raises TestRunConflict if it is already running
        or the project has no test command."""
        command = suite_command(self.repo)
        if command is None:
            raise TestRunConflict(NO_TESTS)
        with self._lock:
            if self._state["status"] == "running":
                raise TestRunConflict("the tests are already running")
            self._state = {"status": "running", "command": command, "exit_code": None}
            self._tail.clear()
            self._started = time.monotonic()
            self._duration_s = None
        threading.Thread(target=self._run, args=(command,), daemon=True).start()
        return self.view()

    def _finish(self, code: int | None) -> None:
        with self._lock:
            self._duration_s = time.monotonic() - self._started
            self._state = {
                **self._state,
                "status": "passed" if code == 0 else "failed",
                "exit_code": code,
            }

    def _run(self, command: str) -> None:
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=env,
                start_new_session=True,
            )
        except OSError as err:
            with self._lock:
                self._tail.append(f"failed to start: {err}")
            self._finish(None)
            return

        def kill() -> None:
            with self._lock:
                self._tail.append(f"[stopped after {TIMEOUT_S:.0f} s]")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        timer = threading.Timer(TIMEOUT_S, kill)
        timer.daemon = True
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                with self._lock:
                    self._tail.append(line.rstrip("\n"))
            code = proc.wait()
        finally:
            timer.cancel()
        self._finish(code)


_runners: dict[Path, TestRunner] = {}
_runners_lock = threading.Lock()


def runner_for(repo: Path) -> TestRunner:
    """The one TestRunner of `repo`, made on first use."""
    key = Path(repo).resolve()
    with _runners_lock:
        if key not in _runners:
            _runners[key] = TestRunner(key)
        return _runners[key]
