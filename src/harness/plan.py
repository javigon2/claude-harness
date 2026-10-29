"""Load a plan file and check it: ids, dependencies, cycles, ownership, acceptance."""

import json
import re
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from harness.models import Plan


class PlanError(ValueError):
    """The plan file is invalid. The message says why."""


# `pytest`, `uv run pytest -q`, `python -m pytest -x`: pytest with only flags, no test path
_WHOLE_SUITE = re.compile(r"^(?:uv run |python -m )?(?:pytest|py\.test)(?:\s+-\S+)*\s*$")


def drop_whole_suite(plan: Plan) -> None:
    """Remove bare whole-suite pytest commands from every order's acceptance, so agents run
    only targeted checks (the full suite runs on demand from the page). An order whose only
    command is a whole-suite one keeps it, because acceptance cannot be empty."""
    for order in plan.orders:
        kept = [c for c in order.acceptance if not _WHOLE_SUITE.match(c.strip())]
        if kept:
            order.acceptance = kept


def load_plan(path: Path) -> Plan:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise PlanError(f"invalid JSON: {err}") from err
    try:
        plan = Plan.model_validate(data)
    except ValidationError as err:
        raise PlanError(f"invalid plan: {err}") from err
    validate_plan(plan)
    drop_whole_suite(plan)
    return plan


def validate_plan(plan: Plan) -> list[list[str]]:
    seen: set[str] = set()
    for order in plan.orders:
        if order.id in seen:
            raise PlanError(f"duplicate id: {order.id}")
        seen.add(order.id)

    for order in plan.orders:
        for dep in order.depends_on:
            if dep not in seen:
                raise PlanError(f"unknown dependency: {order.id} depends on {dep}")

    for order in plan.orders:
        if any(not cmd.strip() for cmd in order.acceptance):
            raise PlanError(f"empty acceptance: {order.id}")

    waves = compute_waves(plan)

    # Tasks start as soon as their dependencies are done, so two orders that own the same
    # file must be ordered by a (transitive) depends_on path.
    by_id = {o.id: o for o in plan.orders}
    ancestors: dict[str, set[str]] = {}
    for wave in waves:
        for oid in wave:
            found: set[str] = set()
            for dep in by_id[oid].depends_on:
                found.add(dep)
                found |= ancestors[dep]
            ancestors[oid] = found
    owners: dict[str, list[str]] = {}
    for order in plan.orders:
        for p in order.files_owned:
            key = str(PurePosixPath(p))
            for other in owners.setdefault(key, []):
                if other not in ancestors[order.id] and order.id not in ancestors[other]:
                    raise PlanError(
                        f"overlapping files_owned: {key} ({other}, {order.id}) "
                        "are not ordered by depends_on"
                    )
            owners[key].append(order.id)
    return waves


def compute_waves(plan: Plan) -> list[list[str]]:
    remaining = {o.id: set(o.depends_on) for o in plan.orders}
    placed: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        wave = sorted(oid for oid, deps in remaining.items() if deps <= placed)
        if not wave:
            raise PlanError(f"cycle: {', '.join(sorted(remaining))}")
        waves.append(wave)
        placed.update(wave)
        for oid in wave:
            del remaining[oid]
    return waves
