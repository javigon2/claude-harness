"""Render planning timing reports as Markdown."""

from harness.planning_models import SessionReport


def _s(ms: int) -> str:
    return f"{ms / 1000:.1f}"


def _render_one(r: SessionReport) -> str:
    stage = "?" if r.stage is None else str(r.stage)
    route = r.route if r.route is not None else "?"
    lines = [
        f"## {r.session_id} (stage {stage}, route {route})",
        "",
        f"Model time {_s(r.model_ms)} s, cost ${r.cost_usd:.2f}",
        "",
        "| Phase | Turns | Model time (s) |",
        "|---|---|---|",
    ]
    phases: list[str] = []
    for turn in r.turns:
        if turn.phase not in phases:
            phases.append(turn.phase)
    for phase in phases:
        count = sum(1 for t in r.turns if t.phase == phase)
        lines.append(f"| {phase} | {count} | {_s(r.phase_ms.get(phase, 0))} |")
    missing = sum(1 for t in r.turns if t.duration_ms is None)
    if missing:
        lines += ["", f"{missing} turn(s) have no duration (logged before timings existed)."]
    lines.append("")
    if r.tools:
        lines += ["| Tool | Calls | Total (s) | Max (s) | No result |", "|---|---|---|---|---|"]
        for t in sorted(r.tools, key=lambda t: t.total_ms, reverse=True):
            lines.append(
                f"| {t.tool} | {t.count} | {_s(t.total_ms)} | {_s(t.max_ms)} | {t.missing_result} |"
            )
    else:
        lines.append("No tool timings recorded (older session).")
    wait = sum(t.wait_before_ms or 0 for t in r.turns)
    lines += ["", f"User wait between turns: {_s(wait)} s (not model latency)"]
    return "\n".join(lines)


def render_markdown(reports: list[SessionReport]) -> str:
    if not reports:
        return "No planner sessions found."
    return "\n\n".join(_render_one(r) for r in reports) + "\n"
