"""Build per-session planning timing reports from the planner event log."""

from pathlib import Path

from harness.models import Event
from harness.planner_log import read_planner_events
from harness.planning_models import SessionReport, ToolTiming, TurnTiming


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _report(session_id: str, events: list[Event]) -> SessionReport:
    stage: int | None = None
    route: str | None = None
    turns: list[TurnTiming] = []
    tool_stats: dict[str, list[int]] = {}  # tool -> [count, total_ms, max_ms, missing]
    last_reply: Event | None = None
    pending: Event | None = None  # the planner_message awaiting its reply
    for event in events:
        payload = event.payload
        if event.type == "planner_started":
            stage = _int_or_none(payload.get("stage"))
        elif event.type in ("planner_triage", "planner_done"):
            if isinstance(payload.get("route"), str):
                route = payload["route"]
        elif event.type == "planner_message":
            pending = event
        elif event.type in ("planner_reply", "planner_turn_failed"):
            phase = payload.get("phase")
            if pending is not None and isinstance(pending.payload.get("phase"), str):
                phase = pending.payload["phase"]
            wait: int | None = None
            if pending is not None and last_reply is not None:
                wait = max(0, round((pending.ts - last_reply.ts).total_seconds() * 1000))
            turns.append(
                TurnTiming(
                    phase=phase if isinstance(phase, str) else "?",
                    duration_ms=_int_or_none(payload.get("duration_ms")),
                    cost_usd=_float_or_none(payload.get("cost_usd")),
                    wait_before_ms=wait,
                )
            )
            pending = None
            if event.type == "planner_reply":
                last_reply = event
        elif event.type == "planner_tool_finished":
            name = payload.get("tool")
            if not isinstance(name, str):
                continue
            stats = tool_stats.setdefault(name, [0, 0, 0, 0])
            stats[0] += 1
            if payload.get("status") == "missing_result":
                stats[3] += 1
                continue
            ms = _int_or_none(payload.get("duration_ms")) or 0
            stats[1] += ms
            stats[2] = max(stats[2], ms)
    phase_ms: dict[str, int] = {}
    for turn in turns:
        if turn.duration_ms is not None:
            phase_ms[turn.phase] = phase_ms.get(turn.phase, 0) + turn.duration_ms
    cost = 0.0
    if last_reply is not None:
        cost = _float_or_none(last_reply.payload.get("cost_usd")) or 0.0
    return SessionReport(
        session_id=session_id,
        stage=stage,
        route=route,
        turns=turns,
        tools=[ToolTiming(name, s[0], s[1], s[2], s[3]) for name, s in tool_stats.items()],
        phase_ms=phase_ms,
        model_ms=sum(phase_ms.values()),
        cost_usd=cost,
    )


def build_reports(repo: Path, last: int = 5, session_id: str | None = None) -> list[SessionReport]:
    """Reports for planner sessions in log order: the newest `last`, or only `session_id`
    (empty if unknown). A session's events are those sharing its `run_id`."""
    by_session: dict[str, list[Event]] = {}
    for event in read_planner_events(repo):
        by_session.setdefault(event.run_id, []).append(event)
    ids = list(by_session)
    if session_id is not None:
        ids = [i for i in ids if i == session_id]
    else:
        ids = ids[-last:] if last > 0 else []
    return [_report(i, by_session[i]) for i in ids]
