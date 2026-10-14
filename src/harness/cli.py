"""Command-line entry point: `harness <command>`."""

import asyncio
from pathlib import Path
from typing import Annotated, cast

import typer

from harness.config import TIER_LADDER, Tier
from harness.events import EVENTS_FILE
from harness.events import follow as follow_lines
from harness.models import Plan
from harness.plan import PlanError, load_plan, validate_plan
from harness.planner import (
    PlannerError,
    PlannerSession,
    SdkPlannerSession,
    check_skills,
    run_planner,
)
from harness.report import money, report_text, status_text
from harness.review import FakeReviewer, Reviewer, SdkReviewer, fake_review
from harness.scheduler import Scheduler, SchedulerError
from harness.state import RunState, StateError, latest_run, load_state, new_run_id
from harness.state import run_dir as run_dir_of
from harness.state import runs_root as runs_root_of
from harness.verify import DEFAULT_TIMEOUT_S
from harness.worker import FakeWorker, SdkWorker, Worker, fake_result

app = typer.Typer(help="Plan-and-execute harness for Claude agents.", no_args_is_help=True)

RepoOpt = Annotated[Path, typer.Option("--repo", help="Target git repo (default: cwd).")]
RunIdOpt = Annotated[str | None, typer.Option("--run-id", help="Run id (default: the latest run).")]


def _fail(message: str) -> None:
    """typer.echo(message, err=True), then raise typer.Exit(code=1)."""
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _load_plan_or_exit(plan: Path) -> Plan:
    """load_plan(plan); on PlanError -> _fail(f"Invalid plan: {err}"); on OSError ->
    _fail(f"Cannot read plan: {err}")."""
    try:
        return load_plan(plan)
    except PlanError as err:
        _fail(f"Invalid plan: {err}")
    except OSError as err:
        _fail(f"Cannot read plan: {err}")
    raise AssertionError("unreachable")


def _make_worker(fake: bool) -> Worker:
    """FakeWorker(default=fake_result("done", summary="fake worker: no changes",
    input_tokens=1000, output_tokens=200)) if fake, else SdkWorker()."""
    if fake:
        return FakeWorker(
            default=fake_result(
                "done", summary="fake worker: no changes", input_tokens=1000, output_tokens=200
            )
        )
    return SdkWorker()


def _make_reviewer(review: bool, fake: bool) -> Reviewer | None:
    """None if not review; FakeReviewer(default=fake_review("pass")) if fake; else
    SdkReviewer()."""
    if not review:
        return None
    if fake:
        return FakeReviewer(default=fake_review("pass"))
    return SdkReviewer()


def _load_run_or_exit(repo: Path, run_id: str | None) -> RunState:
    """The RunState of run_id, or of latest_run(repo) when run_id is None. No run at all
    -> _fail(f"No runs in {runs_root_of(repo)}"); StateError -> _fail(str(err))."""
    rid = run_id or latest_run(repo)
    if rid is None:
        _fail(f"No runs in {runs_root_of(repo)}")
        raise AssertionError("unreachable")
    try:
        return load_state(run_dir_of(repo, rid))
    except StateError as err:
        _fail(str(err))
        raise AssertionError("unreachable") from err


