import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def test_new_run_id_converts_offset_time_to_utc() -> None:
    tz = timezone(timedelta(hours=2))
    assert new_run_id(datetime(2026, 10, 14, 11, 30, 5, tzinfo=tz)) == "20261014-093005"


def test_pid_round_trips(tmp_path: Path) -> None:
    d = tmp_path / "run"
    save_state(d, RunState(run_id="r", plan_path="p", stage="s", pid=42))
    assert load_state(d).pid == 42


def test_save_twice_overwrites(tmp_path: Path) -> None:
    d = tmp_path / "run"
    save_state(d, RunState(run_id="r", plan_path="p", stage="s"))
    save_state(d, RunState(run_id="r", plan_path="p", stage="s2"))
    assert load_state(d).stage == "s2"


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "run"
    save_state(d, RunState(run_id="r", plan_path="p", stage="s"))
    path = d / STATE_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["bogus"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError) as excinfo:
        load_state(d)
    assert str(excinfo.value).startswith(f"Invalid run state at {d / STATE_FILE}: ")


def test_latest_run_ignores_dirs_without_state_and_plain_files(tmp_path: Path) -> None:
    repo = tmp_path
    save_state(
        run_dir(repo, "20260101-000000"),
        RunState(run_id="20260101-000000", plan_path="p", stage="s"),
    )
    (runs_root(repo) / "20990101-000000").mkdir()
    (runs_root(repo) / "zzz.txt").write_text("x", encoding="utf-8")
    assert latest_run(repo) == "20260101-000000"
