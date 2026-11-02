"""Mechanical quality checks on planner-written work orders (no model call)."""

import shlex
from pathlib import Path, PurePosixPath

from harness.models import Plan

MAX_CONTEXT_ITEM_CHARS = 6000
MAX_CONTEXT_TOTAL_CHARS = 24000


def _bad_path(path: str) -> bool:
    p = PurePosixPath(path)
    return p.is_absolute() or ".." in p.parts


def check_plan_quality(plan: Plan, repo: Path) -> list[str]:
    """One string per problem found in `plan`, [] when fine. File contents are never read;
    `files_readonly` may be created by any transitive dependency."""
    repo = Path(repo)
    by_id = {o.id: o for o in plan.orders}

    def upstream_owned(order_id: str) -> set[str]:
        owned: set[str] = set()
        seen: set[str] = set()
        stack = list(by_id[order_id].depends_on)
        while stack:
            dep = stack.pop()
            if dep in seen or dep not in by_id:
                continue
            seen.add(dep)
            owned.update(by_id[dep].files_owned)
            stack.extend(by_id[dep].depends_on)
        return owned

    problems: list[str] = []
    for order in plan.orders:
        for path in order.files_owned:
            if _bad_path(path):
                problems.append(
                    f"{order.id}: files_owned {path} must be a relative path without .."
                )
        upstream: set[str] | None = None
        for path in order.files_readonly:
            if _bad_path(path) or (repo / path).exists():
                continue
            if upstream is None:
                upstream = upstream_owned(order.id)
            if path not in upstream:
                problems.append(
                    f"{order.id}: files_readonly {path} does not exist and no earlier order "
                    "creates it"
                )
        for item in order.context:
            if len(item.content) > MAX_CONTEXT_ITEM_CHARS:
                problems.append(
                    f"{order.id}: context item '{item.label}' is {len(item.content)} chars "
                    f"(max {MAX_CONTEXT_ITEM_CHARS})"
                )
        total = sum(len(i.content) for i in order.context)
        if total > MAX_CONTEXT_TOTAL_CHARS:
            problems.append(
                f"{order.id}: context totals {total} chars (max {MAX_CONTEXT_TOTAL_CHARS})"
            )
        if not order.steps:
            problems.append(f"{order.id}: no steps")
        for command in order.acceptance:
            try:
                shlex.split(command)
            except ValueError:
                problems.append(f"{order.id}: acceptance command does not parse: {command}")
    return problems
