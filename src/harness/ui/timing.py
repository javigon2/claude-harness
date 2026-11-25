"""Where the time of one Orchestrator conversation went, for debugging: routing, hand-off,
agent startup, the model thinking and writing, tools, and harness overhead.

Built only from the logs. A conversation is an Orchestrator thread (`orch-…` in the planner
log); each user message opens an exchange that lasts until the next one. Inside it:

- routing: `orchestrator_turn.routing_ms` (the Orchestrator's one-turn session);
- the agent's turns: chat turns (`chat_user` -> `chat_reply`/`chat_failed` in the chat log)
  or Opus turns (`planner_message` -> `planner_reply`/`planner_turn_failed`), each split by
  its own `connect_ms` (starting the Claude process), `api_ms` (model calls: thinking and
  writing) and the tool durations logged inside it; what is left of the turn is "other
  agent time" (the SDK and process between calls);
- harness: a chat turn's `git_ms` (work branch, snapshots, commit) and, for Opus, the gaps
  between its turns (plan checks, work-order validation);
- hand-off: what is left of the exchange up to the agent's last reply (dispatch, queues,
  threads).

The time the user takes to send the next message is not counted.

Older logs lack some fields; their turns count as unsplit agent time and a note says so."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from harness.models import Event
from harness.planner_log import read_planner_events
from harness.titles import read_title
from harness.ui.chat import read_chat_events

SEGMENTS: list[tuple[str, str]] = [
    ("routing", "Routing (Orchestrator)"),
    ("handoff", "Hand-off and queueing"),
    ("startup", "Agent startup"),
    ("model", "Model thinking and writing"),
    ("tools", "Running tools"),
    ("agent_other", "Other agent time"),
    ("harness", "Harness overhead (git, checks)"),
]
ENDS_CHAT_TURN = ("chat_reply", "chat_failed", "chat_closed")
ENDS_OPUS_TURN = ("planner_reply", "planner_turn_failed")


class ToolTime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    count: int
    total_ms: int
    max_ms: int


class ExchangeTiming(BaseModel):
    """One user message and everything it set off, up to the next one. `ms` has one value
    per SEGMENTS key; `response_ms` runs from the message to the agent's last reply."""

    model_config = ConfigDict(extra="forbid")

    index: int
    ts: str
    text: str
    action: str | None = None
    to: str | None = None
    effort: str | None = None
    turns: int = 0
    response_ms: int = 0
    ms: dict[str, int] = Field(default_factory=dict)
    routing_detail: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class ThreadTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    title: str | None = None
    wall_ms: int = 0
    labels: dict[str, str] = Field(default_factory=lambda: dict(SEGMENTS))
    totals: dict[str, int] = Field(default_factory=dict)
    exchanges: list[ExchangeTiming] = Field(default_factory=list)
    tools: list[ToolTime] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, round(value))


def _gap(a: datetime, b: datetime) -> int:
    return max(0, round((b - a).total_seconds() * 1000))


def _turns(
    events: list[Event], start_type: str, ends: tuple[str, ...]
) -> list[tuple[Event, Event]]:
    """(start, end) pairs, in order; a start without an end is dropped."""
    out: list[tuple[Event, Event]] = []
    pending: Event | None = None
    for e in events:
        if e.type == start_type:
            pending = e
        elif e.type in ends and pending is not None:
            out.append((pending, e))
            pending = None
    return out


def _split_turn(
    start: Event, end: Event, tool_events: list[Event], tools: dict[str, list[int]]
) -> tuple[dict[str, int], int, bool]:
    """(segments, harness_ms, measured) of one agent turn. `tools` gathers per-tool totals."""
    p = end.payload
    turn_ms = _ms(p.get("duration_ms"))
    measured = turn_ms is not None
    if turn_ms is None:
        turn_ms = _gap(start.ts, end.ts)
    used = [e for e in tool_events if start.ts <= e.ts <= end.ts]
    tool_ms = 0
    for e in used:
        name = e.payload.get("tool")
        d = _ms(e.payload.get("duration_ms")) or 0
        tool_ms += d
        if isinstance(name, str):
            row = tools.setdefault(name, [0, 0, 0])
            row[0] += 1
            row[1] += d
            row[2] = max(row[2], d)
    connect = _ms(p.get("connect_ms")) or 0
    api = _ms(p.get("api_ms"))
    if api is None:  # no model time logged: what is not tools or startup is unsplit
        api = 0
        measured = False
    connect = min(connect, turn_ms)
    api = min(api, turn_ms - connect)
    tool_ms = min(tool_ms, turn_ms - connect - api)
    seg = {
        "startup": connect,
        "model": api,
        "tools": tool_ms,
        "agent_other": turn_ms - connect - api - tool_ms,
    }
    return seg, _ms(p.get("git_ms")) or 0, measured


