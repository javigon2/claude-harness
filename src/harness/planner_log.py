"""The planner event log: <repo>/.harness/planner/events.jsonl.

Append-only and shared by every planner session on the repo (the office page's and
`harness plan`'s). One line per event, written with the harness's own `Event` model, where
`run_id` holds the planner session id (`planner-<timestamp>`) and `task_id` is None. A
session runs from its `planner_started` event to its `planner_done` or `planner_failed`;
a view reads only the last one."""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.models import Event
from harness.planner import (
    PROJECT_PLAN_FILE,
    PlannerSession,
    PlannerTurn,
    _fingerprint,
    is_approved,
)
from harness.state import new_run_id

PLANNER_EVENTS = Path(".harness") / "planner" / "events.jsonl"

_WRITE_LOCK = threading.Lock()


def new_session_id() -> str:
    """`planner-<new_run_id()>`, e.g. planner-20261016-093005."""
    return f"planner-{new_run_id()}"


class PlannerLog:
    """A synchronous appender for one planner session, callable from any thread. One write
    plus flush per line, like EventLog.emit, under a process-wide lock."""

    def __init__(self, repo: Path, session_id: str) -> None:
        self.path: Path = Path(repo) / PLANNER_EVENTS
        self.session_id: str = session_id

    def emit(self, type: str, payload: dict[str, Any] | None = None) -> Event:
        """Append Event(run_id=session_id, type, payload) and return it. A failed write
        (OSError) is swallowed: a log problem must not end a paid planner session."""
        event = Event(run_id=self.session_id, type=type, payload=payload or {})
        line = event.model_dump_json() + "\n"
        try:
            with _WRITE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
        except OSError:
            pass
        return event


def read_planner_events(repo: Path) -> list[Event]:
    """Every readable event in the repo's planner log, in file order. [] if there is no log.
    Blank and unparseable lines (e.g. a partial last line) are skipped."""
    try:
        text = (Path(repo) / PLANNER_EVENTS).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[Event] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(Event.model_validate_json(line))
        except ValueError:
            continue
    return events


def last_session(events: list[Event]) -> list[Event]:
    """The events of the session whose `planner_started` is the last one in `events`
    (events of other sessions interleaved with it are dropped). [] if none started."""
    for k in range(len(events) - 1, -1, -1):
        if events[k].type == "planner_started":
            sid = events[k].run_id
            return [e for e in events[k:] if e.run_id == sid]
    return []


class ObservedSession:
    """A PlannerSession wrapper that logs every turn and detects "plan ready".

    send(): logs `planner_message`, awaits the inner session, then logs `planner_reply`
    with `plan_changed` = the PROJECT_PLAN.md fingerprint changed during the turn (to a
    file that exists). If it changed, `phase_fn()` is "project" and the plan is still
    `Status: draft`, it also logs `plan_ready`. The turn is returned unchanged; close()
    delegates. `cost_usd` is the session's running cost after the latest turn."""

    def __init__(
        self,
        inner: PlannerSession,
        repo: Path,
        log: PlannerLog,
        phase_fn: Callable[[], str] = lambda: "project",
    ) -> None:
        self._inner = inner
        self._plan = Path(repo).resolve() / PROJECT_PLAN_FILE
        self._log = log
        self._phase = phase_fn
        self.cost_usd = 0.0
        self.reply_text: str | None = None
        self.ready = False

    def _approved(self) -> bool:
        try:
            return is_approved(self._plan.read_text(encoding="utf-8"))
        except OSError:
            return False

    async def send(self, text: str) -> PlannerTurn:
        self._log.emit("planner_message", {"chars": len(text), "phase": self._phase()})
        before = _fingerprint(self._plan)
        turn = await self._inner.send(text)
        after = _fingerprint(self._plan)
        changed = after not in (None, before)
        self.cost_usd = turn.usage.cost_usd
        self.reply_text = turn.text
        self.ready = False
        self._log.emit(
            "planner_reply",
            {"text": turn.text, "plan_changed": changed, "cost_usd": self.cost_usd},
        )
        if changed and self._phase() == "project" and not self._approved():
            self.ready = True
            self._log.emit(
                "plan_ready",
                {"path": PROJECT_PLAN_FILE.as_posix(), "cost_usd": self.cost_usd},
            )
        return turn

    async def close(self) -> None:
        await self._inner.close()
