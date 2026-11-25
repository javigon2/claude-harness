"""Model IDs and the escalation ladder. The only place model IDs live."""

import os
from collections.abc import Mapping
from typing import Literal

Tier = Literal["haiku", "sonnet", "opus"]

TIER_LADDER: tuple[Tier, ...] = ("haiku", "sonnet", "opus")

MODEL_IDS: dict[Tier, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}

PLANNER_TIERS: dict[str, Tier] = {
    "project": "opus",
    "quick": "sonnet",
    "staging": "sonnet",
    "fallback": "opus",
}

PLANNER_ALL_OPUS_ENV = "HARNESS_PLANNER_ALL_OPUS"


def planner_tier(phase: str, env: Mapping[str, str] | None = None) -> Tier:
    """The model tier for a planner phase; opus everywhere when HARNESS_PLANNER_ALL_OPUS=1.
    Raises ValueError for an unknown phase."""
    if phase not in PLANNER_TIERS:
        raise ValueError(f"unknown planner phase: {phase}")
    if (env if env is not None else os.environ).get(PLANNER_ALL_OPUS_ENV) == "1":
        return "opus"
    return PLANNER_TIERS[phase]
