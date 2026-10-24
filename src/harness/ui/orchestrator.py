"""The Orchestrator: the office's router. Every request typed to it goes to a one-turn Sonnet
session that picks who takes the work (a Haiku or Sonnet chat, or Opus planning) and never
answers or edits anything itself. Follow-ups stay with the current holder or move up a tier.

Every turn is written to the planner event log (`run_id` = the thread id, `orch-<stamp>`), so
a restart or a read-only page rebuilds the thread with `orchestrator_view_from_log`."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from harness.config import TIER_LADDER, Tier
from harness.models import Event
from harness.planner import PlannerSession
from harness.planner_log import last_session, read_planner_events
from harness.state import new_run_id
from harness.ui.chat import _pid_alive, chats_from_log, read_chat_events
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
