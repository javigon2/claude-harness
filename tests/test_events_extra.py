import itertools
import threading
from pathlib import Path

from harness.events import EventLog, follow, read_events


async def test_emit_defaults(tmp_path: Path) -> None:
    event = await EventLog(tmp_path / "ev", "r1").emit("x")
    assert event.payload == {}
    assert event.task_id is None
    assert event.run_id == "r1"


async def test_read_events_skips_blank_lines(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "ev", "r1")
    event = await log.emit("x")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("\n   \n")
    assert read_events(log.path) == [event]


def test_follow_skips_blank_and_holds_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "partial.jsonl"
    path.write_text("a\n\nb\npart", encoding="utf-8")

    def append_rest() -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("ial\n")

    timer = threading.Timer(0.1, append_rest)
    timer.start()
    try:
        lines = list(itertools.islice(follow(path, 0.01), 3))
    finally:
        timer.cancel()
    assert lines == ["a", "b", "partial"]


def test_follow_waits_for_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "later.jsonl"

    def create_file() -> None:
        path.write_text("x\n", encoding="utf-8")

    timer = threading.Timer(0.1, create_file)
    timer.start()
    try:
        lines = list(itertools.islice(follow(path, 0.01), 1))
    finally:
        timer.cancel()
    assert lines == ["x"]
