"""The office: the boss's state machine behind the interactive page.

One `Office` per server owns at most one planner session. The session runs `run_planner` on
a dedicated thread with its own asyncio loop; questions and answers cross over a queue. Every
transition is also written to the planner event log, so a restart (or a read-only page) can
rebuild the boss's state with `office_view_from_log`. All state changes happen under one lock.

States: idle, busy, asking, ready, staging, supervising, interrupted, failed, terminal
(a `harness plan` is busy in another process)."""

import asyncio
import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.events import EVENTS_FILE
from harness.models import Event
from harness.planner import (
    PROJECT_PLAN_FILE,
    PlannerError,
    PlannerSession,
    PlanResult,
    SdkPlannerSession,
    run_planner,
)
from harness.planner_log import (
    ObservedSession,
    PlannerLog,
    last_session,
    new_session_id,
    read_planner_events,
)
from harness.state import STATE_FILE, StateError, load_state, run_dir
from harness.ui.demo import DemoPlannerSession
from harness.ui.markdown import parse_markdown
from harness.ui.view import IDLE_MINUTES, list_runs
from harness.worker import ProgressFn

LOCK_FILE = Path(".harness") / "office.lock"
MAX_TEXT_CHARS = 20_000
PROGRESS_LINES = 8
JOIN_TIMEOUT_S = 10.0

State = Literal[
    "idle",
    "busy",
    "asking",
    "ready",
    "staging",
    "supervising",
    "interrupted",
    "failed",
    "terminal",
]

SessionFactory = Callable[[Path, ProgressFn], PlannerSession]

_STATUS_RE = re.compile(r"^Status: (draft|approved) ?(?:\d{4}-\d{2}-\d{2})?$", re.MULTILINE)

RESUME_PROMPT = (
    "An earlier planning session was interrupted. The current draft is {project_plan}; "
    "read it first and continue planning from it with the user. "
    "Original description:\n\n{description}"
)


class OfficeError(RuntimeError):
    """The office cannot start (e.g. another `harness ui` owns the repo)."""


class OfficeConflict(Exception):
    """The request does not fit the office's state. The message is a sentence for the user;
    the server answers 409 with it."""


class OfficeView(BaseModel):
    """What the page shows for the boss. `reply_blocks` is `parse_markdown(reply)`."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["interactive", "read-only"]
    state: State
    session_id: str | None = None
    stage: int | None = None
    progress: list[str] = Field(default_factory=list)
    reply: str | None = None
    reply_blocks: list[dict[str, Any]] = Field(default_factory=list)
    plan_exists: bool = False
    plan_status: Literal["draft", "approved"] | None = None
    cost_usd: float = 0.0
    error: str | None = None
    run_id: str | None = None
    next_stage: int | None = None
    can_click: bool = False


# ---------------------------------------------------------------- small helpers


def plan_status(repo: Path) -> Literal["draft", "approved"] | None:
    """The `Status:` of PROJECT_PLAN.md ("draft" or "approved"), None if the file or the line
    is missing."""
    try:
        text = (Path(repo) / PROJECT_PLAN_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    m = _STATUS_RE.search(text)
    if m is None:
        return None
    return "draft" if m.group(1) == "draft" else "approved"


def pid_alive(pid: object) -> bool:
    """True if a process with this pid exists (signal 0). False for anything that is not a
    positive int."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_run(repo: Path, now: float | None = None) -> str | None:
    """The newest run in `repo` that is still going: state.json has finished == False and
    events.jsonl (or state.json) was written less than IDLE_MINUTES ago. A run started from a
    terminal counts. None if there is none."""
    t = time.time() if now is None else now
    for run_id in list_runs(repo):
        directory = run_dir(repo, run_id)
        try:
            written = (directory / EVENTS_FILE).stat().st_mtime
        except OSError:
            try:
                written = (directory / STATE_FILE).stat().st_mtime
            except OSError:
                continue
        if t - written >= IDLE_MINUTES * 60:
            continue
        try:
            if not load_state(directory).finished:
                return run_id
        except (StateError, OSError, ValueError):
            continue
    return None


def _duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{seconds:g} s"


