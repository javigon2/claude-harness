"""Agent chats: a Haiku or Sonnet session the user talks to from the office page.

The agent edits the repo directly on the current branch; after every turn the harness
commits only the paths that turn changed. Every chat is logged to
<repo>/.harness/chats/events.jsonl (one `Event` per line, `run_id` = chat id), so a restart
or a read-only page can show past chats with `chats_from_log`. `ChatManager` runs the open
chats, one thread each, and commits after every agent turn."""

import asyncio
import os
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from claude_agent_sdk import ClaudeAgentOptions
from pydantic import BaseModel, ConfigDict, Field

from harness.config import MODEL_IDS, Tier
from harness.models import Event
from harness.plan import PlanError, load_plan
from harness.planner import (
    DEFAULT_EFFORT,
    Effort,
    PlannerSession,
    SdkPlannerSession,
    effort_or_default,
)
from harness.roles import DEFAULT_ROLE, ChatRole, researcher_prompt, role_or_default
from harness.state import StateError, load_state, new_run_id, run_dir
from harness.ui.chat_git import changed_paths, chat_commit_message, commit_paths, dirty_snapshot
from harness.ui.demo import DemoChatSession
from harness.ui.gitops import GitOpError
from harness.ui.markdown import parse_markdown
from harness.ui.uploads import attachment_note, image_ids
from harness.ui.view import progress_text
from harness.ui.work_branch import WORK_PREFIX, current_branch, ensure_work_branch
from harness.worker import WORKER_ENV, WORKER_TOOLS, ProgressFn
from harness.worktree import WorktreeError

CHAT_EVENTS = Path(".harness") / "chats" / "events.jsonl"
CHAT_TIERS: tuple[Tier, ...] = ("haiku", "sonnet")
CHAT_MAX_TURNS = 40
CHAT_MAX_CHARS = 20_000
CHAT_KEEP_CLOSED = 5
RESUME_WINDOW_S = 2 * 24 * 3600  # a closed chat can be picked up again for this long
CHAT_PROGRESS_LINES = 8
CHAT_JOIN_TIMEOUT_S = 10.0
CHAT_APPEND = (
    "You are chatting with the user in the harness office page. Make the change they ask "
    "for in this repo, run its checks, and reply in a few lines. The harness commits your "
    "edits after each turn; never run git commit, push or checkout. The office picks "
    "the model for every message and switches it on its own (and tells the user), so "
    "there is no model selector here: never suggest switching models."
)

RESEARCH_TIERS: tuple[Tier, ...] = ("haiku", "sonnet", "opus")
ARTIFACT_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
ARTIFACT_MAX = 40
_REPORT_PATH_RE = re.compile(r"(?<![\w/.-])(reports/[\w./-]+\.(?:md|png|jpe?g|gif|webp|svg|csv))")

ChatState = Literal["working", "waiting", "closed", "failed"]


def chat_tier_ok(tier: object, role: object) -> bool:
    """True if a chat of `role` may run on `tier`: builders on CHAT_TIERS (opus builds go
    to the planning desk), researchers on haiku, sonnet or opus."""
    tiers = RESEARCH_TIERS if role_or_default(role) == "researcher" else CHAT_TIERS
    return tier in tiers


def artifact_kind(path: str) -> str | None:
    """ "image", "report" (a .md under reports/), "data" (.csv) or None for `path`."""
    low = path.lower()
    if low.endswith(ARTIFACT_IMAGE_EXTS):
        return "image"
    if low.endswith(".md") and low.startswith("reports/"):
        return "report"
    if low.endswith(".csv"):
        return "data"
    return None


