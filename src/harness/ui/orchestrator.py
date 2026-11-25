"""The Orchestrator: the office's router. Every request typed to it goes to a one-turn Sonnet
session that picks who takes the work (a Haiku or Sonnet chat, or Opus planning) and never
answers or edits anything itself. Follow-ups stay with the current holder or move up a tier.

Every turn is written to the planner event log (`run_id` = the thread id, `orch-<stamp>`), so
a restart or a read-only page rebuilds the thread with `orchestrator_view_from_log`."""

import asyncio
import inspect
import os
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from harness.config import TIER_LADDER, Tier
from harness.models import Event
from harness.planner import (
    DEFAULT_EFFORT,
    EFFORTS,
    ORCHESTRATOR_PROMPT,
    Effort,
    PlannerSession,
    PlannerTurn,
    Routing,
    SdkRoutingSession,
    effort_or_default,
    parse_routing,
)
from harness.planner_log import PlannerLog, last_session, read_planner_events
from harness.roles import DEFAULT_ROLE, ChatRole, role_or_default, role_title
from harness.state import new_run_id
from harness.ui.chat import (
    CHAT_TIERS,
    ChatConflict,
    ChatManager,
    _artifacts,
    _pid_alive,
    _progress_line,
    chats_from_log,
    read_chat_events,
)
from harness.ui.demo import DemoPlannerSession
from harness.ui.markdown import parse_markdown
from harness.ui.uploads import attachment_note, image_ids
from harness.worker import ProgressFn

OrchState = Literal["idle", "routing", "failed"]
OrchAction = Literal["start", "forward", "escalate", "switch", "retune", "handoff"]
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
ABORTED = "aborted by the user"
NOTHING_TO_ABORT = "no agent is working on this request"


class OrchestratorConflict(Exception):
    """The message does not fit the Orchestrator's state (answered 409)."""


class HandoffRefused(Exception):
    """The Opus desk refused a handoff. The message is a sentence for the user."""


class OpusDesk(Protocol):
    def start(
        self,
        text: str,
        plan: Literal["quick", "project"],
        images: Sequence[str] = (),
        effort: Effort | None = None,
    ) -> str:
        """Start an Opus planning session on `text` at thinking `effort`; return its
        session id."""
        ...

    def message(self, text: str, images: Sequence[str] = ()) -> None:
        """Send `text` to the current Opus session."""
        ...

    def resume(self, session_id: str, text: str, images: Sequence[str] = ()) -> str:
        """Continue the past planning session `session_id` on its own Claude session with
        `text`; return the new session id. Raise HandoffRefused with a sentence if it cannot."""

    def abort(self) -> None:
        """Stop the planning session now, whatever it is doing. HandoffRefused when there is
        none."""
        ...

    def detach(self) -> None:
        """A new request starts while the planning session is still working: it keeps
        working but no longer belongs to the shown request."""
        ...

    def attach(self, session_id: str) -> None:
        """Its request was reopened: the planning session belongs to it again."""
        ...

    def release(self) -> None:
        """End the current session if it only waits for the user; raise HandoffRefused
        while it works."""

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
    images: list[str] = Field(default_factory=list)
    agent_role: str | None = None  # an agent reply's role: builder, researcher or planner
    artifacts: list[dict[str, str]] = Field(default_factory=list)