def _reason_note(reason: str, idle_s: object = None) -> str:
    """The sentence the page shows for a planner_failed reason."""
    if reason == "idle timeout":
        after = _duration(float(idle_s)) if isinstance(idle_s, int | float) else "a long time"
        return f"Session closed after {after} without a message; the draft is kept."
    if reason.startswith("interrupted"):
        return "The planning session was interrupted; the draft is kept."
    return reason


def _is_interruption(reason: str) -> bool:
    return reason.startswith("interrupted") or reason == "idle timeout"


def _plan_fields(repo: Path) -> dict[str, Any]:
    status = plan_status(repo)
    return {"plan_exists": (repo / PROJECT_PLAN_FILE).is_file(), "plan_status": status}


def _progress_line(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool", "?"))
    detail = payload.get("detail")
    return f"{tool}: {detail}" if isinstance(detail, str) and detail else tool


def _terminal_live(sess: list[Event]) -> bool:
    if not sess or sess[0].payload.get("source") != "terminal":
        return False
    if any(e.type in ("planner_done", "planner_failed") for e in sess):
        return False
    return pid_alive(sess[0].payload.get("pid"))


def office_view_from_log(repo: Path, *, interactive: bool = False) -> OfficeView:
    """The boss's state rebuilt from the planner event log and the run files, without
    touching anything. Used by read-only mode and on restart.

    A live run (even one started in a terminal) is `supervising`. Otherwise the last
    session decides: ended by planner_failed -> interrupted (reason starts with
    "interrupted" or is "idle timeout") or failed; ended by planner_done -> idle; not ended
    with a dead pid -> interrupted; not ended with a live `harness plan` -> terminal; not
    ended with a live office pid -> busy/staging, asking or ready from its last events.
    `can_click` is true only in interactive mode and never for `terminal`."""
    repo = Path(repo)
    fields: dict[str, Any] = {
        "mode": "interactive" if interactive else "read-only",
        **_plan_fields(repo),
    }
    sess = last_session(read_planner_events(repo))
    if sess:
        started = sess[0].payload
        fields["session_id"] = sess[0].run_id
        stage = started.get("stage")
        fields["stage"] = stage if isinstance(stage, int) else None
        replies = [e for e in sess if e.type == "planner_reply"]
        if replies:
            fields["cost_usd"] = float(replies[-1].payload.get("cost_usd") or 0.0)
            text = replies[-1].payload.get("text")
            if isinstance(text, str) and text:
                fields["reply"] = text
                fields["reply_blocks"] = parse_markdown(text)
    state: State = "idle"
    live = live_run(repo)
    ended = next((e for e in reversed(sess) if e.type in ("planner_done", "planner_failed")), None)
    if live is not None:
        state = "supervising"
        fields["run_id"] = live
    elif ended is not None and ended.type == "planner_failed":
        reason = str(ended.payload.get("reason") or "planner failed")
        state = "interrupted" if _is_interruption(reason) else "failed"
        fields["error"] = _reason_note(reason, ended.payload.get("idle_s"))
    elif sess and ended is None:
        pid = sess[0].payload.get("pid")
        if not pid_alive(pid):
            state = "interrupted"
            fields["error"] = "The planning session was interrupted; the draft is kept."
        elif sess[0].payload.get("source") == "terminal":
            state = "terminal"
        else:
            state = _live_office_state(sess, fields)
    fields["state"] = state
    fields["can_click"] = interactive and state != "terminal"
    return OfficeView(**fields)


def _live_office_state(sess: list[Event], fields: dict[str, Any]) -> State:
    """busy/staging, asking or ready for a session another process is running, from its last
    message and reply."""
    k = next(
        (
            i
            for i in range(len(sess) - 1, -1, -1)
            if sess[i].type in ("planner_message", "planner_reply")
        ),
        None,
    )
    if k is None or sess[k].type == "planner_message":
        if k is not None and sess[k].payload.get("phase") == "staging":
            return "staging"
        fields["progress"] = [
            _progress_line(e.payload) for e in sess[k or 0 :] if e.type == "planner_progress"
        ][-PROGRESS_LINES:]
        return "busy"
    return "ready" if any(e.type == "plan_ready" for e in sess[k:]) else "asking"


# ---------------------------------------------------------------- the office


class Office:
    """Owns the one planner session of a `harness ui` server.

    Public calls: `start`, `message`, `implement`, `next_stage` (each raises OfficeConflict
    with a sentence when the state does not allow it), `view`, `open`, `close`. `fake` swaps
    in the scripted DemoPlannerSession; `session_factory(repo, progress)` overrides both.
    `launcher` replaces subprocess.Popen when a run is started (tests capture argv)."""

    def __init__(
        self,
        repo: Path,
        *,
        fake: bool = False,
        fake_delay: float = 2.0,
        max_cost: float | None = None,
        session_factory: SessionFactory | None = None,
        idle_timeout_s: float = 7200.0,
        launcher: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.fake = fake
        self.fake_delay = fake_delay
        self.max_cost = max_cost
        self.idle_timeout_s = idle_timeout_s
        self._launcher = launcher
        self._clock = clock
        if session_factory is not None:
            self._factory: SessionFactory = session_factory
        elif fake:
            self._factory = lambda repo, progress: DemoPlannerSession(
                repo, progress=progress, delay_s=fake_delay
            )
        else:
            self._factory = lambda repo, progress: SdkPlannerSession(repo, progress=progress)

        self._lock = threading.Lock()
        self._state: State = "idle"
        self._phase: Literal["project", "staging"] = "project"
        self._session_id: str | None = None
        self._stage: int | None = None
        self._error: str | None = None
        self._reply: str | None = None
        self._progress_lines: list[str] = []
        self._cost = 0.0
        self._run_id: str | None = None
        self._log: PlannerLog | None = None
        self._observed: ObservedSession | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._stopping = threading.Event()
        self._waiting = False
        self._last_activity = 0.0
        self._end_reason: str | None = None
        self._owns_lock_file = False

    # ------------------------------------------------------------ lock file

    @property
    def _lock_path(self) -> Path:
        return self.repo / LOCK_FILE

    def open(self, port: int = 0) -> None:
        """Take `.harness/office.lock` ({pid, port, started}). Raises OfficeError if another
        live process owns it; a lock whose pid is dead is taken over."""
        path = self._lock_path
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = None
        if isinstance(held, dict) and pid_alive(held.get("pid")):
            raise OfficeError(
                f"Another harness ui (pid {held.get('pid')}, port {held.get('port')}) owns "
                "this repo; stop it or use --read-only"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": os.getpid(), "port": port, "started": self._clock()}) + "\n",
            encoding="utf-8",
        )
        self._owns_lock_file = True

    def close(self) -> str | None:
        """Shut down: stop a live planner session (logged as interrupted: server stopped,
        joined for up to JOIN_TIMEOUT_S), never touch a launched run, remove the lock file.
        Returns the id of a run that is still going, or None."""
        self._stopping.set()
        thread = self._thread
        live = thread is not None and thread.is_alive()
        waiting = False
        if live:
            with self._lock:
                waiting = self._waiting
                if self._end_reason is None and self._log is not None:
                    self._end_reason = "interrupted: server stopped"
                    self._log.emit(
                        "planner_failed",
                        {"reason": self._end_reason, "cost_usd": self._current_cost()},
                    )
            self._answers.put(None)
            if not waiting and self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass
            assert thread is not None
            thread.join(JOIN_TIMEOUT_S)
        if self._owns_lock_file:
            try:
                self._lock_path.unlink()
            except OSError:
                pass
            self._owns_lock_file = False
        with self._lock:
            return self._run_id if self._run_id and self._run_alive(self._run_id) else None

    # ------------------------------------------------------------ view

    def _current_cost(self) -> float:
        return self._observed.cost_usd if self._observed is not None else self._cost

    def _run_alive(self, run_id: str) -> bool:
        try:
            return not load_state(run_dir(self.repo, run_id)).finished
        except (StateError, OSError, ValueError):
            return False

    def view(self) -> OfficeView:
        """The boss's state now. Idle, failed and interrupted give way to `supervising`
        while a run is live (also one started in a terminal) and to `terminal` while a
        `harness plan` is busy elsewhere."""
        with self._lock:
            state, run_id, error = self._state, self._run_id, self._error
            if state in ("idle", "failed", "interrupted"):
                live = live_run(self.repo, self._clock())
                if live is not None:
                    state, run_id = "supervising", live
                elif _terminal_live(last_session(read_planner_events(self.repo))):
                    state = "terminal"
            reply = self._reply
            return OfficeView(
                mode="interactive",
                state=state,
                session_id=self._session_id,
                stage=self._stage,
                progress=list(self._progress_lines),
                reply=reply,
                reply_blocks=parse_markdown(reply) if reply else [],
                cost_usd=self._current_cost(),
                error=error,
                run_id=run_id,
                can_click=state != "terminal",
                **_plan_fields(self.repo),
            )

    # ------------------------------------------------------------ guards

    def _guard_idle(self) -> None:
        """Raise OfficeConflict unless a new planner session may start. Caller holds the lock."""
        if self._state in ("busy", "asking", "ready", "staging"):
            raise OfficeConflict("a planning session is already in progress")
        live = live_run(self.repo, self._clock())
        if live is not None:
            raise OfficeConflict(f"a run is active ({live}); wait for it to finish")
        sess = last_session(read_planner_events(self.repo))
        if _terminal_live(sess):
            raise OfficeConflict(
                f"a planner is busy in a terminal (pid {sess[0].payload.get('pid')})"
            )

    # ------------------------------------------------------------ start

    def start(self, description: str | None = None, from_draft: bool = False) -> None:
        """Start a stage-1 planner session. With `description`, an existing PROJECT_PLAN.md is
        first archived as PROJECT_PLAN.<UTC timestamp>.md. With `from_draft`, the session is
        told to pick up the draft on disk. Raises OfficeConflict if a session or run is
        active, a terminal planner is busy, or the input does not fit."""
        with self._lock:
            self._guard_idle()
            plan = self.repo / PROJECT_PLAN_FILE
            if from_draft:
                if not plan.is_file():
                    raise OfficeConflict("there is no draft plan to continue from")
                original = self._previous_description()
                text = RESUME_PROMPT.format(project_plan=plan, description=original)
                shown = original
            else:
                shown = (description or "").strip()
                if not shown:
                    raise OfficeConflict("the description is empty")
                if len(shown) > MAX_TEXT_CHARS:
                    raise OfficeConflict("the description is too long")
                text = shown
                if plan.exists():
                    self._archive(plan)
            self._begin(text, shown, stage=1, from_draft=from_draft)

    def _previous_description(self) -> str:
        sess = last_session(read_planner_events(self.repo))
        value = sess[0].payload.get("description") if sess else None
        return value if isinstance(value, str) else ""

    def _archive(self, plan: Path) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self._clock()))
        target = plan.with_name(f"PROJECT_PLAN.{stamp}.md")
        n = 1
        while target.exists():
            n += 1
            target = plan.with_name(f"PROJECT_PLAN.{stamp}-{n}.md")
        plan.rename(target)

    def _begin(self, text: str, shown: str, *, stage: int, from_draft: bool = False) -> None:
        """Start the planner thread. Caller holds the lock and has passed _guard_idle."""
        sid = new_session_id()
        log = PlannerLog(self.repo, sid)
        self._phase = "project" if stage == 1 else "staging"
        self._session_id, self._stage, self._log = sid, stage, log
        self._error = self._reply = self._run_id = None
        self._progress_lines = []
        self._cost = 0.0
        self._end_reason = None
        self._answers = queue.Queue()
        self._stopping.clear()
        self._waiting = False
        self._state = "busy" if stage == 1 else "staging"
        observed = ObservedSession(
            self._factory(self.repo, self._on_progress), self.repo, log, lambda: self._phase
        )
        self._observed = observed
        log.emit(
            "planner_started",
            {
                "session_id": sid,
                "source": "office",
                "pid": os.getpid(),
                "stage": stage,
                "description": shown,
                "resumed_from_draft": from_draft,
            },
        )
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(text, stage, observed),
            name="harness-planner",
            daemon=False,
        )
        self._thread.start()

    # ------------------------------------------------------------ messages

    def message(self, text: str) -> None:
        """Hand `text` to the planner (an answer, or a request to change the plan). Allowed
        when the boss is asking or ready and no run is live."""
        with self._lock:
            self._guard_message()
            self._send(text)

    def _guard_message(self) -> None:
        live = live_run(self.repo, self._clock())
        if live is not None:
            raise OfficeConflict(f"a run is active ({live}); wait for it to finish")
        if self._state not in ("asking", "ready"):
            raise OfficeConflict("the planner is not waiting for a message")

    def _send(self, text: str) -> None:
        """Queue `text` and turn the boss busy. Caller holds the lock."""
        self._state = "staging" if self._phase == "staging" else "busy"
        self._progress_lines = []
        self._answers.put(text)

    def implement(self) -> None:
        raise OfficeConflict("not yet")

    def next_stage(self) -> None:
        raise OfficeConflict("not yet")

    # ------------------------------------------------------------ planner thread

    async def _on_progress(self, info: dict[str, Any]) -> None:
        with self._lock:
            self._progress_lines = [*self._progress_lines, _progress_line(info)][-PROGRESS_LINES:]
            log = self._log
        if log is not None:
            log.emit(
                "planner_progress",
                {"tool": info.get("tool"), "detail": info.get("detail", "")},
            )

    def _ask(self, _prompt: str) -> str | None:
        """run_planner's `ask`: park the boss as asking (or ready) and wait for a message.
        Returns None on shutdown or after the idle timeout."""
        observed = self._observed
        with self._lock:
            self._reply = observed.reply_text if observed is not None else self._reply
            self._state = "ready" if observed is not None and observed.ready else "asking"
            self._waiting = True
            self._last_activity = self._clock()
        poll = min(1.0, max(0.05, self.idle_timeout_s / 4))
        try:
            while True:
                try:
                    return self._answers.get(timeout=poll)
                except queue.Empty:
                    pass
                if self._stopping.is_set():
                    return None
                if self._clock() - self._last_activity >= self.idle_timeout_s:
                    with self._lock:
                        if self._end_reason is None and self._log is not None:
                            self._end_reason = "idle timeout"
                            self._log.emit(
                                "planner_failed",
                                {
                                    "reason": "idle timeout",
                                    "cost_usd": self._current_cost(),
                                    "idle_s": self.idle_timeout_s,
                                },
                            )
                    return None
        finally:
            with self._lock:
                self._waiting = False

    async def _plan(self, text: str, stage: int, observed: ObservedSession) -> PlanResult:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        return await run_planner(text, self.repo, observed, self._ask, lambda _t: None, stage=stage)

    def _thread_main(self, text: str, stage: int, observed: ObservedSession) -> None:
        try:
            result = asyncio.run(self._plan(text, stage, observed))
        except PlannerError as err:
            self._failed(str(err))
        except asyncio.CancelledError:
            self._failed("interrupted: server stopped")
        except Exception as err:  # the thread must always leave the boss in a final state
            self._failed(f"unexpected error: {type(err).__name__}: {err}")
        else:
            self._done(result)

    def _failed(self, reason: str) -> None:
        """The session ended without a plan. Logs planner_failed unless shutdown or the idle
        timeout already did."""
        with self._lock:
            if self._end_reason is None and self._log is not None:
                self._end_reason = reason
                self._log.emit(
                    "planner_failed", {"reason": reason, "cost_usd": self._current_cost()}
                )
            reason = self._end_reason or reason
            self._state = "interrupted" if _is_interruption(reason) else "failed"
            self._error = _reason_note(reason, self.idle_timeout_s)

    def _done(self, result: PlanResult) -> None:
        with self._lock:
            if self._log is not None:
                try:
                    shown = result.plan_path.relative_to(self.repo).as_posix()
                except ValueError:
                    shown = str(result.plan_path)
                self._log.emit(
                    "planner_done",
                    {
                        "plan_path": shown,
                        "cost_usd": result.usage.cost_usd,
                        "rounds": result.rounds,
                    },
                )
            self._cost = result.usage.cost_usd
            self._state = "idle"
