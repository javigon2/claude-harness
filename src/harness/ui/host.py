"""The Host: the launcher's server owns it and it owns the one Office of the open project.

With no project open there is no Office; opening a folder builds one (and closes the old one),
which is what the page's "Open" and "Switch project" do. Switching, and restarting the server,
are refused while an agent is working, so no session is cut off."""

import threading
import time
from collections.abc import Callable
from pathlib import Path

from harness.ui.office import CHAT_WORKING, Office, OfficeError
from harness.ui.projects import default_recent_path, remember
from harness.worktree import WorktreeError, ensure_excluded

NO_PROJECT = "no project is open; pick a folder first"
PLANNING_BUSY = "a planning session is open; finish it first"
ROUTING_BUSY = "the orchestrator is routing a message"


class HostConflict(Exception):
    """The request does not fit the Host's state. The message is a sentence for the user; the
    server answers 409 with it."""


class Host:
    """One per server. `current()` is the open project and its Office (None, None before a
    project is opened); `open_project` switches; `request_restart` marks the server for a
    restart, which the launcher carries out after `serve_forever` returns.

    `port` goes into each Office's lock file. `office_factory(path)` builds the Office of a
    project (tests use it to capture launches); by default it is
    Office(path, fake=fake, fake_delay=fake_delay, max_cost=max_cost). Recent projects are
    kept in `recent_path` (default: default_recent_path())."""

    def __init__(
        self,
        *,
        port: int,
        cwd: Path,
        fake: bool = False,
        fake_delay: float = 2.0,
        max_cost: float | None = None,
        recent_path: Path | None = None,
        office_factory: Callable[[Path], Office] | None = None,
    ) -> None:
        self.port = port
        self.cwd = Path(cwd)
        self.fake = fake
        self.fake_delay = fake_delay
        self.max_cost = max_cost
        self.recent_path = Path(recent_path) if recent_path is not None else default_recent_path()
        self.started: float = time.time()
        self.restart_requested = False
        self._factory: Callable[[Path], Office] = office_factory or (
            lambda path: Office(path, fake=fake, fake_delay=fake_delay, max_cost=max_cost)
        )
        self._lock = threading.Lock()
        self._project: Path | None = None
        self._office: Office | None = None

    def current(self) -> tuple[Path | None, Office | None]:
        """(project folder, its Office), or (None, None)."""
        with self._lock:
            return self._project, self._office

    def busy(self) -> str | None:
        """Why the project cannot be switched or the server restarted now, or None: a
        planning session in progress, the Orchestrator routing a message, or a chat working."""
        _project, office = self.current()
        if office is None:
            return None
        if office.boss_status()[1] in ("busy", "asking", "ready", "staging"):
            return PLANNING_BUSY
        if office.orchestrator.view().state == "routing":
            return ROUTING_BUSY
        if office.chats.working():
            return CHAT_WORKING
        return None

    def _build(self, path: Path) -> Office:
        """A new Office for `path`, opened (lock taken). Raises HostConflict if the folder
        is owned by another harness ui."""
        try:
            ensure_excluded(path)
        except (WorktreeError, OSError):
            pass  # not a git repo yet (Initialize comes first), or not writable
        office = self._factory(path)
        try:
            office.open(self.port)
        except OfficeError as err:
            office.close()
            raise HostConflict(str(err)) from err
        return office

    def open_project(self, path: Path) -> None:
        """Make `path` the open project: close the current Office, build and open one for
        `path`, remember it. Raises HostConflict if `path` is not a folder, the Host is busy
        or another harness ui owns the folder; the previous project then stays open."""
        path = Path(path)
        if not path.is_dir():
            raise HostConflict(f"not a folder: {path}")
        with self._lock:
            old_project, old_office = self._project, self._office
        reason = self.busy()
        if reason is not None:
            raise HostConflict(reason)
        with self._lock:
            if (self._project, self._office) != (old_project, old_office):
                raise HostConflict("the project changed while opening; try again")
            if old_office is not None:
                old_office.close()
                self._project = self._office = None
            try:
                office = self._build(path)
            except HostConflict:
                if old_project is not None and old_project.is_dir():
                    try:
                        self._office = self._build(old_project)
                        self._project = old_project
                    except HostConflict:
                        pass
                raise
            self._project, self._office = path, office
        remember(self.recent_path, path)

    def request_restart(self) -> None:
        """Mark the server for a restart. Raises HostConflict while busy."""
        reason = self.busy()
        if reason is not None:
            raise HostConflict(reason)
        self.restart_requested = True

    def close(self) -> str | None:
        """Close the Office (planner sessions stop, launched runs keep going). Returns the id
        of a run that is still going, or None."""
        with self._lock:
            office, self._office, self._project = self._office, None, None
        return office.close() if office is not None else None