class OrchestratorView(BaseModel):
    """What the page shows for the Orchestrator. `holder_id` is a chat id or a planner
    session id; `progress` lives in memory only, while routing; `cost_usd` sums every
    Orchestrator turn in the log, across all threads."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = None
    state: OrchState = "idle"
    holder: Tier | None = None
    holder_id: str | None = None
    holder_role: str | None = None
    transcript: list[OrchEntry] = Field(default_factory=list)
    reason: str | None = None
    progress: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None


def rank(tier: Tier) -> int:
    """TIER_LADDER.index(tier): haiku 0, sonnet 1, opus 2."""
    return TIER_LADDER.index(tier)


def new_thread_id() -> str:
    """f"orch-{new_run_id()}-{4 hex}", e.g. orch-20261023-101500-9f3a: unique even for two
    requests in the same second (New request then a message)."""
    return f"orch-{new_run_id()}-{secrets.token_hex(2)}"


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


def routing_line(
    action: OrchAction,
    to: Tier,
    plan: str,
    reason: str,
    effort: Effort | None = None,
    previous: Effort | None = None,
    role: str | None = None,
) -> str:
    """The Orchestrator's line in the thread for one turn. `shown` is f"opus ({plan} plan)"
    when to is "opus", else to. start: f"Sent to {shown}: {reason}"; escalate:
    f"Moved up to {shown}: {reason}"; switch (down to an easier tier): f"Switched down to
    {to}: {reason}"; forward: f"Passed to {to}." (no plan, no reason). With `effort`, a
    model change names it: "Moved up to sonnet (high effort): ..." or "opus (project plan,
    high effort)". retune (same agent, new effort): f"Effort raised to {effort}: {reason}"
    or "lowered", against `previous`. handoff (a different role takes over): f"Handed to
    {Title} ({to}): {reason}". A researcher is always named: "Sent to Researcher (sonnet)"."""
    if action == "handoff" or (role == "researcher" and action in ("start", "escalate", "switch")):
        named = f"{to}, {effort} effort" if effort is not None and action != "start" else to
        verb = {
            "handoff": "Handed to",
            "start": "Sent to",
            "escalate": "Moved up to",
            "switch": "Switched down to",
        }[action]
        return f"{verb} {role_title(role)} ({named}): {reason}"
    if action == "retune":
        up = previous is None or EFFORTS.index(effort or DEFAULT_EFFORT) > EFFORTS.index(previous)
        return f"Effort {'raised' if up else 'lowered'} to {effort}: {reason}"
    shown = f"opus ({plan} plan)" if to == "opus" else to
    if action in ("escalate", "switch") and effort is not None:
        named = f"{plan} plan, {effort} effort" if to == "opus" else f"{effort} effort"
        shown = f"{to} ({named})"
    if action == "start":
        return f"Sent to {shown}: {reason}"
    if action == "escalate":
        return f"Moved up to {shown}: {reason}"
    if action == "switch":
        return f"Switched down to {shown}: {reason}"
    return f"Passed to {to}."


def big_effort_jump(current: Effort, wanted: Effort) -> bool:
    """True for a two-step change (low <-> high): only that retunes a live agent."""
    return abs(EFFORTS.index(wanted) - EFFORTS.index(current)) >= 2


def _cost(payload: dict[str, Any]) -> float:
    v = payload.get("cost_usd")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return 0.0
    return float(v)


def _text(payload: dict[str, Any]) -> str | None:
    t = payload.get("text")
    return t if isinstance(t, str) and t else None