def collect_artifacts(repo: Path, paths: Sequence[str], text: str) -> list[dict[str, str]]:
    """What a turn produced for the page: each changed path (repo-relative) that is an
    image, a report or a CSV, in that order; plus `reports/...` paths named in the reply
    text that exist (when the change snapshot was skipped). At most ARTIFACT_MAX."""
    found: dict[str, str] = {}
    for p in paths:
        kind = artifact_kind(p)
        if kind is not None:
            found.setdefault(p, kind)
    root = Path(repo)
    for m in _REPORT_PATH_RE.finditer(text or ""):
        p = m.group(1).rstrip(".")
        kind = artifact_kind(p)
        if kind is not None and p not in found and ".." not in p and (root / p).is_file():
            found[p] = kind
    order = {"report": 0, "image": 1, "data": 2}
    items = sorted(found.items(), key=lambda kv: (order[kv[1]], kv[0]))
    return [{"path": p, "kind": k} for p, k in items[:ARTIFACT_MAX]]


def _artifacts(value: object) -> list[dict[str, str]]:
    """The logged artifacts list, keeping only well-formed items."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and item.get("kind") in ("image", "report", "data")
        ):
            out.append({"path": item["path"], "kind": item["kind"]})
    return out


ChatSessionFactory = Callable[[Path, Tier, ProgressFn], PlannerSession]

_WRITE_LOCK = threading.Lock()


class ChatConflict(Exception):
    """The request does not fit the chat's state. The message is a sentence for the user;
    the server answers 409 with it."""


class ChatEntry(BaseModel):
    """One message of a chat. `blocks` is parse_markdown(text) for the agent's replies and
    [] for the user's messages."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "agent"]
    ts: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, str]] = Field(default_factory=list)


