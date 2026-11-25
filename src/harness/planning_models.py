"""Data shapes for the planning timing report (`harness planning-report`)."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolTiming:
    tool: str
    count: int
    total_ms: int
    max_ms: int
    missing_result: int


@dataclass(frozen=True)
class TurnTiming:
    phase: str
    duration_ms: int | None
    cost_usd: float | None
    wait_before_ms: int | None


@dataclass(frozen=True)
class SessionReport:
    session_id: str
    stage: int | None
    route: str | None
    turns: list[TurnTiming] = field(default_factory=list)
    tools: list[ToolTiming] = field(default_factory=list)
    phase_ms: dict[str, int] = field(default_factory=dict)
    model_ms: int = 0
    cost_usd: float = 0.0