def orchestrator_view_from_log(repo: Path, thread_id: str | None = None) -> OrchestratorView:
    """The Orchestrator's current thread rebuilt from the planner and chat logs, without
    touching anything. Algorithm in the `decision: orchestrator_view_from_log` context
    item. Never raises on a missing or partly unreadable log.

    With `thread_id` it is that thread instead, open or closed, as a read-only past view:
    always idle, no holder, and `cost_usd` is that thread's own. An unknown id is the empty
    view."""
    wanted = thread_id
    all_planner = read_planner_events(repo)
    events = [e for e in all_planner if e.run_id.startswith("orch-")]
    cost = sum(
        _cost(e.payload)
        for e in events
        if e.type in ("orchestrator_turn", "orchestrator_failed")
        and (wanted is None or e.run_id == wanted)
    )
    started: Event | None = None
    for e in events:
        if e.type == "orchestrator_started" and (wanted is None or e.run_id == wanted):
            started = e
    if started is None:
        return OrchestratorView(cost_usd=cost)
    thread_id = started.run_id
    thread = [e for e in events if e.run_id == thread_id]
    last_start = max(i for i, e in enumerate(thread) if e.type == "orchestrator_started")
    if wanted is None and any(e.type == "orchestrator_closed" for e in thread[last_start:]):
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
                entry = OrchEntry(
                    role="user",
                    who="You",
                    ts=e.ts.isoformat(),
                    text=t,
                    images=image_ids(p),
                )
                items.append((e.ts, entry))
            last_user = e
        elif e.type == "orchestrator_turn":
            t = _text(p)
            # a first hand-off to a chat agent is not shown: its desk bubble already says
            # who has it; a model change (up or down) and an Opus hand-off (plan kind) are
            shown = (
                p.get("target_kind") != "chat"
                or p.get("action") in ("escalate", "switch", "retune", "handoff")
                or p.get("role") == "researcher"
            )
            if t and shown:
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
            agent_role = DEFAULT_ROLE
            for e in mine:
                if e.type == "chat_started" and isinstance(e.payload.get("tier"), str):
                    who = e.payload["tier"]
                    agent_role = role_or_default(e.payload.get("role"))
                    break
            cands = [e for e in mine if e.type == "chat_reply"]
        else:
            who = "opus"
            agent_role = "planner"
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
                            agent_role=agent_role,
                            artifacts=_artifacts(e.payload.get("artifacts")),
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
    holder_role: str | None = None
    if last_turn is not None:
        r = last_turn.payload.get("reason")
        reason = r if isinstance(r, str) else None
        kind = last_turn.payload.get("target_kind")
        tid = last_turn.payload.get("target_id")
        if wanted is not None:
            pass  # a past thread has no holder
        elif kind == "chat":
            for v in chats_from_log(repo):
                if v.chat_id == tid:
                    if v.state in ("working", "waiting"):
                        holder, holder_id, holder_role = v.tier, tid, v.role
                    break
        elif kind == "opus":
            sess = last_session(all_planner)
            if (
                sess
                and sess[0].run_id == tid
                and not any(e.type in ("planner_done", "planner_failed") for e in sess)
            ):
                holder, holder_id, holder_role = "opus", tid, "planner"

    last = thread[-1]
    state: OrchState = "idle"
    error: str | None = None
    if wanted is not None:
        pass  # a past thread is read only
    elif last.type == "orchestrator_user" and _pid_alive(started.payload.get("pid")):
        state = "routing"
    elif last.type == "orchestrator_failed":
        state = "failed"
        error = str(last.payload.get("reason") or "the orchestrator failed")
    return OrchestratorView(
        thread_id=thread_id,
        state=state,
        holder=holder,
        holder_id=holder_id,
        holder_role=holder_role,
        transcript=transcript,
        reason=reason,
        progress=[],
        cost_usd=cost,
        error=error,
    )


Target = tuple[Tier, Literal["chat", "opus"], str]  # (tier, target kind, chat id or session id)


def last_agent_target(events: list[Event]) -> Target | None:
    """The agent that took the thread's latest turn: a chat, or the Opus planning session."""
    found: Target | None = None
    for e in events:
        if e.type != "orchestrator_turn":
            continue
        p = e.payload
        tier, kind, tid = p.get("to"), p.get("target_kind"), p.get("target_id")
        if kind == "chat" and tier in ("haiku", "sonnet", "opus") and isinstance(tid, str):
            found = (cast(Tier, tier), "chat", tid)
        elif kind == "opus" and isinstance(tid, str) and tid:
            found = ("opus", "opus", tid)
        else:
            found = None
    return found


