"""The Orchestrator: the office's router. Every request typed to it goes to a one-turn Sonnet
session that picks who takes the work (a Haiku or Sonnet chat, or Opus planning) and never
answers or edits anything itself. Follow-ups stay with the current holder or move up a tier.

Every turn is written to the planner event log (`run_id` = the thread id, `orch-<stamp>`), so
a restart or a read-only page rebuilds the thread with `orchestrator_view_from_log`."""

import asyncio
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from harness.config import TIER_LADDER, Tier
from harness.models import Event
from harness.planner import (
    ORCHESTRATOR_PROMPT,
    PlannerSession,
    PlannerTurn,
    Routing,
    SdkTriageSession,
    parse_routing,
)
from harness.planner_log import PlannerLog, last_session, read_planner_events
from harness.state import new_run_id
from harness.ui.chat import (
    ChatConflict,
    ChatManager,
    _pid_alive,
    _progress_line,
    chats_from_log,
    read_chat_events,
)
from harness.ui.demo import DemoPlannerSession
from harness.ui.markdown import parse_markdown
from harness.worker import ProgressFn

OrchState = Literal["idle", "routing", "failed"]
OrchAction = Literal["start", "forward", "escalate"]
OrchSessionFactory = Callable[[Path, ProgressFn], PlannerSession]
TAIL_ENTRIES = 12
TAIL_CHARS = 500
MAX_TEXT_CHARS = 20_000
PROGRESS_LINES = 8
JOIN_TIMEOUT_S = 10.0
NO_ROUTING_NEW = "the orchestrator gave no route; sending to sonnet"
NO_ROUTING_FOLLOW = "the orchestrator gave no route; passing it on"
OPUS_HOLDS = "opus holds the work"
ROUTING_BUSY = "the orchestrator is still routing the last message"
OPUS_BUSY = "opus is working; wait for its reply"


class OrchestratorConflict(Exception):
    """The message does not fit the Orchestrator's state (answered 409)."""


class HandoffRefused(Exception):
    """The Opus desk refused a handoff. The message is a sentence for the user."""


class OpusDesk(Protocol):
    def start(self, text: str, plan: Literal["quick", "project"]) -> str:
        """Start an Opus planning session on `text`; return its session id."""
        ...

    def message(self, text: str) -> None:
        """Send `text` to the current Opus session."""
        ...

    def live(self, session_id: str) -> bool:
        """True if `session_id` is the current session and it is busy/asking/ready/staging."""
        ...

    def waiting(self, session_id: str) -> bool:
        """True if `session_id` is the current session and it is asking or ready."""
        ...


class OrchEntry(BaseModel):
    """One line of the Orchestrator thread. `who` is "You", "Orchestrator", "haiku",
    "sonnet" or "opus"; `blocks` is parse_markdown(text), [] for the user's messages."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "orchestrator", "agent"]
    who: str
    ts: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class OrchestratorView(BaseModel):
    """What the page shows for the Orchestrator. `holder_id` is a chat id or a planner
    session id; `progress` lives in memory only, while routing; `cost_usd` sums every
    Orchestrator turn in the log, across all threads."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = None
    state: OrchState = "idle"
    holder: Tier | None = None
    holder_id: str | None = None
    transcript: list[OrchEntry] = Field(default_factory=list)
    reason: str | None = None
    progress: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None


def rank(tier: Tier) -> int:
    """TIER_LADDER.index(tier): haiku 0, sonnet 1, opus 2."""
    return TIER_LADDER.index(tier)


def new_thread_id() -> str:
    """f"orch-{new_run_id()}", e.g. orch-20261023-101500."""
    return f"orch-{new_run_id()}"


def tail_text(entries: list[OrchEntry]) -> str:
    """The last TAIL_ENTRIES entries, one line each: f"{who}: {text}" where text has every
    whitespace run collapsed to one space (" ".join(text.split())) and is then cut to
    TAIL_CHARS characters; lines joined with "\\n". "(none)" when `entries` is empty."""
    if not entries:
        return "(none)"
    lines = [f"{e.who}: {' '.join(e.text.split())[:TAIL_CHARS]}" for e in entries[-TAIL_ENTRIES:]]
    return "\n".join(lines)


