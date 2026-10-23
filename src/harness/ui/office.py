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
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.events import EVENTS_FILE
from harness.models import Event
from harness.plan import PlanError, load_plan
from harness.planner import (
    APPROVE_QUICK,
    PROJECT_PLAN_FILE,
    PlannerError,
    PlannerSession,
    PlanResult,
    Route,
    RouteChoice,
    SdkPlannerSession,
    Triage,
    quick_plan_dir,
    run_planner,
)
from harness.planner_log import (
    ObservedSession,
    PlannerLog,
    last_session,
    new_session_id,
    read_planner_events,
)
from harness.state import STATE_FILE, StateError, latest_run, load_state, new_run_id, run_dir
from harness.ui.chat import ChatConflict, ChatManager, ChatSessionFactory, ChatView, chats_from_log
from harness.ui.demo import DemoPlannerSession
from harness.ui.markdown import parse_markdown
from harness.ui.view import IDLE_MINUTES, list_runs
from harness.worker import ProgressFn

LOCK_FILE = Path(".harness") / "office.lock"
MAX_TEXT_CHARS = 20_000
PROGRESS_LINES = 8
JOIN_TIMEOUT_S = 10.0
CHAT_WORKING = "an agent chat is working; wait for its turn to end"
STAGING_BLOCK = "the office is writing stage work orders; wait for it to finish"

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

Phase = Literal["triage", "project", "quick", "staging"]

SessionFactory = Callable[[Path, ProgressFn], PlannerSession]

_STATUS_RE = re.compile(r"^Status: (draft|approved) ?(?:\d{4}-\d{2}-\d{2})?$", re.MULTILINE)

APPROVAL_TEXT = (
    "The user approved the plan in the office page; the Status line is already set. "
    "Reply with one short line."
)

OFFICE_DIR = Path(".harness") / "office"

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