class ChatView(BaseModel):
    """What the page shows for one chat. `progress` holds the last CHAT_PROGRESS_LINES
    "<tool>: <detail>" lines of the current turn (only while working); `commits` holds
    "<sha> <subject>" per commit, oldest first."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str
    tier: Tier
    state: ChatState
    run_id: str | None = None
    task_id: str | None = None
    transcript: list[ChatEntry] = Field(default_factory=list)
    progress: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    commits: list[str] = Field(default_factory=list)
    error: str | None = None
    session_id: str | None = None
    updated_at: str | None = None
    detached: bool = False
    effort: Effort | None = None  # thinking effort of the agent's current session
    role: ChatRole = DEFAULT_ROLE


class ChatLog:
    """A synchronous appender for one chat, callable from any thread. One write plus flush
    per line under a module-wide lock, like PlannerLog."""

    def __init__(self, repo: Path, chat_id: str) -> None:
        """self.path = Path(repo) / CHAT_EVENTS; self.chat_id = chat_id."""
        self.path: Path = Path(repo) / CHAT_EVENTS
        self.chat_id: str = chat_id

    def emit(self, type: str, payload: dict[str, Any] | None = None) -> Event:
        """Append Event(run_id=chat_id, type, payload) and return it. A failed write
        (OSError) is swallowed: a log problem must not end a chat."""
        event = Event(run_id=self.chat_id, type=type, payload=payload or {})
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


def read_chat_events(repo: Path) -> list[Event]:
    """Every readable event in the repo's chat log, in file order. [] if there is no log.
    Blank and unparseable lines are skipped."""
    try:
        text = (Path(repo) / CHAT_EVENTS).read_text(encoding="utf-8", errors="replace")
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


def _pid_alive(pid: object) -> bool:
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


def _progress_line(payload: dict[str, Any]) -> str:
    """progress_text(payload): the step in plain words."""
    return progress_text(payload)


def _num(value: object, default: float) -> float:
    try:
        return float(value) if value else default  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _build_view(chat_id: str, events: list[Event]) -> ChatView | None:
    started = events[0].payload
    tier = started.get("tier")
    role = role_or_default(started.get("role"))
    if not chat_tier_ok(tier, role):
        return None
    transcript: list[ChatEntry] = []
    progress: list[str] = []
    commits: list[str] = []
    cost = 0.0
    error: str | None = None
    ended: str | None = None
    last_user = False
    pid = started.get("pid")
    session_id: str | None = None
    effort: Effort | None = effort_or_default(started["effort"]) if "effort" in started else None
    for e in events[1:]:
        p = e.payload
        text = p.get("text")
        if e.type == "chat_user" and isinstance(text, str) and text:
            transcript.append(
                ChatEntry(role="user", ts=e.ts.isoformat(), text=text, images=image_ids(p))
            )
            progress = []
            last_user = True
        elif e.type == "chat_reply" and isinstance(text, str):
            transcript.append(
                ChatEntry(
                    role="agent",
                    ts=e.ts.isoformat(),
                    text=text,
                    blocks=parse_markdown(text),
                    artifacts=_artifacts(p.get("artifacts")),
                )
            )
            progress = []
            cost = _num(p.get("cost_usd"), 0.0)
            last_user = False
            sid = p.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
        elif e.type == "chat_resumed":
            ended = None
            error = None
            pid = p.get("pid")
            if "effort" in p:
                effort = effort_or_default(p["effort"])
        elif e.type == "chat_retuned":
            effort = effort_or_default(p.get("to"))
        elif e.type == "chat_progress":
            progress = (progress + [_progress_line(p)])[-CHAT_PROGRESS_LINES:]
        elif e.type == "chat_commit":
            commits.append(f"{p.get('sha')} {p.get('subject')}")
        elif e.type == "chat_note" and isinstance(text, str):
            error = text
        elif e.type == "chat_failed":
            ended = "failed"
            error = str(p.get("reason") or "the agent failed")
            cost = _num(p.get("cost_usd"), cost)
        elif e.type == "chat_closed" and ended != "failed":
            ended = "closed"
    state: ChatState
    if ended == "failed":
        state = "failed"
    elif ended == "closed" or not _pid_alive(pid):
        state = "closed"
    else:
        state = "working" if last_user else "waiting"
    run_id = started.get("run_id")
    task_id = started.get("task_id")
    return ChatView(
        chat_id=chat_id,
        tier=tier,
        state=state,
        run_id=run_id if isinstance(run_id, str) else None,
        task_id=task_id if isinstance(task_id, str) else None,
        transcript=transcript,
        progress=progress if state == "working" else [],
        cost_usd=cost,
        commits=commits,
        error=error,
        session_id=session_id,
        updated_at=events[-1].ts.isoformat(),
        effort=effort,
        role=role,
    )


def chats_from_log(repo: Path) -> list[ChatView]:
    """Every chat in the log, newest first, rebuilt without touching anything. Algorithm in
    the `decision: chats_from_log` context item."""
    groups: dict[str, list[Event]] = {}
    skipped: set[str] = set()
    for e in read_chat_events(repo):
        if e.run_id in groups:
            groups[e.run_id].append(e)
        elif e.type == "chat_started" and e.run_id not in skipped:
            groups[e.run_id] = [e]
        else:
            skipped.add(e.run_id)
    views = [v for cid, evs in groups.items() if (v := _build_view(cid, evs)) is not None]
    views.reverse()
    return views


def task_brief(repo: Path, run_id: str, task_id: str) -> str:
    """The text that seeds a chat about one task of a run. Format in the
    `decision: task_brief` context item. Raises ChatConflict(f"unknown task {task_id} in
    run {run_id}") when the run's state cannot be loaded or has no such task."""
    conflict = ChatConflict(f"unknown task {task_id} in run {run_id}")
    try:
        state = load_state(run_dir(Path(repo), run_id))
    except StateError:
        raise conflict from None
    ts = state.tasks.get(task_id)
    if ts is None:
        raise conflict
    order = None
    try:
        plan = load_plan(Path(repo) / state.plan_path)
        order = next((o for o in plan.orders if o.id == task_id), None)
    except (PlanError, OSError, ValueError):
        pass
    title = order.title if order else "unknown"
    goal = order.goal if order else "unknown"
    files = ", ".join(order.files_owned) if order else "unknown"
    summary = ts.history[-1].summary if ts.history and ts.history[-1].summary else "none"
    error = ts.error or "none"
    return "\n".join(
        [
            f"You are picking up task {task_id} ({title}) from run {run_id}; it ended {ts.status}.",
            f"Goal: {goal}",
            f"Files: {files}",
            f"Last summary: {summary}",
            f"Error: {error}",
        ]
    )


class SdkChatSession(SdkPlannerSession):
    """A PlannerSession on one ClaudeSDKClient for a Haiku or Sonnet chat. send() and
    close() are SdkPlannerSession's; only options() differ."""

    def __init__(self, repo: Path, tier: Tier, *, progress: ProgressFn | None = None) -> None:
        """super().__init__(repo, progress=progress, progress_interval_s=0.0);
        self.tier = tier."""
        super().__init__(repo, progress=progress, progress_interval_s=0.0)
        self.tier = tier
        self.role: ChatRole = DEFAULT_ROLE  # set by ChatManager before the first turn

    def options(self) -> ClaudeAgentOptions:
        """The exact options in the `decision: chat SDK options` context item."""
        return ClaudeAgentOptions(
            model=MODEL_IDS[self.tier],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": researcher_prompt() if self.role == "researcher" else CHAT_APPEND,
            },
            tools=list(WORKER_TOOLS),
            allowed_tools=list(WORKER_TOOLS),
            skills=[],
            setting_sources=["project"],
            permission_mode="acceptEdits",
            cwd=str(self.repo),
            max_turns=CHAT_MAX_TURNS,
            strict_mcp_config=True,
            env=dict(WORKER_ENV),
            resume=self.resume_id,
            effort=self.effort,
        )


