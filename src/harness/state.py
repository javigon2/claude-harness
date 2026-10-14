"""Run state on disk: <repo>/.harness/runs/<run_id>/state.json, rewritten after every change."""

import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.models import TaskState

STATE_FILE = "state.json"

RUNS_DIR = Path(".harness") / "runs"


class StateError(RuntimeError):
    """The run state is missing or unreadable. The message says which file and why."""


class RunState(BaseModel):
    """Everything the runner knows about one run. `tasks` keeps the plan's order."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    plan_path: str
    stage: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished: bool = False
    tasks: dict[str, TaskState] = Field(default_factory=dict)


def new_run_id(now: datetime | None = None) -> str:
    """The UTC timestamp `YYYYmmdd-HHMMSS` of `now` (default: datetime.now(UTC)), e.g.
    "20261014-093005". A naive `now` is taken as UTC."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    return now.strftime("%Y%m%d-%H%M%S")


def runs_root(repo: Path) -> Path:
    """<repo>/.harness/runs"""
    return repo / RUNS_DIR


def run_dir(repo: Path, run_id: str) -> Path:
    """<repo>/.harness/runs/<run_id>"""
    return runs_root(repo) / run_id


def save_state(directory: Path, state: RunState) -> None:
    """Set state.updated_at = datetime.now(UTC), then write state.model_dump_json(indent=2)
    plus a trailing newline to <directory>/state.json atomically: create `directory`
    (parents too) if missing, write <directory>/state.json.tmp (UTF-8), then
    os.replace it onto state.json. OSError propagates."""
    state.updated_at = datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILE
    tmp = directory / (STATE_FILE + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state(directory: Path) -> RunState:
    """Read and validate <directory>/state.json. Raises StateError(f"No run state at
    {path}") if the file does not exist, and StateError(f"Invalid run state at {path}:
    {err}") if it cannot be read (OSError) or is not a valid RunState (ValueError, which
    covers pydantic's ValidationError). `path` is the state.json path as a str."""
    path = str(directory / STATE_FILE)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StateError(f"No run state at {path}") from None
    except OSError as err:
        raise StateError(f"Invalid run state at {path}: {err}") from err
    try:
        return RunState.model_validate_json(text)
    except (ValidationError, ValueError) as err:
        raise StateError(f"Invalid run state at {path}: {err}") from err


def latest_run(repo: Path) -> str | None:
    """The run id of the newest run under runs_root(repo): the largest directory name, in
    plain string order, among directories that contain a state.json. None if there is
    none (including when runs_root does not exist)."""
    root = runs_root(repo)
    if not root.is_dir():
        return None
    names = [p.name for p in root.iterdir() if p.is_dir() and (p / STATE_FILE).is_file()]
    return max(names) if names else None
