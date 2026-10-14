import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.events import EventLog, read_events

runner = CliRunner()


async def test_creates_run_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "run" / "r1"
    EventLog(run_dir, "r1")
    assert run_dir.is_dir()


async def test_single_emit_round_trip(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "run" / "r1", "r1")
    event = await log.emit("run_started", payload={"n": 1})
    assert read_events(log.path) == [event]


async def test_three_emits_keep_order(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "r1", "r1")
    returned = [await log.emit("step", task_id=f"t{i}") for i in range(3)]
    assert read_events(log.path) == returned


async def test_concurrent_emits_stay_whole(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "r1", "r1")

    async def worker(worker_id: int) -> None:
        for i in range(50):
            await log.emit("tick", task_id=f"w{worker_id}", payload={"i": i})

    await asyncio.gather(*(worker(w) for w in range(20)))

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1000
    for line in lines:
        json.loads(line)
    assert len(read_events(log.path)) == 1000


def test_cli_prints_raw_lines(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    log = EventLog(run_dir, "r1")

    async def emit_two() -> None:
        await log.emit("a")
        await log.emit("b")

    asyncio.run(emit_two())

    result = runner.invoke(app, ["events", str(run_dir)])
    assert result.exit_code == 0
    expected = log.path.read_text(encoding="utf-8").splitlines()
    assert result.output.splitlines() == expected
    assert len(result.output.splitlines()) == 2


def test_cli_missing_log_exits_1(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["events", str(empty)])
    assert result.exit_code == 1
    assert "No event log at" in result.output
