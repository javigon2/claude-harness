"""Model IDs and the escalation ladder. The only place model IDs live."""

from typing import Literal

Tier = Literal["haiku", "sonnet", "opus"]

TIER_LADDER: tuple[Tier, ...] = ("haiku", "sonnet", "opus")

MODEL_IDS: dict[Tier, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