class TranscriptEntry(BaseModel):
    """One message of the conversation with Opus. `blocks` is `parse_markdown(text)` for
    Opus's replies and [] for the user's messages."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "opus"]
    ts: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)


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
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    chats: list[ChatView] = Field(default_factory=list)
    route: Route | None = None
    triage_reason: str | None = None
    plan_kind: Literal["project", "quick"] | None = None
    quick_plan: str | None = None
    resumable_run: str | None = None
    handoff_chat: str | None = None


# ---------------------------------------------------------------- small helpers


def status_of(text: str) -> Literal["draft", "approved"] | None:
    """The `Status:` of a PROJECT_PLAN.md text ("draft" or "approved"), None if the line is
    missing."""
    m = _STATUS_RE.search(text)
    if m is None:
        return None
    return "draft" if m.group(1) == "draft" else "approved"


def plan_status(repo: Path) -> Literal["draft", "approved"] | None:
    """status_of(PROJECT_PLAN.md in `repo`); None if the file is missing."""
    try:
        return status_of((Path(repo) / PROJECT_PLAN_FILE).read_text(encoding="utf-8"))
    except OSError:
        return None


def next_stage_of(repo: Path) -> int | None:
    """The stage "Plan next stage" would plan: N+1 when the last run launched by an office
    came from `stage-N/plan.json` and has finished, PROJECT_PLAN.md is approved and has a
    `## Stage N+1:` heading, and no run was launched from `stage-(N+1)/plan.json` yet.
    None otherwise."""
    launches = [e for e in read_planner_events(repo) if e.type == "run_launched"]
    if not launches or plan_status(repo) != "approved":
        return None
    m = re.search(r"stage-(\d+)/plan\.json$", str(launches[-1].payload.get("plan_path")))
    run_id = launches[-1].payload.get("run_id")
    if m is None or not isinstance(run_id, str):
        return None
    try:
        if not load_state(run_dir(repo, run_id)).finished:
            return None
        text = (repo / PROJECT_PLAN_FILE).read_text(encoding="utf-8")
    except (StateError, OSError, ValueError):
        return None
    following = int(m.group(1)) + 1
    if str(following) not in re.findall(r"^## Stage (\d+): ", text, re.MULTILINE):
        return None
    done = any(
        re.search(rf"stage-{following}/plan\.json$", str(e.payload.get("plan_path")))
        for e in launches
    )
    return None if done else following


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


def resumable_run_of(repo: Path) -> str | None:
    """latest_run(repo) if finished with a task not done and not live; else None."""
    rid = latest_run(repo)
    if rid is None:
        return None
    try:
        state = load_state(run_dir(repo, rid))
    except (StateError, OSError, ValueError):
        return None
    if not state.finished or all(t.status == "done" for t in state.tasks.values()):
        return None
    if live_run(repo) is not None:
        return None
    return rid


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


def transcript_of(sess: list[Event]) -> list[TranscriptEntry]:
    """The conversation of one planner session, in log order: each `planner_user` event
    becomes a "user" entry and each `planner_reply` an "opus" entry, when its `text` is a
    non-empty str. Other events are skipped. Never raises."""
    out: list[TranscriptEntry] = []
    try:
        for e in sess:
            text = e.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            if e.type == "planner_user":
                out.append(TranscriptEntry(role="user", ts=e.ts.isoformat(), text=text))
            elif e.type == "planner_reply":
                out.append(
                    TranscriptEntry(
                        role="opus", ts=e.ts.isoformat(), text=text, blocks=parse_markdown(text)
                    )
                )
    except Exception:
        return out
    return out


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
        route = started.get("route")
        route = route if route in ("quick", "project") else None
        for e in sess:
            if e.type == "planner_triage" and e.payload.get("route") in (
                "direct",
                "quick",
                "project",
            ):
                route = e.payload["route"]
                reason = e.payload.get("reason")
                fields["triage_reason"] = reason if isinstance(reason, str) else None
        fields["route"] = route
        fields["plan_kind"] = route if route in ("quick", "project") else None
        if route == "quick":
            paths = [e.payload.get("path") for e in sess if e.type == "plan_ready"]
            if paths and isinstance(paths[-1], str):
                fields["quick_plan"] = paths[-1]
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
    fields["transcript"] = transcript_of(sess)
    fields["chats"] = chats_from_log(repo)
    fields["state"] = state
    fields["can_click"] = interactive and state != "terminal"
    if state in ("idle", "failed", "interrupted"):
        fields["resumable_run"] = resumable_run_of(repo)
    if state == "idle":
        fields["next_stage"] = next_stage_of(repo)
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

    `start` takes a route: "auto" (Opus triages; direct work goes to a chat), "quick" (a
    one-stage plan) or "project" (the full flow).

    Public calls: `start`, `message`, `implement`, `next_stage` (each raises OfficeConflict
    with a sentence when the state does not allow it), `view`, `open`, `close`. `fake` swaps
    in the scripted DemoPlannerSession; `session_factory(repo, progress)` overrides both.
    `launcher` replaces subprocess.Popen when a run is started (tests capture argv).
    `chats` is the ChatManager; `chat_session_factory` overrides its sessions.
    With `auto_advance` (default) a stage-N run that ends with every task done starts the
    planning of stage N+1 by itself; `resume_run` resumes a stopped run."""

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
        chat_session_factory: ChatSessionFactory | None = None,
        clock: Callable[[], float] = time.time,
        auto_advance: bool = True,
    ) -> None:
        self.auto_advance = auto_advance
        self._resume_stamp: int | None = None
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
        self._phase: Phase = "project"
        self._route_choice: RouteChoice = "project"
        self._route: Route | None = None
        self._triage: Triage | None = None
        self._description = ""
        self._quick_dir: Path | None = None
        self._quick_plan: str | None = None
        self._handoff_chat: str | None = None
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
        self._popen: Any = None
        self._launched_plan: str | None = None
        self.chats = ChatManager(
            self.repo,
            fake=fake,
            fake_delay=fake_delay,
            session_factory=chat_session_factory,
            busy_fn=self._chat_block,
        )

    def _chat_block(self) -> str | None:
        """busy_fn for the chats: a sentence while chats must not edit the repo, else None.
        Never takes the office lock (ChatManager calls it from request threads)."""
        live = live_run(self.repo, self._clock())
        if live is not None:
            return f"a run is active ({live}); wait for it to finish"
        if self._state == "supervising":
            return f"a run is active ({self._run_id}); wait for it to finish"
        if self._state == "staging":
            return STAGING_BLOCK
        return None

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
        self._restore()

    def _restore(self) -> None:
        """Rebuild the boss from the planner log after a restart. A session that never ended
        and whose process is gone is closed in the log (planner_failed, "interrupted: server
        restarted") exactly once, so the boss shows `interrupted` with the draft viewable. A
        live run is picked up as `supervising` (no process handle: finished decides)."""
        sess = last_session(read_planner_events(self.repo))
        if sess and not any(e.type in ("planner_done", "planner_failed") for e in sess):
            if not pid_alive(sess[0].payload.get("pid")):
                replies = [e for e in sess if e.type == "planner_reply"]
                cost = float(replies[-1].payload.get("cost_usd") or 0.0) if replies else 0.0
                PlannerLog(self.repo, sess[0].run_id).emit(
                    "planner_failed", {"reason": "interrupted: server restarted", "cost_usd": cost}
                )
        v = office_view_from_log(self.repo, interactive=True)
        with self._lock:
            self._session_id, self._stage, self._cost = v.session_id, v.stage, v.cost_usd
            self._reply = v.reply
            if v.state in ("interrupted", "failed"):
                self._state, self._error = v.state, v.error
            elif v.state == "supervising":
                self._state, self._run_id = "supervising", v.run_id

    def close(self) -> str | None:
        """Shut down: stop a live planner session (logged as interrupted: server stopped,
        joined for up to JOIN_TIMEOUT_S), never touch a launched run, remove the lock file.
        Also closes every open chat.
        Returns the id of a run that is still going, or None."""
        self.chats.close()
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
            rid = self._run_id
            if rid is None:
                return None
            running = self._popen is not None and self._popen.poll() is None
            return rid if running or self._run_alive(rid) else None

    # ------------------------------------------------------------ view

    def _current_cost(self) -> float:
        return self._observed.cost_usd if self._observed is not None else self._cost

    def _run_alive(self, run_id: str) -> bool:
        try:
            return not load_state(run_dir(self.repo, run_id)).finished
        except (StateError, OSError, ValueError):
            return False

    def _transcript(self) -> list[TranscriptEntry]:
        """The current session's conversation from the planner log. Caller holds the lock."""
        if self._session_id is None:
            return []
        sid = self._session_id
        return transcript_of([e for e in read_planner_events(self.repo) if e.run_id == sid])

    def view(self) -> OfficeView:
        """The boss's state now. Idle, failed and interrupted give way to `supervising`
        while a run is live (also one started in a terminal) and to `terminal` while a
        `harness plan` is busy elsewhere."""
        with self._lock:
            self._refresh_supervising()
            state, run_id, error = self._state, self._run_id, self._error
            if state in ("idle", "failed", "interrupted"):
                live = live_run(self.repo, self._clock())
                if live is not None:
                    state, run_id = "supervising", live
                elif _terminal_live(last_session(read_planner_events(self.repo))):
                    state = "terminal"
            reply = self._reply
            view = OfficeView(
                next_stage=next_stage_of(self.repo) if state == "idle" else None,
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
                transcript=self._transcript(),
                route=self._route,
                triage_reason=self._triage.reason if self._triage else None,
                plan_kind=(
                    "quick"
                    if self._route == "quick"
                    else "project"
                    if self._route == "project"
                    else None
                ),
                quick_plan=self._quick_plan,
                handoff_chat=self._handoff_chat,
                **_plan_fields(self.repo),
            )
        return view.model_copy(
            update={
                "chats": self.chats.views(),
                "resumable_run": resumable_run_of(self.repo)
                if view.state in ("idle", "failed", "interrupted")
                else None,
            }
        )

    # ------------------------------------------------------------ guards

    def _guard_idle(self) -> None:
        """Raise OfficeConflict unless a new planner session may start. Caller holds the lock."""
        self._refresh_supervising()
        if self._state in ("busy", "asking", "ready", "staging"):
            raise OfficeConflict("a planning session is already in progress")
        live = live_run(self.repo, self._clock())
        if live is not None:
            raise OfficeConflict(f"a run is active ({live}); wait for it to finish")
        if self._state == "supervising":
            raise OfficeConflict(f"a run is active ({self._run_id}); wait for it to finish")
        sess = last_session(read_planner_events(self.repo))
        if _terminal_live(sess):
            raise OfficeConflict(
                f"a planner is busy in a terminal (pid {sess[0].payload.get('pid')})"
            )

    # ------------------------------------------------------------ start

    def start(
        self,
        description: str | None = None,
        from_draft: bool = False,
        route: RouteChoice = "project",
    ) -> None:
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
            self._begin(
                text,
                shown,
                stage=1,
                from_draft=from_draft,
                route="project" if from_draft else route,
            )

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

    def _begin(
        self,
        text: str,
        shown: str,
        *,
        stage: int,
        from_draft: bool = False,
        route: RouteChoice = "project",
        auto: bool = False,
    ) -> None:
        """Start the planner thread. Caller holds the lock and has passed _guard_idle."""
        sid = new_session_id()
        log = PlannerLog(self.repo, sid)
        if from_draft or stage != 1:
            route = "project"
        self._phase = "staging" if stage != 1 else "triage" if route == "auto" else route
        self._route_choice = route
        self._route = None if route == "auto" else route
        self._triage = None
        self._description = shown
        self._quick_dir = quick_plan_dir(self._clock()) if route in ("auto", "quick") else None
        self._quick_plan = None
        self._handoff_chat = None
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
                "route": route,
                "auto": auto,
            },
        )
        if stage == 1 and not from_draft:
            log.emit("planner_user", {"text": shown})
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
            if self._log is not None:
                self._log.emit("planner_user", {"text": text.strip()})
            self._send(text)

    def _guard_message(self) -> None:
        self._refresh_supervising()
        live = live_run(self.repo, self._clock())
        if live is not None:
            raise OfficeConflict(f"a run is active ({live}); wait for it to finish")
        if self._state == "supervising":
            raise OfficeConflict(f"a run is active ({self._run_id}); wait for it to finish")
        if self._state not in ("asking", "ready"):
            raise OfficeConflict("the planner is not waiting for a message")

    def _send(self, text: str) -> None:
        """Queue `text` and turn the boss busy. Caller holds the lock."""
        self._state = "staging" if self._phase == "staging" else "busy"
        self._progress_lines = []
        self._answers.put(text)

    def implement(self) -> None:
        """Approve the draft plan and go on to stage 1's work orders and the run: rewrite
        `Status: draft` to `Status: approved <today>`, then tell the session. Allowed when
        the boss is asking or ready and PROJECT_PLAN.md has a `Status: draft` line."""
        with self._lock:
            self._refresh_supervising()
            if self._state == "staging":
                raise OfficeConflict("already staging: the planner is writing the work orders")
            live = live_run(self.repo, self._clock())
            if self._state == "supervising" or live is not None:
                raise OfficeConflict(f"a run is already active ({live or self._run_id})")
            if self._state not in ("asking", "ready"):
                raise OfficeConflict("the planner is not waiting for a message")
            if self.chats.working():
                raise OfficeConflict(CHAT_WORKING)
            if self._phase == "quick":
                if self._quick_plan is None:
                    raise OfficeConflict("the quick plan is not written yet")
                self._send(APPROVE_QUICK)
                return
            plan = self.repo / PROJECT_PLAN_FILE
            try:
                text = plan.read_text(encoding="utf-8")
            except OSError:
                text = ""
            today = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
            new, n = re.subn(r"^Status: draft$", f"Status: approved {today}", text, flags=re.M)
            if n == 0:
                raise OfficeConflict("the plan has no `Status: draft` line to approve")
            plan.write_text(new, encoding="utf-8")
            self._phase = "staging"
            self._send(APPROVAL_TEXT)

    def next_stage(self) -> None:
        """Plan the next stage of the approved project plan: a new session runs
        `run_planner(stage=N+1)` (staging from the start), then its run is launched.
        Allowed when the boss is idle, no run is live and `next_stage_of` has a stage."""
        with self._lock:
            self._guard_idle()
            if self._state != "idle":
                raise OfficeConflict("the planner is not idle; start a new project instead")
            following = next_stage_of(self.repo)
            if following is None:
                raise OfficeConflict("there is no next stage to plan")
            if self.chats.working():
                raise OfficeConflict(CHAT_WORKING)
            self._begin("", f"stage {following}", stage=following)

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
            if self._phase == "quick" and self._quick_dir is not None:
                rel = (self._quick_dir / "plan.json").as_posix()
                try:
                    load_plan(self.repo / self._quick_dir / "plan.json")
                except (PlanError, OSError, ValueError):
                    self._quick_plan = None
                    self._state = "asking"
                else:
                    self._quick_plan = rel
                    self._state = "ready"
                    if self._log is not None:
                        self._log.emit(
                            "plan_ready", {"path": rel, "cost_usd": self._current_cost()}
                        )
            else:
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
        return await run_planner(
            text,
            self.repo,
            observed,
            self._ask,
            lambda _t: None,
            stage=stage,
            route=self._route_choice,
            on_triage=self._on_triage,
            quick_dir=self._quick_dir,
        )

    def _on_triage(self, triage: Triage) -> None:
        """run_planner's on_triage: keep Opus's routing and log it."""
        with self._lock:
            self._triage = triage
            self._route = triage.route
            if triage.route in ("quick", "project"):
                self._phase = triage.route
            log = self._log
        if log is not None:
            log.emit(
                "planner_triage",
                {"route": triage.route, "tier": triage.tier, "reason": triage.reason},
            )

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
        """run_planner returned a valid stage plan: log it, then launch the run."""
        with self._lock:
            shown: str | None = None
            if result.plan_path is not None:
                try:
                    shown = result.plan_path.relative_to(self.repo).as_posix()
                except ValueError:
                    shown = str(result.plan_path)
            if self._log is not None:
                self._log.emit(
                    "planner_done",
                    {
                        "plan_path": shown,
                        "cost_usd": result.usage.cost_usd,
                        "rounds": result.rounds,
                        "route": result.route,
                    },
                )
            self._cost = result.usage.cost_usd
            if result.route == "direct" or shown is None:
                tier = result.triage.tier if result.triage else "sonnet"
                try:
                    self._handoff_chat = self.chats.start(tier, self._description)
                except ChatConflict as err:
                    reason = f"could not hand to {tier}: {err}"
                    self._state = "failed"
                    self._error = reason
                    if self._log is not None:
                        self._log.emit("planner_failed", {"reason": reason, "cost_usd": self._cost})
                else:
                    self._state = "idle"
                return
            if self.chats.working():
                reason = f"could not start the run: {CHAT_WORKING}"
                self._state = "failed"
                self._error = reason
                if self._log is not None:
                    self._log.emit("planner_failed", {"reason": reason, "cost_usd": self._cost})
                return
            try:
                self._launch(shown)
            except (PlanError, OSError, ValueError) as err:
                reason = f"could not start the run: {err}"
                self._state = "failed"
                self._error = reason
                if self._log is not None:
                    self._log.emit("planner_failed", {"reason": reason, "cost_usd": self._cost})

    def run_argv(self, rel_plan: str, run_id: str) -> list[str]:
        """The `harness run` command line. Built only from server-side values."""
        argv = [
            sys.executable, "-m", "harness", "run", rel_plan,
            "--repo", str(self.repo), "--run-id", run_id,
        ]  # fmt: skip
        if self.fake:
            argv += ["--fake", "--fake-delay", str(self.fake_delay)]
        if self.max_cost is not None:
            argv += ["--max-cost", str(self.max_cost)]
        return argv

    def _launch(self, rel_plan: str, *, run_id: str | None = None, resume: bool = False) -> None:
        """Validate the stage plan again, then start `harness run` as a detached process
        (own session, so Ctrl-C on the UI leaves it running; output to
        .harness/office/run-<id>.log). With `resume` it adds --resume for `run_id` and
        appends to the log. Caller holds the lock."""
        load_plan(self.repo / rel_plan)
        rid = run_id or new_run_id()
        log_dir = self.repo / OFFICE_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        launcher = self._launcher or subprocess.Popen
        argv = self.run_argv(rel_plan, rid) + (["--resume"] if resume else [])
        self._resume_stamp = None
        if resume:
            try:
                self._resume_stamp = (run_dir(self.repo, rid) / STATE_FILE).stat().st_mtime_ns
            except OSError:
                self._resume_stamp = None
        with (log_dir / f"run-{rid}.log").open("a" if resume else "w", encoding="utf-8") as out:
            popen = launcher(
                argv,
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._popen = popen
        self._run_id = rid
        self._launched_plan = rel_plan
        self._state = "supervising"
        if self._log is not None:
            self._log.emit(
                "run_launched",
                {
                    "run_id": rid,
                    "plan_path": rel_plan,
                    "pid": getattr(popen, "pid", None),
                    "fake": self.fake,
                    "resume": resume,
                },
            )

    def _refresh_supervising(self) -> None:
        """supervising -> idle once the run's state.json says finished or the process has
        exited. A process that exited non-zero leaves an error naming its log. Caller holds
        the lock."""
        if self._state != "supervising" or self._run_id is None:
            return
        finished = self._run_finished(self._run_id)
        code = self._popen.poll() if self._popen is not None else None
        gone = self._popen is None and live_run(self.repo, self._clock()) != self._run_id
        if not (finished or code is not None or gone):
            return
        if code not in (None, 0):
            self._error = (
                f"run {self._run_id} exited with code {code}; "
                f"see {(OFFICE_DIR / f'run-{self._run_id}.log').as_posix()}"
            )
        self._state = "idle"
        self._after_run()

    def _after_run(self) -> None:
        """After a run ends: plan the next stage when every task is done, else leave a
        one-line count of what stopped. Caller holds the lock."""
        m = re.search(r"stage-(\d+)/plan\.json$", self._launched_plan or "")
        if m is None or self._run_id is None:
            return
        try:
            state = load_state(run_dir(self.repo, self._run_id))
        except (StateError, OSError, ValueError):
            return
        if not state.finished:
            return
        n = int(m.group(1))
        statuses = [t.status for t in state.tasks.values()]
        if all(s == "done" for s in statuses):
            if (
                self.auto_advance
                and next_stage_of(self.repo) == n + 1
                and live_run(self.repo, self._clock()) is None
                and not self.chats.working()
            ):
                self._begin("", f"stage {n + 1}", stage=n + 1, auto=True)
            return
        self._error = (
            f"Stage {n} stopped: {statuses.count('done')} done, {statuses.count('failed')} "
            f"failed, {statuses.count('blocked')} blocked, {statuses.count('paused')} paused."
        )

    def resume_run(self) -> None:
        """Resume the stopped run (resumable_run_of) with `harness run --resume`: the same
        run id, skipping tasks already done."""
        with self._lock:
            self._guard_idle()
            if self.chats.working():
                raise OfficeConflict(CHAT_WORKING)
            rid = resumable_run_of(self.repo)
            if rid is None:
                raise OfficeConflict("there is no stopped run to resume")
            try:
                plan_path = load_state(run_dir(self.repo, rid)).plan_path
            except (StateError, OSError, ValueError):
                raise OfficeConflict("there is no stopped run to resume") from None
            if self._log is None:
                self._log = PlannerLog(self.repo, self._session_id or new_session_id())
            try:
                self._launch(plan_path, run_id=rid, resume=True)
            except (PlanError, OSError, ValueError) as err:
                raise OfficeConflict(f"could not resume the run: {err}") from err
            self._error = None

    def _run_finished(self, run_id: str) -> bool:
        if self._resume_stamp is not None:
            try:
                now = (run_dir(self.repo, run_id) / STATE_FILE).stat().st_mtime_ns
            except OSError:
                now = None
            if now == self._resume_stamp:
                return False
            self._resume_stamp = None
        try:
            return load_state(run_dir(self.repo, run_id)).finished
        except (StateError, OSError, ValueError):
            return False