def _takes(fn: Callable[..., Any], name: str) -> bool:
    """True if `fn` accepts a keyword argument `name`."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def agent_efforts(events: list[Event]) -> dict[str, Effort]:
    """agent id -> the effort of its latest orchestrator_turn that logged one."""
    found: dict[str, Effort] = {}
    for e in events:
        tid = e.payload.get("target_id")
        if e.type == "orchestrator_turn" and isinstance(tid, str) and "effort" in e.payload:
            found[tid] = effort_or_default(e.payload["effort"])
    return found


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
        titler: Callable[[Path, str, str], object] | None = None,
    ) -> None:
        """Fields in the `decision: Orchestrator fields` context item. `titler(repo,
        thread_id, first_message)` names a new thread; it runs on its own thread."""
        self.repo = Path(repo).resolve()
        self._titler = titler
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
                return SdkRoutingSession(repo, progress=progress)

            self._factory = sdk_factory
        self._lock = threading.Lock()
        self._state: OrchState = "idle"
        self._error: str | None = None
        self._progress: list[str] = []
        self._thread_id: str | None = None
        self._log: PlannerLog | None = None
        self._pid_logged = False
        self._target: Target | None = None
        self._resumable: Target | None = None  # an ended chat of a reopened thread
        self._efforts: dict[str, Effort] = {}  # agent id -> its session's thinking effort
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._closed = False
        self._gen = 0  # bumped by abort, so a cancelled routing turn that returns late is dropped

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

    def _role_of(self, target: Target | None) -> ChatRole | None:
        """The chat role of `target`'s agent (builder or researcher); None for the Opus
        planning desk or no target. Caller holds self._lock."""
        if target is None or target[1] != "chat":
            return None
        for view in self.chats.views():
            if view.chat_id == target[2]:
                return view.role
        return DEFAULT_ROLE

    def _emit(self, type: str, payload: dict[str, Any]) -> None:
        if self._log is not None:
            self._log.emit(type, payload)

    # -- public calls -------------------------------------------------------------------

    def message(self, text: str, images: Sequence[str] = ()) -> None:
        """Take one user message. Algorithm in the `decision: message` context item."""
        stripped = text.strip()
        ids = tuple(images)
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
                if self._titler is not None:
                    threading.Thread(
                        target=self._titler,
                        args=(self.repo, self._thread_id, stripped),
                        daemon=True,
                        name=f"title-{self._thread_id}",
                    ).start()
            if not self._pid_logged:
                self._emit("orchestrator_started", {"pid": os.getpid()})
                self._pid_logged = True
            entries = orchestrator_view_from_log(self.repo).transcript
            user_payload: dict[str, Any] = {"text": stripped}
            if ids:
                user_payload["images"] = list(ids)
            self._emit("orchestrator_user", user_payload)
            if holder is not None and holder[1] == "opus":
                try:
                    if ids:
                        self.opus.message(stripped, images=ids)
                    else:
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
                        "effort": self._efforts.get(holder[2], DEFAULT_EFFORT),
                        "cost_usd": 0.0,
                        "text": routing_line("forward", "opus", "project", OPUS_HOLDS),
                    },
                )
                self._state = "idle"
                self._error = None
                return
            if holder is None and self._resumable is not None:
                tier, kind, agent_id = self._resumable
                self._resumable = None
                effort = self._efforts.get(agent_id, DEFAULT_EFFORT)
                try:
                    if kind == "chat":
                        self.chats.resume(agent_id, stripped, images=ids, effort=effort)
                    else:
                        agent_id = self.opus.resume(agent_id, stripped, images=ids)
                except (ChatConflict, HandoffRefused):
                    pass  # too old or no saved session: a fresh agent gets the thread's tail
                else:
                    reason = "resumed the same agent with its own history"
                    plan = "project"
                    self._emit(
                        "orchestrator_turn",
                        {
                            "action": "forward",
                            "to": tier,
                            "plan": plan,
                            "reason": reason,
                            "target_kind": kind,
                            "target_id": agent_id,
                            "effort": effort,
                            "cost_usd": 0.0,
                            "text": routing_line("forward", tier, plan, reason),
                        },
                    )
                    self._efforts[agent_id] = effort
                    self._target = (tier, kind, agent_id)
                    self._state = "idle"
                    self._error = None
                    return
            self._state = "routing"
            self._error = None
            self._progress = []
            assert self._log is not None
            holder_effort = self._efforts.get(holder[2], DEFAULT_EFFORT) if holder else None
            holder_role = self._role_of(holder)
            self._thread = threading.Thread(
                target=self._thread_main,
                args=(
                    stripped,
                    holder,
                    entries,
                    self._log,
                    ids,
                    self._gen,
                    holder_effort,
                    holder_role,
                ),
                name="harness-orchestrator",
                daemon=True,
            )
            self._thread.start()

    def _release_holder(self) -> None:
        """Let go of the agent holding the current thread so the next request gets its own.
        A waiting agent is closed (a chat keeps its session id, so it can be resumed); one
        that is working keeps working on its own and is only detached from the thread, so
        the room shows none of it. Caller holds self._lock."""
        self.chats.detach()
        live = self._live_holder()
        if live is None:
            return
        (_tier, kind, target_id), chat_state = live
        if kind == "chat":
            if chat_state == "waiting":
                try:
                    self.chats.close_chat(target_id)
                except ChatConflict:
                    pass
        elif self.opus.waiting(target_id):
            try:
                self.opus.release()
            except HandoffRefused:
                pass
        else:
            self.opus.detach()
        self._target = None

    def _leave_thread(self) -> None:
        """Close the current thread in the log. Caller holds self._lock."""
        if self._log is not None:
            self._emit("orchestrator_closed", {"reason": "new request"})
        self._thread_id = None
        self._log = None
        self._target = None
        self._resumable = None
        self._efforts = {}
        self._pid_logged = False
        self._state = "idle"
        self._error = None
        self._progress = []

    def new_thread(self) -> None:
        """Start over ("New request"): the agent of the old thread is released, so the new
        request starts with new agents and none of the old one's state."""
        with self._lock:
            if self._state == "routing":
                raise OrchestratorConflict(ROUTING_BUSY)
            self._release_holder()
            self._leave_thread()

    def open_thread(self, thread_id: str) -> None:
        """Go back to a past request: it becomes the current thread, and its last chat agent
        is resumed on the next message while it is within RESUME_WINDOW_S."""
        with self._lock:
            if self._state == "routing":
                raise OrchestratorConflict(ROUTING_BUSY)
            if thread_id == self._thread_id:
                return
            events = [e for e in read_planner_events(self.repo) if e.run_id == thread_id]
            if not any(e.type == "orchestrator_started" for e in events):
                raise OrchestratorConflict("unknown request")
            self._release_holder()
            self._leave_thread()
            resumable = last_agent_target(events)
            self._thread_id = thread_id
            self._log = PlannerLog(self.repo, thread_id)
            self._emit("orchestrator_started", {"pid": os.getpid(), "reopened": True})
            self._pid_logged = True
            self._resumable = resumable
            self._efforts = agent_efforts(events)
            if resumable is not None:  # its agent may still be working or waiting: rejoin it
                tier, kind, agent_id = resumable
                if kind == "chat" and self.chats.attach(agent_id):
                    self._target, self._resumable = resumable, None
                elif kind == "opus" and self.opus.live(agent_id):
                    self.opus.attach(agent_id)
                    self._target, self._resumable = resumable, None

    def abort(self) -> None:
        """Stop what is working on the current request now: a routing turn is cancelled, and
        the agent holding the request (chat or Opus) is aborted, working, waiting or stuck.
        The next message is routed afresh. Raises OrchestratorConflict when nothing works."""
        with self._lock:
            done = False
            if self._state == "routing":
                self._gen += 1
                if self._loop is not None and self._task is not None:
                    try:
                        self._loop.call_soon_threadsafe(self._task.cancel)
                    except RuntimeError:
                        pass
                self._emit("orchestrator_failed", {"reason": ABORTED, "cost_usd": 0.0})
                self._state = "failed"
                self._error = ABORTED
                self._progress = []
                done = True
            live = self._live_holder()
            if live is not None:
                (_tier, kind, target_id), _chat_state = live
                try:
                    if kind == "chat":
                        self.chats.abort(target_id)
                    else:
                        self.opus.abort()
                except (ChatConflict, HandoffRefused):
                    pass
                self._target = None
                done = True
            if not done:
                raise OrchestratorConflict(NOTHING_TO_ABORT)

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
                kind = "chat" if v.holder_id.startswith("chat-") else "opus"
                self._target = (v.holder, kind, v.holder_id)
            else:
                self._target = None
            self._resumable = None
            thread = [e for e in events if e.run_id == v.thread_id] if v.thread_id else []
            self._efforts = agent_efforts(thread)
            if self._target is None and v.thread_id:
                self._resumable = last_agent_target(thread)

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

    def _fail(
        self,
        log: PlannerLog,
        reason: str,
        cost: float,
        routing_ms: int | None = None,
        gen: int | None = None,
    ) -> None:
        with self._lock:
            if self._closed or (gen is not None and gen != self._gen):
                return
            payload: dict[str, Any] = {"reason": reason, "cost_usd": cost}
            if routing_ms is not None:
                payload["routing_ms"] = routing_ms
            log.emit("orchestrator_failed", payload)
            self._state = "failed"
            self._error = reason
            self._progress = []

    def _thread_main(
        self,
        text: str,
        holder: Target | None,
        entries: list[OrchEntry],
        log: PlannerLog,
        images: tuple[str, ...] = (),
        gen: int = 0,
        holder_effort: Effort | None = None,
        holder_role: ChatRole | None = None,
    ) -> None:
        try:
            asyncio.run(
                self._route(text, holder, entries, log, images, gen, holder_effort, holder_role)
            )
        except asyncio.CancelledError:
            return
        except Exception as err:
            self._fail(log, f"unexpected error: {type(err).__name__}: {err}", 0.0, gen=gen)

    def _start_target(
        self,
        to: Tier,
        plan: Literal["quick", "project"],
        text: str,
        images: tuple[str, ...] = (),
        effort: Effort = DEFAULT_EFFORT,
        role: ChatRole = DEFAULT_ROLE,
    ) -> Target:
        if role == "researcher":  # a direct agent on any tier, Opus included
            return (to, "chat", self._start_chat(to, text, images, effort, role))
        if to == "opus":
            # older desks take neither images nor effort (see test_old_opus_signature)
            extra: dict[str, Any] = {"images": images} if images else {}
            if _takes(self.opus.start, "effort"):
                extra["effort"] = effort
            return ("opus", "opus", self.opus.start(text, plan, **extra))
        return (to, "chat", self._start_chat(to, text, images, effort, role))

    def _start_chat(
        self, to: Tier, text: str, images: tuple[str, ...], effort: Effort, role: ChatRole
    ) -> str:
        # older chat managers (test fakes) take no role
        if _takes(self.chats.start, "role"):
            return self.chats.start(to, text, images=images, effort=effort, role=role)
        return self.chats.start(to, text, images=images, effort=effort)

    async def _route(
        self,
        text: str,
        holder: Target | None,
        entries: list[OrchEntry],
        log: PlannerLog,
        images: tuple[str, ...] = (),
        gen: int = 0,
        holder_effort: Effort | None = None,
        holder_role: ChatRole | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        current: Effort = holder_effort or DEFAULT_EFFORT
        prompt = ORCHESTRATOR_PROMPT.format(
            repo=self.repo,
            holder=holder[0] if holder else "nobody (new work)",
            role=(holder_role or "planner") if holder else "none",
            effort=current if holder else "none (new work)",
            tail=tail_text(entries),
            text=text + attachment_note(self.repo, images),
        )

        async def progress_cb(info: dict[str, Any]) -> None:
            with self._lock:
                self._progress = [*self._progress, _progress_line(info)][-PROGRESS_LINES:]

        started = time.monotonic()
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
        routing_ms = round((time.monotonic() - started) * 1000)
        cost = turn.usage.cost_usd
        if turn.error:
            self._fail(log, f"orchestrator session failed: {turn.error}", cost, routing_ms, gen)
            return
        routing = parse_routing(turn.text)
        with self._lock:
            if self._closed or gen != self._gen:
                return
        action: OrchAction
        to: Tier = "sonnet"
        effort: Effort = DEFAULT_EFFORT
        role: ChatRole = holder_role or DEFAULT_ROLE
        dispatch_start = time.monotonic()
        try:
            if holder is None:
                routing = routing or Routing(to="sonnet", reason=NO_ROUTING_NEW)
                to = routing.to
                effort = routing.effort
                role = routing.role
                action = "start"
                target = self._start_target(to, routing.plan, text, images, effort, role)
            elif (
                routing is not None
                and holder[1] == "chat"
                and routing.role != (holder_role or DEFAULT_ROLE)
            ):
                # a different kind of work (build <-> research): a new agent of that role
                # takes over with the thread's tail, whatever the tier
                action = "handoff"
                to = routing.to
                effort = routing.effort
                role = routing.role
                brief = handoff_brief(entries, text, holder[0])
                try:
                    self.chats.close_chat(holder[2])
                except ChatConflict:
                    pass
                target = self._start_target(to, routing.plan, brief, images, effort, role)
            elif (
                routing is not None
                and routing.to in CHAT_TIERS
                and rank(routing.to) < rank(holder[0])
                and holder[1] == "chat"
            ):
                action = "switch"  # easier than the holder: a cheaper, faster agent takes it
                to = routing.to
                effort = routing.effort
                brief = handoff_brief(entries, text, holder[0])
                try:
                    self.chats.close_chat(holder[2])
                except ChatConflict:
                    pass
                target = self._start_target(to, routing.plan, brief, images, effort, role)
            elif routing is None or rank(routing.to) <= rank(holder[0]):
                to = holder[0]
                effort = current
                target = holder
                if (
                    routing is not None
                    and holder[1] == "chat"
                    and big_effort_jump(current, routing.effort)
                ):
                    # same model, a low <-> high jump: the agent restarts on its own Claude
                    # session at the new effort (a one-step change keeps the agent as is)
                    if self.chats.retune(holder[2], routing.effort, text, images=images):
                        action = "retune"
                        effort = routing.effort
                    else:
                        action = "forward"  # no saved session: the chat noted it, kept effort
                else:
                    action = "forward"
                    self.chats.message(holder[2], text, images=images)
                routing = Routing(
                    to=holder[0],
                    plan=routing.plan if routing else "project",
                    effort=effort,
                    reason=routing.reason if routing else NO_ROUTING_FOLLOW,
                )
            else:
                action = "escalate"
                to = routing.to
                effort = routing.effort
                brief = handoff_brief(entries, text, holder[0])
                try:
                    self.chats.close_chat(holder[2])
                except ChatConflict:
                    pass
                target = self._start_target(to, routing.plan, brief, images, effort, role)
        except (ChatConflict, HandoffRefused) as err:
            self._fail(log, f"could not hand to {to}: {err}", cost, routing_ms, gen)
            return
        with self._lock:
            if self._closed or gen != self._gen:
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
                    "effort": effort,
                    "role": role if target[1] == "chat" else "planner",
                    "cost_usd": cost,
                    "routing_ms": routing_ms,
                    # timing breakdown for the Timing panel (harness.ui.timing)
                    "routing_connect_ms": turn.connect_ms,
                    "routing_api_ms": turn.api_ms,
                    "dispatch_ms": round((time.monotonic() - dispatch_start) * 1000),
                    "text": routing_line(
                        action,
                        target[0],
                        routing.plan,
                        routing.reason,
                        effort,
                        current,
                        role if target[1] == "chat" else None,
                    ),
                },
            )
            self._efforts[target[2]] = effort
            self._target = target
            self._state = "idle"
            self._error = None
            self._progress = []