def new_chat_id() -> str:
    """f"chat-{new_run_id()}-{secrets.token_hex(2)}", e.g. chat-20261023-101500-9f3a."""
    return f"chat-{new_run_id()}-{secrets.token_hex(2)}"


@dataclass
class _Chat:
    """One open chat. Every field is read and written under ChatManager._lock, except
    `loop` and `turn`, which only the chat's own thread sets."""

    chat_id: str
    tier: Tier
    log: ChatLog
    state: ChatState = "working"
    progress: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None
    last_text: str = ""
    inbox: queue.Queue[str | None] = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    turn: asyncio.Task[Any] | None = None
    resume_id: str | None = None
    detached: bool = False
    effort: Effort = DEFAULT_EFFORT
    session_id: str | None = None  # the Claude session of the latest reply
    cost_base: float = 0.0  # cost of earlier sessions of this chat (before a retune)
    role: ChatRole = DEFAULT_ROLE


def _check_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ChatConflict("the message is empty")
    if len(stripped) > CHAT_MAX_CHARS:
        raise ChatConflict("the message is too long")
    return stripped


class ChatManager:
    """Owns the open chats of one `harness ui` server, any number per tier in CHAT_TIERS: a
    new chat always gets its own agent, even while another agent of the tier is busy. A
    chat left behind by "New request" is `detached` (still open, hidden from the room).

    Each chat runs on its own daemon thread with its own asyncio loop; messages cross over
    `_Chat.inbox`. `live_fn()` returns the id of a live run (default: office.live_run), and
    while one is live a chat commits only when it is already on a work branch.
    `busy_fn()` returns a sentence while chats must not edit the repo (a run
    is live, or the office is staging) and None otherwise; it is always called before
    taking `_lock`, never while holding it. `fake` swaps in DemoChatSession;
    `session_factory(repo, tier, progress)` overrides both."""

    def __init__(
        self,
        repo: Path,
        *,
        fake: bool = False,
        fake_delay: float = 2.0,
        session_factory: ChatSessionFactory | None = None,
        busy_fn: Callable[[], str | None] = lambda: None,
        live_fn: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """self.repo = Path(repo).resolve(); keep busy_fn and clock; the factory is
        session_factory, else (fake) DemoChatSession(repo, tier, progress=progress,
        delay_s=fake_delay), else SdkChatSession(repo, tier, progress=progress).
        self._lock = threading.Lock(); self._chats: dict[str, _Chat] = {}."""
        self.repo = Path(repo).resolve()
        self._busy_fn = busy_fn
        if live_fn is None:

            def live_fn() -> str | None:
                from harness.ui.office import live_run

                return live_run(self.repo)

        self._live_fn = live_fn
        self._clock = clock
        if session_factory is not None:
            self._factory: ChatSessionFactory = session_factory
        elif fake:

            def fake_factory(repo: Path, tier: Tier, progress: ProgressFn) -> PlannerSession:
                return DemoChatSession(repo, tier, progress=progress, delay_s=fake_delay)

            self._factory = fake_factory
        else:

            def sdk_factory(repo: Path, tier: Tier, progress: ProgressFn) -> PlannerSession:
                return SdkChatSession(repo, tier, progress=progress)

            self._factory = sdk_factory
        self._lock = threading.Lock()
        self._chats: dict[str, _Chat] = {}

    def _prepare_branch(self) -> str | None:
        """A note when this turn must not commit, else None; on the way, put the repo on a
        work branch."""
        try:
            if self._live_fn() is not None:
                branch = current_branch(self.repo)
                if branch is None or not branch.startswith(WORK_PREFIX):
                    return f"a run is active on {branch or 'HEAD'}; chat changes are not committed"
                return None
            label = "chat-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            ensure_work_branch(self.repo, label)
        except GitOpError as err:
            return f"commit failed: {err}"
        return None

    def _refuse_if_busy(self) -> None:
        reason = self._busy_fn()
        if isinstance(reason, str) and reason:
            raise ChatConflict(reason)

    def _open_chat(self, chat_id: str) -> _Chat:
        """Caller holds _lock."""
        chat = self._chats.get(chat_id)
        if chat is not None:
            return chat
        if any(e.run_id == chat_id for e in read_chat_events(self.repo)):
            raise ChatConflict("the chat is closed")
        raise ChatConflict("unknown chat")

    def start(
        self,
        tier: str,
        text: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        images: Sequence[str] = (),
        effort: Effort = DEFAULT_EFFORT,
        role: str = DEFAULT_ROLE,
    ) -> str:
        """Open a `role` chat (builder or researcher) on `tier` at thinking `effort` and send
        `text` (stripped) and `images` (ids) as its first message; return the chat id.
        Checks in the order of the `decision: conflicts` item."""
        chat_role = role_or_default(role)
        if not chat_tier_ok(tier, chat_role):
            if chat_role == "researcher":
                raise ChatConflict(f"no {tier} researcher; use haiku, sonnet or opus")
            raise ChatConflict(f"no {tier} chat agent; use haiku or sonnet")
        stripped = _check_text(text)
        if (run_id is None) != (task_id is None):
            raise ChatConflict("send both run_id and task_id")
        self._refuse_if_busy()
        with self._lock:
            note = attachment_note(self.repo, images)
            first = stripped + note
            if run_id is not None and task_id is not None:
                brief = task_brief(self.repo, run_id, task_id)
                first = f"{brief}\n\nThe user says:\n{stripped}{note}"
            chat_id = new_chat_id()
            chat = _Chat(chat_id, cast(Tier, tier), ChatLog(self.repo, chat_id))
            chat.effort = effort_or_default(effort)
            chat.role = chat_role
            chat.log.emit(
                "chat_started",
                {
                    "tier": tier,
                    "run_id": run_id,
                    "task_id": task_id,
                    "pid": os.getpid(),
                    "effort": chat.effort,
                    "role": chat.role,
                },
            )
            payload: dict[str, Any] = {"text": stripped}
            if images:
                payload["images"] = list(images)
            chat.log.emit("chat_user", payload)
            chat.last_text = stripped
            self._chats[chat_id] = chat
            chat.inbox.put(first)
            chat.thread = threading.Thread(
                target=self._thread_main, args=(chat,), name=f"harness-{chat_id}", daemon=True
            )
            chat.thread.start()
        return chat_id

    def message(self, chat_id: str, text: str, images: Sequence[str] = ()) -> None:
        """Send `text` (stripped) and `images` (ids) to a waiting chat."""
        stripped = _check_text(text)
        self._refuse_if_busy()
        with self._lock:
            chat = self._open_chat(chat_id)
            if chat.state == "working":
                raise ChatConflict("the agent is still working")
            self._send_locked(chat, stripped, images)

    def resume(
        self,
        chat_id: str,
        text: str,
        images: Sequence[str] = (),
        effort: Effort | None = None,
    ) -> None:
        """Pick up an ended chat on its own Claude session, so the agent still knows
        everything it did. Allowed for RESUME_WINDOW_S after its last event, while the log
        holds a session id. `effort` defaults to the chat's last logged effort."""
        stripped = _check_text(text)
        self._refuse_if_busy()
        with self._lock:
            if chat_id in self._chats:
                raise ChatConflict("the chat is already open")
            view = next((v for v in chats_from_log(self.repo) if v.chat_id == chat_id), None)
            if view is None:
                raise ChatConflict("unknown chat")
            if view.state not in ("closed", "failed"):
                raise ChatConflict("the chat is already open")
            if not view.session_id:
                raise ChatConflict("this chat has no saved session to resume")
            last = datetime.fromisoformat(view.updated_at) if view.updated_at else None
            if last is None or self._clock() - last.timestamp() > RESUME_WINDOW_S:
                raise ChatConflict("this chat is older than 2 days and cannot be resumed")
            chat = _Chat(chat_id, view.tier, ChatLog(self.repo, chat_id))
            chat.resume_id = chat.session_id = view.session_id
            chat.cost_usd = view.cost_usd
            chat.effort = effort_or_default(effort or view.effort)
            chat.role = view.role
            chat.log.emit(
                "chat_resumed",
                {"pid": os.getpid(), "session_id": view.session_id, "effort": chat.effort},
            )
            payload: dict[str, Any] = {"text": stripped}
            if images:
                payload["images"] = list(images)
            chat.log.emit("chat_user", payload)
            chat.last_text = stripped
            self._chats[chat_id] = chat
            chat.inbox.put(stripped + attachment_note(self.repo, images))
            chat.thread = threading.Thread(
                target=self._thread_main, args=(chat,), name=f"harness-{chat_id}", daemon=True
            )
            chat.thread.start()

    def retune(self, chat_id: str, effort: Effort, text: str, images: Sequence[str] = ()) -> bool:
        """Change a waiting chat's thinking effort, then send `text` as its next message.
        The SDK cannot change effort on a live session, so the chat's session thread is
        stopped and a new one starts on the same chat id, resuming its Claude session (the
        agent keeps its whole history) at the new effort; logged chat_retuned {"from", "to",
        "session_id"}. Returns True when it was retuned. Without a saved session id the
        effort stays, a chat_note says so, the message is sent as usual, and it returns
        False. Refused like `message` (a working chat is "still working")."""
        stripped = _check_text(text)
        self._refuse_if_busy()
        with self._lock:
            old = self._open_chat(chat_id)
            if old.state == "working":
                raise ChatConflict("the agent is still working")
            target = effort_or_default(effort)
            if target == old.effort:
                self._send_locked(old, stripped, images)
                return False
            if not old.session_id:
                old.log.emit(
                    "chat_note",
                    {"text": f"no saved session to retune; kept {old.effort} effort"},
                )
                self._send_locked(old, stripped, images)
                return False
            chat = _Chat(chat_id, old.tier, old.log)
            chat.resume_id = chat.session_id = old.session_id
            chat.effort = target
            chat.role = old.role
            chat.cost_usd = chat.cost_base = old.cost_usd
            chat.detached = old.detached
            self._end(old, "closed")  # stops its thread; nothing is logged as closed
            chat.log.emit(
                "chat_retuned",
                {"from": old.effort, "to": target, "session_id": old.session_id},
            )
            self._chats[chat_id] = chat
            self._send_locked(chat, stripped, images)
            chat.thread = threading.Thread(
                target=self._thread_main, args=(chat,), name=f"harness-{chat_id}", daemon=True
            )
            chat.thread.start()
            return True

    def _send_locked(self, chat: _Chat, stripped: str, images: Sequence[str]) -> None:
        """Log and queue one user message for `chat`. Caller holds _lock."""
        payload: dict[str, Any] = {"text": stripped}
        if images:
            payload["images"] = list(images)
        chat.log.emit("chat_user", payload)
        chat.state = "working"
        chat.error = None
        chat.progress = []
        chat.last_text = stripped
        chat.inbox.put(stripped + attachment_note(self.repo, images))

    def close_chat(self, chat_id: str) -> None:
        """End an open chat (logged chat_closed {"reason": "closed by the user"}); a turn in
        progress is cancelled."""
        with self._lock:
            chat = self._open_chat(chat_id)
            chat.log.emit("chat_closed", {"reason": "closed by the user"})
            self._end(chat, "closed")

    def abort(self, chat_id: str) -> None:
        """Stop a working (or stuck) chat now: its turn is cancelled and the chat ends,
        logged chat_closed {"reason": "aborted by the user"}. Its session id stays in the
        log, so it can still be resumed. A late reply of the cancelled turn is dropped."""
        with self._lock:
            chat = self._open_chat(chat_id)
            chat.log.emit("chat_closed", {"reason": "aborted by the user"})
            self._end(chat, "closed")

    def detach(self, keep: Sequence[str] = ()) -> None:
        """A new request starts: every open chat except `keep` is detached, so the room shows
        none of it; a working one keeps working."""
        with self._lock:
            for chat in self._chats.values():
                chat.detached = chat.chat_id not in keep

    def attach(self, chat_id: str) -> bool:
        """Show an open chat in the room again (its request was reopened). False when it is
        not open."""
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                return False
            chat.detached = False
            return True

    def working(self) -> bool:
        """True while any open chat is in state "working"."""
        with self._lock:
            return any(c.state == "working" for c in self._chats.values())

    def views(self) -> list[ChatView]:
        """Open chats (newest first, state/progress/error/cost from memory), then at most
        CHAT_KEEP_CLOSED ended chats from chats_from_log, newest first."""
        logged = chats_from_log(self.repo)
        with self._lock:
            open_chats = dict(self._chats)
            snapshot = {
                cid: (c.state, list(c.progress), c.error, c.cost_usd, c.detached)
                for cid, c in open_chats.items()
            }
        result: list[ChatView] = []
        for view in logged:
            mem = snapshot.get(view.chat_id)
            if mem is None:
                continue
            state, progress, error, cost, detached = mem
            result.append(
                view.model_copy(
                    update={
                        "state": state,
                        "progress": progress if state == "working" else [],
                        "error": error,
                        "cost_usd": cost,
                        "detached": detached,
                    }
                )
            )
        ended = [v for v in logged if v.chat_id not in snapshot and v.state in ("closed", "failed")]
        return result + ended[:CHAT_KEEP_CLOSED]

    def close(self) -> None:
        """Server shutdown: log chat_closed {"reason": "server stopped"} for every open chat,
        end it (cancel its turn), then join the threads within CHAT_JOIN_TIMEOUT_S in
        total."""
        with self._lock:
            chats = list(self._chats.values())
            for chat in chats:
                chat.log.emit("chat_closed", {"reason": "server stopped"})
                self._end(chat, "closed")
        deadline = time.monotonic() + CHAT_JOIN_TIMEOUT_S
        for chat in chats:
            if chat.thread is not None:
                chat.thread.join(max(0.0, deadline - time.monotonic()))

    # -- internals ---------------------------------------------------------------------

    def _end(self, chat: _Chat, state: ChatState) -> None:
        """Caller holds _lock."""
        chat.state = state
        self._chats.pop(chat.chat_id, None)
        chat.inbox.put(None)
        loop, turn = chat.loop, chat.turn
        if loop is not None and turn is not None:
            try:
                loop.call_soon_threadsafe(turn.cancel)
            except RuntimeError:
                pass

    def _fail(self, chat: _Chat, reason: str) -> None:
        with self._lock:
            if self._chats.get(chat.chat_id) is not chat:
                return
            chat.error = reason
            chat.log.emit("chat_failed", {"reason": reason, "cost_usd": chat.cost_usd})
            self._end(chat, "failed")

    def _thread_main(self, chat: _Chat) -> None:
        try:
            asyncio.run(self._serve(chat))
        except Exception as err:  # the thread must always leave the chat ended or waiting
            self._fail(chat, f"unexpected error: {type(err).__name__}: {err}")

    async def _serve(self, chat: _Chat) -> None:
        chat.loop = asyncio.get_running_loop()

        async def progress(info: dict[str, Any]) -> None:
            with self._lock:
                chat.progress = [*chat.progress, _progress_line(info)][-CHAT_PROGRESS_LINES:]
            payload: dict[str, Any] = {"tool": info.get("tool"), "detail": info.get("detail", "")}
            summary = info.get("summary")
            if isinstance(summary, str) and summary:
                payload["summary"] = summary
            chat.log.emit("chat_progress", payload)

        session = self._factory(self.repo, chat.tier, progress)
        if chat.resume_id and hasattr(session, "resume_id"):
            session.resume_id = chat.resume_id
        if hasattr(session, "effort"):  # fixed for the session's life; see retune
            session.effort = chat.effort
        if hasattr(session, "role"):
            session.role = chat.role
        if hasattr(session, "tool_event"):  # per-tool durations for the Timing panel

            def tool_event(event: str, payload: dict[str, Any]) -> None:
                if event == "planner_tool_finished":
                    chat.log.emit("chat_tool", payload)

            session.tool_event = tool_event
        try:
            while True:
                text = await asyncio.to_thread(chat.inbox.get)
                if text is None:
                    break
                chat.turn = asyncio.ensure_future(self._turn(chat, session, text))
                try:
                    keep_going = await chat.turn
                except asyncio.CancelledError:
                    break
                finally:
                    chat.turn = None
                if not keep_going:
                    break
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _turn(self, chat: _Chat, session: PlannerSession, text: str) -> bool:
        note: str | None = None
        before: dict[str, str | None] | None
        git_start = time.monotonic()
        blocked = await asyncio.to_thread(self._prepare_branch)
        if blocked is not None:
            before = None
            note = blocked
        else:
            try:
                before = await asyncio.to_thread(dirty_snapshot, self.repo)
            except (WorktreeError, OSError) as err:
                before = None
                note = f"commit failed: {err}"
        git_ms = round((time.monotonic() - git_start) * 1000)
        agent_start = time.monotonic()
        turn = await session.send(text)
        timing: dict[str, Any] = {"duration_ms": round((time.monotonic() - agent_start) * 1000)}
        for key in ("connect_ms", "api_ms"):
            if getattr(turn, key, None) is not None:
                timing[key] = getattr(turn, key)
        git_start = time.monotonic()
        paths: list[str] = []
        if before is not None:
            try:
                after = await asyncio.to_thread(dirty_snapshot, self.repo)
                message = chat_commit_message(chat.tier, chat.last_text)
                # the chat log lives under .harness and is not part of the turn's work
                paths = [p for p in changed_paths(before, after) if not p.startswith(".harness/")]
                sha = await asyncio.to_thread(commit_paths, self.repo, paths, message)
                if sha:
                    chat.log.emit("chat_commit", {"sha": sha, "subject": message})
            except (WorktreeError, OSError) as err:
                note = f"commit failed: {err}"
        timing["git_ms"] = git_ms + round((time.monotonic() - git_start) * 1000)
        if note:
            chat.log.emit("chat_note", {"text": note})
        artifacts = [] if turn.error else collect_artifacts(self.repo, paths, turn.text or "")
        with self._lock:
            if self._chats.get(chat.chat_id) is not chat:
                return False
            chat.cost_usd = chat.cost_base + turn.usage.cost_usd
            if turn.session_id:
                chat.session_id = turn.session_id
            if turn.error:
                chat.log.emit(
                    "chat_failed",
                    {
                        "reason": f"agent session failed: {turn.error}",
                        "cost_usd": chat.cost_usd,
                        **timing,
                    },
                )
                chat.error = f"agent session failed: {turn.error}"
                self._end(chat, "failed")
                return False
            reply: dict[str, Any] = {"text": turn.text, "cost_usd": chat.cost_usd, **timing}
            if artifacts:
                reply["artifacts"] = artifacts
            if turn.session_id:
                reply["session_id"] = turn.session_id
            chat.log.emit("chat_reply", reply)
            chat.state = "waiting"
            chat.error = note
            chat.progress = []
            return True
