"""Plain-text status table and token/cost report for a run, built from its RunState."""

from collections.abc import Sequence

from harness.config import TIER_LADDER, Tier
from harness.models import Usage
from harness.state import RunState


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A left-aligned text table. Column width = the longest of the header and every cell
    in that column. Line 1: headers, each ljust(width), joined by two spaces. Line 2: "-" *
    width per column, joined by two spaces. Then one line per row, cells ljust(width),
    joined by two spaces. Every line is rstripped; lines are joined with "\\n"; no trailing
    newline."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))
    return "\n".join(line.rstrip() for line in lines)


def money(value: float) -> str:
    """f"${value:.4f}", e.g. money(0.0793) == "$0.0793"."""
    return f"${value:.4f}"


def tier_totals(state: RunState) -> dict[Tier, tuple[int, int, Usage]]:
    """For every tier with at least one worker attempt or review: (worker attempts, reviews,
    summed usage). Each AttemptRecord adds 1 attempt and its `usage` to rec.tier; when
    rec.review_tier is not None it also adds 1 review and its `review_usage` to
    rec.review_tier. Keys in TIER_LADDER order."""
    acc: dict[Tier, tuple[int, int, Usage]] = {}
    for ts in state.tasks.values():
        for rec in ts.history:
            attempts, reviews, usage = acc.get(rec.tier, (0, 0, Usage()))
            acc[rec.tier] = (attempts + 1, reviews, usage.plus(rec.usage))
            if rec.review_tier is not None:
                attempts, reviews, usage = acc.get(rec.review_tier, (0, 0, Usage()))
                acc[rec.review_tier] = (attempts, reviews + 1, usage.plus(rec.review_usage))
    return {tier: acc[tier] for tier in TIER_LADDER if tier in acc}


def escalated_tasks(state: RunState) -> list[str]:
    """Task ids, in state.tasks order, whose history uses more than one distinct tier."""
    return [tid for tid, ts in state.tasks.items() if len({r.tier for r in ts.history}) > 1]


def _header(state: RunState) -> str:
    return f"Run {state.run_id}: {state.stage}"


def _usage_cells(u: Usage) -> list[str]:
    return [
        str(u.input_tokens),
        str(u.output_tokens),
        str(u.cache_read_tokens),
        str(u.cache_creation_tokens),
        money(u.cost_usd),
    ]


def status_text(state: RunState) -> str:
    """Exact format in the `decision: report formats` context item."""
    rows = [
        [tid, ts.status, str(ts.attempts), ts.tier_used or "-", money(ts.usage.cost_usd)]
        for tid, ts in state.tasks.items()
    ]
    table = format_table(["task", "status", "attempts", "tier", "cost"], rows)
    return _header(state) + "\n\n" + table


def report_text(state: RunState) -> str:
    """Exact format in the `decision: report formats` context item."""
    sections = [_header(state)]

    task_rows = [
        [tid, ts.status, str(ts.attempts), ts.tier_used or "-", *_usage_cells(ts.usage)]
        for tid, ts in state.tasks.items()
    ]
    sections.append(
        "Per task\n"
        + format_table(
            [
                "task",
                "status",
                "attempts",
                "tier",
                "input",
                "output",
                "cache read",
                "cache write",
                "cost",
            ],
            task_rows,
        )
    )

    totals = tier_totals(state)
    if totals:
        tier_rows = [
            [tier, str(attempts), str(reviews), *_usage_cells(usage)]
            for tier, (attempts, reviews, usage) in totals.items()
        ]
        sections.append(
            "Per tier\n"
            + format_table(
                [
                    "tier",
                    "attempts",
                    "reviews",
                    "input",
                    "output",
                    "cache read",
                    "cache write",
                    "cost",
                ],
                tier_rows,
            )
        )
    else:
        sections.append("Per tier\n(no attempts)")

    tasks = state.tasks.values()
    n = len(state.tasks)
    done = sum(1 for ts in tasks if ts.status == "done")
    failed = sum(1 for ts in tasks if ts.status == "failed")
    blocked = sum(1 for ts in tasks if ts.status == "blocked")
    other = n - done - failed - blocked
    attempts = sum(ts.attempts for ts in tasks)
    total = sum(ts.usage.cost_usd for ts in tasks)
    ids = escalated_tasks(state)
    escalation = f"Escalated: {len(ids)} of {n} tasks"
    if ids:
        escalation += f" ({', '.join(ids)})"
    sections.append(
        f"Totals: {n} tasks: {done} done, {failed} failed, {blocked} blocked, {other} not run; "
        f"{attempts} attempts; cost {money(total)}" + "\n" + escalation
    )

    attention = []
    for tid, ts in state.tasks.items():
        if ts.status not in ("failed", "blocked"):
            continue
        if ts.error:
            reason = ts.error
        elif ts.replan_note:
            reason = ts.replan_note.splitlines()[0]
        else:
            reason = "-"
        attention.append(f"- {tid} ({ts.status}): {reason}")
    if attention:
        sections.append("Needs attention\n" + "\n".join(attention))

    return "\n\n".join(sections)
