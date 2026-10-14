"""Pydantic models shared by the plan loader, runner and event log."""

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.config import Tier

TaskStatus = Literal["pending", "running", "verifying", "done", "failed", "blocked"]

_ID_PATTERN = re.compile(r"[a-z0-9-]+")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextItem(_Strict):
    label: str
    content: str


class Budget(_Strict):
    max_turns: int = Field(ge=1)
    max_attempts: int = Field(ge=1)


class WorkOrder(_Strict):
    id: str
    title: str
    goal: str
    tier: Tier
    depends_on: list[str]
    files_owned: list[str] = Field(min_length=1)
    files_readonly: list[str]
    context: list[ContextItem]
    steps: list[str]
    constraints: list[str]
    acceptance: list[str] = Field(min_length=1)
    budget: Budget
    out_of_scope: list[str]

    @field_validator("id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        lowered = value.lower()
        if not _ID_PATTERN.fullmatch(lowered):
            raise ValueError("id must match [a-z0-9-]+")
        return lowered


class Plan(_Strict):
    stage: str
    orders: list[WorkOrder]


class Usage(_Strict):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


class TaskState(_Strict):
    task_id: str
    status: TaskStatus = "pending"
    attempts: int = 0
    tier_used: Tier | None = None
    usage: Usage = Field(default_factory=Usage)


class Event(_Strict):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    task_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