def handoff_brief(entries: list[OrchEntry], text: str, from_tier: Tier) -> str:
    """The first message for the agent taking over from `from_tier`:
    f"You are taking over this work from {from_tier} in the harness office.\\n"
    f"Conversation so far:\\n{tail_text(entries)}\\n\\nThe user now says:\\n{text}"."""
    return (
        f"You are taking over this work from {from_tier} in the harness office.\n"
        f"Conversation so far:\n{tail_text(entries)}\n\nThe user now says:\n{text}"
    )


def routing_line(action: OrchAction, to: Tier, plan: str, reason: str) -> str:
    """The Orchestrator's line in the thread for one turn. `shown` is f"opus ({plan} plan)"
    when to is "opus", else to. start: f"Sent to {shown}: {reason}"; escalate:
    f"Moved up to {shown}: {reason}"; forward: f"Passed to {to}." (no plan, no reason)."""
    shown = f"opus ({plan} plan)" if to == "opus" else to
    if action == "start":
        return f"Sent to {shown}: {reason}"
    if action == "escalate":
        return f"Moved up to {shown}: {reason}"
    return f"Passed to {to}."


def _cost(payload: dict[str, Any]) -> float:
    v = payload.get("cost_usd")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return 0.0
    return float(v)


def _text(payload: dict[str, Any]) -> str | None:
    t = payload.get("text")
    return t if isinstance(t, str) and t else None


def orchestrator_view_from_log(repo: Path) -> OrchestratorView:
    """The Orchestrator's current thread rebuilt from the planner and chat logs, without
    touching anything. Algorithm in the `decision: orchestrator_view_from_log` context
    item. Never raises on a missing or partly unreadable log."""
    all_planner = read_planner_events(repo)
    events = [e for e in all_planner if e.run_id.startswith("orch-")]
    cost = sum(
        _cost(e.payload) for e in events if e.type in ("orchestrator_turn", "orchestrator_failed")
    )
    started: Event | None = None
    for e in events:
        if e.type == "orchestrator_started":
            started = e
    if started is None:
        return OrchestratorView(cost_usd=cost)
    thread_id = started.run_id
    thread = [e for e in events if e.run_id == thread_id]
    if any(e.type == "orchestrator_closed" for e in thread):
        return OrchestratorView(cost_usd=cost)

    items: list[tuple[datetime, OrchEntry]] = []
    # (kind, target_id) -> (first turn, last user event before it, "to")
    targets: dict[tuple[str, str], tuple[Event, Event | None, Any]] = {}
    last_user: Event | None = None
    for e in thread:
        p = e.payload
        if e.type == "orchestrator_user":
            t = _text(p)
            if t:
                items.append((e.ts, OrchEntry(role="user", who="You", ts=e.ts.isoformat(), text=t)))
            last_user = e
        elif e.type == "orchestrator_turn":
            t = _text(p)
            if t:
                items.append(
                    (
                        e.ts,
                        OrchEntry(
                            role="orchestrator",
                            who="Orchestrator",
                            ts=e.ts.isoformat(),
                            text=t,
                            blocks=parse_markdown(t),
                        ),
                    )
                )
            kind = p.get("target_kind")
            tid = p.get("target_id")
            if kind in ("chat", "opus") and isinstance(tid, str) and tid:
                targets.setdefault((kind, tid), (e, last_user, p.get("to")))
        elif e.type == "orchestrator_failed":
            reason = str(e.payload.get("reason") or "the orchestrator failed")
            items.append(
                (
                    e.ts,
                    OrchEntry(
                        role="orchestrator",
                        who="Orchestrator",
                        ts=e.ts.isoformat(),
                        text=reason,
                        blocks=parse_markdown(reason),
                    ),
                )
            )

    chat_events: list[Event] = []
    if any(k == "chat" for k, _ in targets):
        chat_events = read_chat_events(repo)
    replies: list[tuple[datetime, OrchEntry]] = []
    for (kind, tid), (turn, user, to) in targets.items():
        floor = user.ts if user is not None else turn.ts
        if kind == "chat":
            mine = [e for e in chat_events if e.run_id == tid]
            who = to if isinstance(to, str) else ""
            for e in mine:
                if e.type == "chat_started" and isinstance(e.payload.get("tier"), str):
                    who = e.payload["tier"]
                    break
            cands = [e for e in mine if e.type == "chat_reply"]
        else:
            who = "opus"
            cands = [e for e in all_planner if e.run_id == tid and e.type == "planner_reply"]
        for e in cands:
            t = _text(e.payload)
            if t and e.ts >= floor:
                replies.append(
                    (
                        max(e.ts, turn.ts),
                        OrchEntry(
                            role="agent",
                            who=who,
                            ts=e.ts.isoformat(),
                            text=t,
                            blocks=parse_markdown(t),
                        ),
                    )
                )
    transcript = [entry for _, entry in sorted(items + replies, key=lambda x: x[0])]

    last_turn: Event | None = None
    for e in thread:
        if e.type == "orchestrator_turn":
            last_turn = e
    reason: str | None = None
    holder: Tier | None = None
    holder_id: str | None = None
    if last_turn is not None:
        r = last_turn.payload.get("reason")
        reason = r if isinstance(r, str) else None
        kind = last_turn.payload.get("target_kind")
        tid = last_turn.payload.get("target_id")
        if kind == "chat":
            for v in chats_from_log(repo):
                if v.chat_id == tid:
                    if v.state in ("working", "waiting"):
                        holder, holder_id = v.tier, tid
                    break
        elif kind == "opus":
            sess = last_session(all_planner)
            if (
                sess
                and sess[0].run_id == tid
                and not any(e.type in ("planner_done", "planner_failed") for e in sess)
            ):
                holder, holder_id = "opus", tid

    last = thread[-1]
    state: OrchState = "idle"
    error: str | None = None
    if last.type == "orchestrator_user" and _pid_alive(started.payload.get("pid")):
        state = "routing"
    elif last.type == "orchestrator_failed":
        state = "failed"
        error = str(last.payload.get("reason") or "the orchestrator failed")
    return OrchestratorView(
        thread_id=thread_id,
        state=state,
        holder=holder,
        holder_id=holder_id,
        transcript=transcript,
        reason=reason,
        progress=[],
        cost_usd=cost,
        error=error,
    )


