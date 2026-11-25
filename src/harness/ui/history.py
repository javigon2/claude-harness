"""Dashboard history: "Clear" closes the current period and starts a fresh one, so the
Dashboard tracks new runs only; earlier periods stay viewable under History. Nothing is
deleted: a period is only a time window over the runs and logs that are already on disk.

The file (default ~/.config/agent-orchestrator/dashboard-history.json, next to the recent
projects) holds the closed periods, oldest first; the current period starts where the last
one ended. It spans every project, like the Dashboard's "All projects" overview."""

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.ui.projects import default_recent_path

MAX_LABEL_CHARS = 80

Window = tuple[datetime | None, datetime | None]  # [start, end); None is open-ended
ALL_TIME: Window = (None, None)


class Period(BaseModel):
    """One closed period: runs and costs from `start` (None: the beginning) up to `end`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    start: datetime | None = None
    end: datetime


class History(BaseModel):
    """What the Dashboard's period picker shows. `current_start` is where the current
    period begins (None while nothing was ever cleared)."""

    model_config = ConfigDict(extra="forbid")

    current_start: datetime | None = None
    periods: list[Period] = Field(default_factory=list)


def default_history_path() -> Path:
    return default_recent_path().parent / "dashboard-history.json"


def load_history(path: Path) -> History:
    """The saved history; an empty one when the file is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        periods = [Period.model_validate(p) for p in data.get("periods", [])]
    except (OSError, ValueError, ValidationError, AttributeError, TypeError):
        periods = []
    periods.sort(key=lambda p: p.end)
    return History(current_start=periods[-1].end if periods else None, periods=periods)


def clear_history(path: Path, label: str | None = None, now: datetime | None = None) -> Period:
    """Close the current period at `now` (default: the current UTC time) under `label`
    (default: its date range) and save it; the Dashboard starts fresh from `now`."""
    now = now or datetime.now(UTC)
    history = load_history(path)
    start = history.current_start
    text = (label or "").strip()[:MAX_LABEL_CHARS]
    if not text:
        since = start.strftime("%Y-%m-%d") if start else "start"
        text = f"{since} to {now.strftime('%Y-%m-%d %H:%M')} UTC"
    period = Period(
        id=f"p-{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}",
        label=text,
        start=start,
        end=now,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"periods": [p.model_dump(mode="json") for p in [*history.periods, period]]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return period


def window_of(history: History, period: str | None) -> Window:
    """The time window of `period`: "current" (default) or None, "all", or a period id.
    Raises KeyError for an unknown id."""
    if period in (None, "", "current"):
        return (history.current_start, None)
    if period == "all":
        return ALL_TIME
    for p in history.periods:
        if p.id == period:
            return (p.start, p.end)
    raise KeyError(period)


def in_window(ts: datetime | None, window: Window) -> bool:
    """True when `ts` falls in [start, end). An unknown time counts only for all time."""
    start, end = window
    if ts is None:
        return start is None and end is None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if start is not None and ts < start:
        return False
    return not (end is not None and ts >= end)
