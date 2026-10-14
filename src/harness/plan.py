"""Load a plan file and check it: ids, dependencies, cycles, ownership, acceptance."""

import json
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from harness.models import Plan


class PlanError(ValueError):
    """The plan file is invalid. The message says why."""


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

    by_id = {o.id: o for o in plan.orders}
    for n, wave in enumerate(waves, start=1):
        owners: dict[str, str] = {}
        for oid in wave:
            for p in by_id[oid].files_owned:
                key = str(PurePosixPath(p))
                if key in owners:
                    raise PlanError(
                        f"overlapping files_owned in wave {n}: {key} ({owners[key]}, {oid})"
                    )
                owners[key] = oid
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