Target = tuple[Tier, Literal["chat", "opus"], str]  # (tier, target kind, chat id or session id)


class Orchestrator:
    """Routes every office request through a one-turn session. Public calls: `message`,
    `new_thread` (each raises OrchestratorConflict with a sentence), `view`, `restore`,
    `close`. All state lives under one lock. Lock order: self._lock may be held while
    calling chats.views(), opus.live/waiting/message; the Office never calls the
    Orchestrator while holding its own lock, so this cannot deadlock."""

    def __init__(
        self,
        repo: Path,
        chats: ChatManager,
        opus: OpusDesk,
        *,
        fake: bool = False,
        fake_delay: float = 2.0,
        session_factory: OrchSessionFactory | None = None,
    ) -> None:
        """Fields in the `decision: Orchestrator fields` context item."""
        self.repo = Path(repo).resolve()
        self.chats = chats
        self.opus = opus
        if session_factory is not None:
            self._factory: OrchSessionFactory = session_factory
        elif fake:

            def fake_factory(repo: Path, progress: ProgressFn) -> PlannerSession:
                return DemoPlannerSession(repo, progress=progress, delay_s=fake_delay)

            self._factory = fake_factory
        else:

            def sdk_factory(repo: Path, progress: ProgressFn) -> PlannerSession:
                return SdkTriageSession(repo, progress=progress)

            self._factory = sdk_factory
        self._lock = threading.Lock()
        self._state: OrchState = "idle"
        self._error: str | None = None
        self._progress: list[str] = []
        self._thread_id: str | None = None
        self._log: PlannerLog | None = None
        self._pid_logged = False
        self._target: Target | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._closed = False

    # -- helpers (caller holds self._lock) ----------------------------------------------

    def _live_holder(self) -> tuple[Target, str] | None:
        """(target, chat state or "") of the live holder; clears a dead target."""
        target = self._target
        if target is None:
            return None
        tier, kind, target_id = target
        if kind == "chat":
            for view in self.chats.views():
                if view.chat_id == target_id and view.state in ("working", "waiting"):
                    return target, view.state
        elif self.opus.live(target_id):
            return target, ""
        self._target = None
        return None

    def _emit(self, type: str, payload: dict[str, Any]) -> None:
        if self._log is not None:
            self._log.emit(type, payload)

    # -- public calls -------------------------------------------------------------------

    def message(self, text: str) -> None:
        """Take one user message. Algorithm in the `decision: message` context item."""
        stripped = text.strip()
        if not stripped:
            raise OrchestratorConflict("the message is empty")
        if len(stripped) > MAX_TEXT_CHARS:
            raise OrchestratorConflict("the message is too long")
        with self._lock:
            if self._state == "routing":
                raise OrchestratorConflict(ROUTING_BUSY)
            live = self._live_holder()
            holder = live[0] if live else None
            if live and holder is not None:
                if holder[1] == "chat" and live[1] == "working":
                    raise OrchestratorConflict(f"the {holder[0]} agent is still working")
                if holder[1] == "opus" and not self.opus.waiting(holder[2]):
                    raise OrchestratorConflict(OPUS_BUSY)
            if self._thread_id is None:
                self._thread_id = new_thread_id()
                self._log = PlannerLog(self.repo, self._thread_id)
                self._pid_logged = False
            if not self._pid_logged:
                self._emit("orchestrator_started", {"pid": os.getpid()})
                self._pid_logged = True
            entries = orchestrator_view_from_log(self.repo).transcript
            self._emit("orchestrator_user", {"text": stripped})
            if holder is not None and holder[1] == "opus":
                try:
                    self.opus.message(stripped)
                except HandoffRefused as err:
                    reason = f"could not hand to opus: {err}"
                    self._emit("orchestrator_failed", {"reason": reason, "cost_usd": 0.0})
                    self._state = "failed"
                    self._error = reason
                    raise OrchestratorConflict(str(err)) from None
                self._emit(
                    "orchestrator_turn",
                    {
                        "action": "forward",
                        "to": "opus",
                        "plan": "project",
                        "reason": OPUS_HOLDS,
                        "target_kind": "opus",
                        "target_id": holder[2],
                        "cost_usd": 0.0,
                        "text": routing_line("forward", "opus", "project", OPUS_HOLDS),
                    },
                )
                self._state = "idle"
                self._error = None
                return
            self._state = "routing"
            self._error = None
            self._progress = []
            assert self._log is not None
            self._thread = threading.Thread(
                target=self._thread_main,
                args=(stripped, holder, entries, self._log),
                name="harness-orchestrator",
                daemon=True,
            )
            self._thread.start()

    def new_thread(self) -> None:
        """Start over ("New request")."""
        with self._lock:
            if self._state == "routing":
                raise OrchestratorConflict(ROUTING_BUSY)
            if self._log is not None:
                self._emit("orchestrator_closed", {"reason": "new request"})
            self._thread_id = None
            self._log = None
            self._target = None
            self._pid_logged = False
            self._state = "idle"
            self._error = None
            self._progress = []

    def view(self) -> OrchestratorView:
        """The log view with state, error, progress and holder from memory."""
        logged = orchestrator_view_from_log(self.repo)
        with self._lock:
            live = self._live_holder()
            holder = live[0] if live else None
            return logged.model_copy(
                update={
                    "holder": holder[0] if holder else None,
                    "holder_id": holder[2] if holder else None,
                    "state": self._state,
                    "error": self._error,
                    "progress": list(self._progress) if self._state == "routing" else [],
                }
            )

    def restore(self) -> None:
        """After a restart: close an interrupted turn in the log once, then load the thread."""
        events = [e for e in read_planner_events(self.repo) if e.run_id.startswith("orch-")]
        started: Event | None = None
        for e in events:
            if e.type == "orchestrator_started":
                started = e
        if started is not None:
            thread = [e for e in events if e.run_id == started.run_id]
            if thread[-1].type == "orchestrator_user" and not _pid_alive(
                started.payload.get("pid")
            ):
                PlannerLog(self.repo, started.run_id).emit(
                    "orchestrator_failed",
                    {"reason": "interrupted: server restarted", "cost_usd": 0.0},
                )
        v = orchestrator_view_from_log(self.repo)
        with self._lock:
            self._thread_id = v.thread_id
            self._log = PlannerLog(self.repo, v.thread_id) if v.thread_id else None
            self._pid_logged = False
            self._state = "failed" if v.state == "failed" else "idle"
            self._error = v.error if v.state == "failed" else None
            if v.holder and v.holder_id:
                self._target = (v.holder, "opus" if v.holder == "opus" else "chat", v.holder_id)
            else:
                self._target = None

    def close(self) -> None:
        """Server shutdown: interrupt a turn in progress, join the routing thread."""
        with self._lock:
            self._closed = True
            thread = self._thread
            if self._state == "routing" and self._log is not None:
                reason = "interrupted: server stopped"
                self._emit("orchestrator_failed", {"reason": reason, "cost_usd": 0.0})
                self._state = "failed"
                self._error = reason
                if self._loop is not None and self._task is not None:
                    try:
                        self._loop.call_soon_threadsafe(self._task.cancel)
                    except RuntimeError:
                        pass
        if thread is not None:
            thread.join(JOIN_TIMEOUT_S)

    # -- the routing thread -------------------------------------------------------------

    def _fail(self, log: PlannerLog, reason: str, cost: float) -> None:
        with self._lock:
            if self._closed:
                return
            log.emit("orchestrator_failed", {"reason": reason, "cost_usd": cost})
            self._state = "failed"
            self._error = reason
            self._progress = []

    def _thread_main(
        self, text: str, holder: Target | None, entries: list[OrchEntry], log: PlannerLog
    ) -> None:
        try:
            asyncio.run(self._route(text, holder, entries, log))
        except asyncio.CancelledError:
            return
        except Exception as err:
            self._fail(log, f"unexpected error: {type(err).__name__}: {err}", 0.0)

    def _start_target(self, to: Tier, plan: Literal["quick", "project"], text: str) -> Target:
        if to == "opus":
            return ("opus", "opus", self.opus.start(text, plan))
        return (to, "chat", self.chats.start(to, text))

    async def _route(
        self, text: str, holder: Target | None, entries: list[OrchEntry], log: PlannerLog
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        prompt = ORCHESTRATOR_PROMPT.format(
            repo=self.repo,
            holder=holder[0] if holder else "nobody (new work)",
            tail=tail_text(entries),
            text=text,
        )

        async def progress_cb(info: dict[str, Any]) -> None:
            with self._lock:
                self._progress = [*self._progress, _progress_line(info)][-PROGRESS_LINES:]

        session = self._factory(self.repo, progress_cb)
        try:
            try:
                turn = await session.send(prompt)
            except Exception as err:
                turn = PlannerTurn(error=f"{type(err).__name__}: {err}")
        finally:
            try:
                await session.close()
            except Exception:
                pass
        cost = turn.usage.cost_usd
        if turn.error:
            self._fail(log, f"orchestrator session failed: {turn.error}", cost)
            return
        routing = parse_routing(turn.text)
        with self._lock:
            if self._closed:
                return
        action: OrchAction
        to: Tier = "sonnet"
        try:
            if holder is None:
                routing = routing or Routing(to="sonnet", reason=NO_ROUTING_NEW)
                to = routing.to
                action = "start"
                target = self._start_target(to, routing.plan, text)
            elif routing is None or rank(routing.to) <= rank(holder[0]):
                action = "forward"
                routing = Routing(
                    to=holder[0],
                    plan=routing.plan if routing else "project",
                    reason=routing.reason if routing else NO_ROUTING_FOLLOW,
                )
                to = holder[0]
                self.chats.message(holder[2], text)
                target = holder
            else:
                action = "escalate"
                to = routing.to
                brief = handoff_brief(entries, text, holder[0])
                try:
                    self.chats.close_chat(holder[2])
                except ChatConflict:
                    pass
                target = self._start_target(to, routing.plan, brief)
        except (ChatConflict, HandoffRefused) as err:
            self._fail(log, f"could not hand to {to}: {err}", cost)
            return
        with self._lock:
            if self._closed:
                return
            log.emit(
                "orchestrator_turn",
                {
                    "action": action,
                    "to": target[0],
                    "plan": routing.plan,
                    "reason": routing.reason,
                    "target_kind": target[1],
                    "target_id": target[2],
                    "cost_usd": cost,
                    "text": routing_line(action, target[0], routing.plan, routing.reason),
                },
            )
            self._target = target
            self._state = "idle"
            self._error = None
            self._progress = []
