import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.models import AttemptRecord, TaskState, Usage
from harness.state import (
    STATE_FILE,
    RunState,
    StateError,
    latest_run,
    load_state,
    new_run_id,
    run_dir,
    runs_root,
    save_state,
)


def test_new_run_id_utc() -> None:
    assert new_run_id(datetime(2026, 10, 14, 9, 30, 5, tzinfo=UTC)) == "20261014-093005"


def test_new_run_id_naive_is_utc() -> None:
    assert new_run_id(datetime(2026, 10, 14, 9, 30, 5)) == "20261014-093005"


def test_new_run_id_converts_to_utc() -> None:
    tz = timezone(timedelta(hours=2))
    assert new_run_id(datetime(2026, 10, 14, 11, 30, 5, tzinfo=tz)) == "20261014-093005"


def test_new_run_id_default() -> None:
    assert re.fullmatch(r"\d{8}-\d{6}", new_run_id())


def test_paths(tmp_path: Path) -> None:
    assert runs_root(tmp_path) == tmp_path / ".harness" / "runs"
    assert run_dir(tmp_path, "r1") == tmp_path / ".harness" / "runs" / "r1"


def _state() -> RunState:
    record = AttemptRecord(
        attempt=1,
        tier="sonnet",
        worker_status="done",
        passed=True,
        usage=Usage(input_tokens=3, cost_usd=0.5),
        summary="ok",
    )
    return RunState(
        run_id="r1",
        plan_path="plans/p.json",
        stage="s1",
        tasks={
            "zeta": TaskState(task_id="zeta", status="done", attempts=1, history=[record]),
            "alpha": TaskState(task_id="alpha"),
        },
    )


def test_round_trip_keeps_order(tmp_path: Path) -> None:
    state = _state()
    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert loaded == state
    assert list(loaded.tasks) == ["zeta", "alpha"]
    assert loaded.tasks["zeta"].history[0].summary == "ok"


def test_save_creates_parents_no_tmp_bumps_updated(tmp_path: Path) -> None:
    directory = tmp_path / "a" / "b" / "r1"
    state = _state()
    old = datetime(2000, 1, 1, tzinfo=UTC)
    state.updated_at = old
    save_state(directory, state)
    assert (directory / STATE_FILE).is_file()
    assert not (directory / (STATE_FILE + ".tmp")).exists()
    assert state.updated_at > old
    text = (directory / STATE_FILE).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert load_state(directory).updated_at == state.updated_at


def test_load_missing(tmp_path: Path) -> None:
    with pytest.raises(StateError) as exc:
        load_state(tmp_path / "nope")
    assert str(exc.value) == f"No run state at {tmp_path / 'nope' / STATE_FILE}"


def test_load_invalid_json(tmp_path: Path) -> None:
    (tmp_path / STATE_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError) as exc:
        load_state(tmp_path)
    assert str(exc.value).startswith("Invalid run state at ")


def test_load_missing_run_id(tmp_path: Path) -> None:
    (tmp_path / STATE_FILE).write_text(
        json.dumps({"plan_path": "p", "stage": "s"}), encoding="utf-8"
    )
    with pytest.raises(StateError) as exc:
        load_state(tmp_path)
    assert str(exc.value).startswith("Invalid run state at ")


def test_latest_run(tmp_path: Path) -> None:
    for name in ["20261014-093005", "20261015-000000"]:
        save_state(run_dir(tmp_path, name), RunState(run_id=name, plan_path="p", stage="s"))
    run_dir(tmp_path, "20991231-235959").mkdir(parents=True)
    assert latest_run(tmp_path) == "20261015-000000"


def test_latest_run_none(tmp_path: Path) -> None:
    assert latest_run(tmp_path) is None
    runs_root(tmp_path).mkdir(parents=True)
    assert latest_run(tmp_path) is None