@app.command()
def run(
    plan: Path,
    max_parallel: Annotated[int, typer.Option("--max-parallel", min=1)] = 4,
    review: Annotated[bool, typer.Option("--review", help="Review passing diffs.")] = False,
    review_tier: Annotated[str, typer.Option("--review-tier")] = "sonnet",
    resume: Annotated[bool, typer.Option("--resume", help="Skip tasks already done.")] = False,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id", help="Run id (default: a new UTC timestamp; with --resume, the latest run)."
        ),
    ] = None,
    fake: Annotated[bool, typer.Option("--fake", help="Use a fake worker: no model.")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds per command.")] = (
        DEFAULT_TIMEOUT_S
    ),
    repo: RepoOpt = Path("."),
) -> None:
    """Run a plan."""
    if review_tier not in TIER_LADDER:
        _fail(f"Unknown tier: {review_tier}")
    p = _load_plan_or_exit(plan)
    repo_path = repo.resolve()
    if resume:
        rid = run_id or latest_run(repo_path)
        if rid is None:
            _fail(f"No run to resume in {runs_root_of(repo_path)}")
            raise AssertionError("unreachable")
    else:
        rid = run_id or new_run_id()
    typer.echo(f"Run {rid}: {len(p.orders)} orders, state in {run_dir_of(repo_path, rid)}")
    scheduler = Scheduler(
        p,
        repo_path,
        rid,
        _make_worker(fake),
        plan_path=str(plan),
        max_parallel=max_parallel,
        reviewer=_make_reviewer(review, fake),
        review_tier=cast(Tier, review_tier),
        timeout_s=timeout,
    )
    try:
        state = asyncio.run(scheduler.run(resume=resume))
    except (SchedulerError, StateError) as err:
        _fail(str(err))
        raise AssertionError("unreachable") from err
    typer.echo("")
    typer.echo(report_text(state))
    if any(t.status != "done" for t in state.tasks.values()):
        raise typer.Exit(code=1)


@app.command()
def status(repo: RepoOpt = Path("."), run_id: RunIdOpt = None) -> None:
    """Show task states for a run."""
    typer.echo(status_text(_load_run_or_exit(repo.resolve(), run_id)))


@app.command()
def report(repo: RepoOpt = Path("."), run_id: RunIdOpt = None) -> None:
    """Show tokens and cost per task and per tier."""
    typer.echo(report_text(_load_run_or_exit(repo.resolve(), run_id)))


@app.command()
def validate(plan: Path) -> None:
    """Validate a plan file."""
    p = _load_plan_or_exit(plan)
    waves = validate_plan(p)
    typer.echo(f"OK: {len(p.orders)} orders in {len(waves)} waves")
    for i, wave in enumerate(waves, start=1):
        typer.echo(f"wave {i}: {', '.join(wave)}")


def _make_planner_session(repo: Path) -> PlannerSession:
    """SdkPlannerSession(repo). Tests monkeypatch harness.cli._make_planner_session."""
    return SdkPlannerSession(repo)


def _ask(prompt: str) -> str | None:
    """input(prompt), or None on EOFError."""
    try:
        return input(prompt)
    except EOFError:
        return None


@app.command("plan")
def plan_cmd(
    description: Annotated[str, typer.Argument(help="Short project description.")],
    repo: RepoOpt = Path("."),
) -> None:
    """Plan a project interactively: PROJECT_PLAN.md, then plans/stage-1/plan.json."""
    repo_path = repo.resolve()
    if not repo_path.is_dir():
        _fail(f"Not a directory: {repo_path}")
    try:
        check_skills()
    except PlannerError as err:
        _fail(str(err))
    session = _make_planner_session(repo_path)
    try:
        result = asyncio.run(run_planner(description, repo_path, session, _ask, typer.echo))
    except PlannerError as err:
        _fail(str(err))
        raise AssertionError("unreachable") from err
    typer.echo("")
    typer.echo(f"Plan written to {result.plan_path}")
    typer.echo(f"Planner cost: {money(result.usage.cost_usd)} ({result.rounds} rounds)")
    typer.echo(f"Next: harness run {result.plan_path} --repo {repo_path}")


@app.command()
def events(
    run_dir: Path,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing.")] = False,
) -> None:
    """Print a run's event log (RUN_DIR/events.jsonl)."""
    path = run_dir / EVENTS_FILE
    if follow:
        for line in follow_lines(path):
            typer.echo(line)
        return
    if not path.exists():
        typer.echo(f"No event log at {path}", err=True)
        raise typer.Exit(code=1)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            typer.echo(line)