def build_thread_timing(repo: Path, thread_id: str) -> ThreadTiming | None:
    """The breakdown of Orchestrator thread `thread_id`; None when the log has no such
    thread."""
    planner = read_planner_events(repo)
    thread = [e for e in planner if e.run_id == thread_id]
    users = [e for e in thread if e.type == "orchestrator_user"]
    if not any(e.type == "orchestrator_started" for e in thread):
        return None
    chats = read_chat_events(repo)
    tools: dict[str, list[int]] = {}
    exchanges: list[ExchangeTiming] = []
    unsplit = False
    for i, user in enumerate(users):
        end = users[i + 1].ts if i + 1 < len(users) else None
        window = [e for e in thread if e.ts >= user.ts and (end is None or e.ts < end)]
        turn = next(
            (e for e in window if e.type in ("orchestrator_turn", "orchestrator_failed")), None
        )
        text = str(user.payload.get("text") or "")
        ex = ExchangeTiming(index=i + 1, ts=user.ts.isoformat(), text=text[:120])
        ms = {key: 0 for key, _ in SEGMENTS}
        ms["routing"] = (_ms(turn.payload.get("routing_ms")) or 0) if turn is not None else 0
        last = turn.ts if turn is not None else user.ts
        if turn is not None:
            p = turn.payload
            ex.action = p.get("action") if turn.type == "orchestrator_turn" else "failed"
            ex.to = p.get("to")
            if isinstance(p.get("effort"), str):
                ex.effort = p["effort"]
            for key in ("routing_connect_ms", "routing_api_ms", "dispatch_ms"):
                if (v := _ms(p.get(key))) is not None:
                    ex.routing_detail[key.removesuffix("_ms")] = v
            kind, target = p.get("target_kind"), p.get("target_id")

            def inside(e: Event, lo: datetime = user.ts, hi: datetime | None = end) -> bool:
                return e.ts >= lo and (hi is None or e.ts < hi)

            pairs: list[tuple[Event, Event]] = []
            tool_events: list[Event] = []
            if kind == "chat" and isinstance(target, str):
                mine = [e for e in chats if e.run_id == target and inside(e)]
                pairs = _turns(mine, "chat_user", ENDS_CHAT_TURN)
                tool_events = [e for e in mine if e.type == "chat_tool"]
            elif kind == "opus" and isinstance(target, str):
                mine = [e for e in planner if e.run_id == target and inside(e)]
                pairs = _turns(mine, "planner_message", ENDS_OPUS_TURN)
                tool_events = [e for e in mine if e.type == "planner_tool_finished"]
            prev_end: Event | None = None
            for start, stop in pairs:
                seg, git_ms, measured = _split_turn(start, stop, tool_events, tools)
                unsplit = unsplit or not measured
                for key, value in seg.items():
                    ms[key] += value
                ms["harness"] += git_ms
                if kind == "opus" and prev_end is not None:
                    ms["harness"] += _gap(prev_end.ts, start.ts)
                prev_end = stop
                last = max(last, stop.ts)
            ex.turns = len(pairs)
            if not pairs and turn.type == "orchestrator_turn":
                ex.notes.append("no finished agent turn in this exchange (still working?)")
        ex.response_ms = _gap(user.ts, last)
        accounted = sum(v for k, v in ms.items() if k != "handoff")
        ms["handoff"] = max(0, ex.response_ms - accounted)
        ex.ms = ms
        exchanges.append(ex)
    totals = {key: sum(ex.ms.get(key, 0) for ex in exchanges) for key, _ in SEGMENTS}
    wall = 0
    if users:
        handed = {t.payload.get("target_id") for t in thread if t.type == "orchestrator_turn"}
        agents = [e.ts for e in [*chats, *planner] if e.run_id in handed]  # chats and Opus
        tail = max([e.ts for e in thread] + agents)
        wall = _gap(users[0].ts, tail)
    notes = []
    if unsplit:
        notes.append(
            "Some turns come from an older log without startup/model timing: their time is "
            "counted as other agent time."
        )
    return ThreadTiming(
        thread_id=thread_id,
        title=read_title(repo, thread_id),
        wall_ms=wall,
        totals=totals,
        exchanges=exchanges,
        tools=sorted(
            (ToolTime(tool=k, count=v[0], total_ms=v[1], max_ms=v[2]) for k, v in tools.items()),
            key=lambda t: t.total_ms,
            reverse=True,
        ),
        notes=notes,
    )
