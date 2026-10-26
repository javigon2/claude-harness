"""What the office can launch: the plans under .harness/plans/, the stages still to be turned
into work orders, and the run options stored for the repo."""

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.plan import PlanError, load_plan, validate_plan
from harness.planner import PLANS_DIR, PROJECT_PLAN_FILE
from harness.state import StateError, load_state, run_dir
from harness.ui.view import list_runs

RUN_OPTIONS_FILE = Path(".harness") / "office" / "run_options.json"

_APPROVED_RE = re.compile(r"^Status: approved ?(?:\d{4}-\d{2}-\d{2})?$", re.MULTILINE)
_STAGE_HEADING_RE = re.compile(r"^## Stage (\d+): ", re.MULTILINE)
_STAGE_DIR_RE = re.compile(r"^stage-(\d+)$")


class RunOptions(BaseModel):
    """How the office launches `harness run`."""

    model_config = ConfigDict(extra="forbid")

    review: bool = False
    max_parallel: int = Field(default=4, ge=1, le=16)
    max_cost: float | None = Field(default=None, gt=0)
    fake: bool = False


class PlanEntry(BaseModel):
    """One plan.json under .harness/plans/. `error` is why it cannot run; `last_run` is the
    newest run launched from it."""

    model_config = ConfigDict(extra="forbid")

    path: str  # repo-relative posix
    kind: Literal["stage", "quick"]
    stage: int | None = None
    orders: int = 0
    waves: int = 0
    error: str | None = None
    last_run: str | None = None
    last_status: Literal["running", "done", "stopped"] | None = None


def pid_alive(pid: int | None) -> bool:
    """True if a process with this pid exists (and is ours to signal or not)."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _last_runs(repo: Path) -> dict[str, tuple[str, Literal["running", "done", "stopped"]]]:
    """plan_path -> (run id, status) of the newest run of each plan. A run is `running`
    while unfinished and its process (when known) lives, `done` when finished with every
    task done, else `stopped`."""
    found: dict[str, tuple[str, Literal["running", "done", "stopped"]]] = {}
    for run_id in list_runs(repo):  # newest first
        try:
            state = load_state(run_dir(repo, run_id))
        except StateError:
            continue
        if state.plan_path in found:
            continue
        status: Literal["running", "done", "stopped"]
        if not state.finished:
            status = "running" if state.pid is None or pid_alive(state.pid) else "stopped"
        elif all(t.status == "done" for t in state.tasks.values()):
            status = "done"
        else:
            status = "stopped"
        found[state.plan_path] = (run_id, status)
    return found


def list_plans(repo: Path) -> list[PlanEntry]:
    """Every `stage-<N>/plan.json` by N, then every `quick-*/plan.json`, newest first.
    A plan that does not load or validate has `error` set and no counts."""
    repo = Path(repo)
    root = repo / PLANS_DIR
    if not root.is_dir():
        return []
    stages: list[tuple[int, Path]] = []
    quick: list[Path] = []
    for d in root.iterdir():
        if not (d / "plan.json").is_file():
            continue
        m = _STAGE_DIR_RE.match(d.name)
        if m is not None:
            stages.append((int(m.group(1)), d))
        elif d.name.startswith("quick-"):
            quick.append(d)
    ordered: list[tuple[Literal["stage", "quick"], int | None, Path]] = [
        ("stage", n, d) for n, d in sorted(stages)
    ] + [("quick", None, d) for d in sorted(quick, key=lambda d: d.name, reverse=True)]
    last = _last_runs(repo)
    entries: list[PlanEntry] = []
    for kind, stage, d in ordered:
        path = (d / "plan.json").relative_to(repo).as_posix()
        entry = PlanEntry(path=path, kind=kind, stage=stage)
        try:
            plan = load_plan(d / "plan.json")
            entry.orders = len(plan.orders)
            entry.waves = len(validate_plan(plan))
        except (PlanError, OSError) as err:
            entry.error = str(err)
        if path in last:
            entry.last_run, entry.last_status = last[path]
        entries.append(entry)
    return entries


def unstaged_stages(repo: Path) -> list[int]:
    """The `## Stage N:` headings of an approved PROJECT_PLAN.md whose `stage-N/plan.json`
    does not exist yet, ascending. [] if the plan is missing or still a draft."""
    repo = Path(repo)
    try:
        text = (repo / PROJECT_PLAN_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    if _APPROVED_RE.search(text) is None:
        return []
    numbers = sorted({int(n) for n in _STAGE_HEADING_RE.findall(text)})
    return [n for n in numbers if not (repo / PLANS_DIR / f"stage-{n}" / "plan.json").is_file()]


def load_run_options(repo: Path, default: RunOptions) -> RunOptions:
    """The options stored for `repo`, or `default` if the file is missing or invalid."""
    try:
        text = (Path(repo) / RUN_OPTIONS_FILE).read_text(encoding="utf-8")
        return RunOptions.model_validate_json(text)
    except (OSError, ValidationError, ValueError):
        return default


def save_run_options(repo: Path, options: RunOptions) -> None:
    """Write `options` to RUN_OPTIONS_FILE, creating its directory."""
    path = Path(repo) / RUN_OPTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(options.model_dump_json(indent=2) + "\n", encoding="utf-8")


def options_summary(options: RunOptions) -> str:
    """`review off · 4 parallel · no cost cap · real workers`"""
    cap = "no cost cap" if options.max_cost is None else f"cap ${options.max_cost:.2f}"
    return (
        f"review {'on' if options.review else 'off'} · {options.max_parallel} parallel · "
        f"{cap} · {'fake' if options.fake else 'real'} workers"
    )
