"""Append-only JSONL event log for a run: <run_dir>/events.jsonl."""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from harness.models import Event

EVENTS_FILE = "events.jsonl"


class EventLog:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        """Create run_dir (parents too) if missing. self.path = run_dir / EVENTS_FILE.
        self.run_id = run_id. One asyncio.Lock per instance."""
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path: Path = run_dir / EVENTS_FILE
        self.run_id: str = run_id
        self._lock = asyncio.Lock()

    async def emit(
        self, type: str, task_id: str | None = None, payload: dict[str, Any] | None = None
    ) -> Event:
        """Build Event(run_id, task_id, type, payload or {}). Under the lock, open self.path in
        append mode (UTF-8), write event.model_dump_json() + "\\n" in one write() call, flush.
        Return the event."""
        event = Event(run_id=self.run_id, task_id=task_id, type=type, payload=payload or {})
        line = event.model_dump_json() + "\n"
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        return event


def read_events(path: Path) -> list[Event]:
    """Parse every non-blank line with Event.model_validate_json, in file order."""
    events: list[Event] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(Event.model_validate_json(line))
    return events


def follow(path: Path, poll_interval: float = 0.5) -> Iterator[str]:
    """Wait until path exists, yield every existing line (without the newline), then keep
    yielding new complete lines as they are appended, sleeping poll_interval between polls.
    Never returns. Skip blank lines."""
    while not path.exists():
        time.sleep(poll_interval)
    with path.open("r", encoding="utf-8") as handle:
        buffer = ""
        while True:
            chunk = handle.readline()
            if not chunk:
                time.sleep(poll_interval)
                continue
            buffer += chunk
            if not buffer.endswith("\n"):
                # Partial line: keep it until the rest is appended.
                continue
            line = buffer[:-1]
            buffer = ""
            if line.strip():
                yield line
