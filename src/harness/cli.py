"""Command-line entry point: `harness <command>`."""

import asyncio
import os
import secrets
import signal
from pathlib import Path
from typing import Annotated, NoReturn, cast

import typer

from harness.config import TIER_LADDER, Tier
from harness.events import EVENTS_FILE
from harness.events import follow as follow_lines
from harness.models import Plan, WorkOrder
from harness.plan import PlanError, load_plan, validate_plan
from harness.planner import (
    PlannerError,
    PlannerSession,
    SdkPlannerSession,
    check_skills,
    run_planner,
)
from harness.planner_log import ObservedSession, PlannerLog, new_session_id
from harness.report import money, report_text, status_text
from harness.review import FakeReviewer, Reviewer, SdkReviewer, fake_review
from harness.scheduler import Scheduler, SchedulerError
from harness.state import RunState, StateError, latest_run, load_state, new_run_id
from harness.state import run_dir as run_dir_of
from harness.state import runs_root as runs_root_of
from harness.ui.office import Office, OfficeError
from harness.ui.server import make_server
from harness.verify import DEFAULT_TIMEOUT_S
from harness.worker import FakeProgressStep, FakeWorker, SdkWorker, Worker, fake_result
from harness.worktree import WorktreeError, ensure_excluded

app = typer.Typer(help="Plan-and-execute harness for Claude agents.", no_args_is_help=True)

RepoOpt = Annotated[Path, typer.Option("--repo", help="Target git repo (default: cwd).")]
RunIdOpt = Annotated[str | None, typer.Option("--run-id", help="Run id (default: the latest run).")]


def _stored_plan_path(plan: Path, repo: Path) -> str:
    """The plan path `harness ui` can always resolve: relative to `repo` (posix) when the plan
    is inside it, else absolute."""
    resolved = plan.resolve()
    try:
        return resolved.relative_to(repo).as_posix()
    except ValueError:
        return str(resolved)


def _fail(message: str) -> NoReturn:
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


def _fake_progress_steps(order: WorkOrder) -> list[FakeProgressStep]:
    """The `--fake-delay` progress script for one order: Read of files_owned[0], Edit of
    files_owned[-1], then Bash `uv run pytest -q`."""
    return [
        ("Read", {"file_path": order.files_owned[0]}),
        ("Edit", {"file_path": order.files_owned[-1]}),
        ("Bash", {"command": "uv run pytest -q"}),
    ]


def _make_worker(fake: bool, fake_delay: float = 0.0) -> Worker:
    """FakeWorker(default=fake_result("done", summary="fake worker: no changes",
    input_tokens=1000, output_tokens=200)) if fake, else SdkWorker(). With fake and
    fake_delay > 0 the FakeWorker also gets delay_s=fake_delay and the progress steps of
    _fake_progress_steps."""
    if fake:
        return FakeWorker(
            default=fake_result(
                "done", summary="fake worker: no changes", input_tokens=1000, output_tokens=200
            ),
            delay_s=fake_delay,
            progress_steps=_fake_progress_steps if fake_delay > 0 else None,
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
    try:
        return load_state(run_dir_of(repo, rid))
    except StateError as err:
        _fail(str(err))


async def _run_until_stopped(scheduler: Scheduler, resume: bool) -> RunState:
    """scheduler.run(resume), with SIGTERM asking the scheduler to stop (running tasks are
    paused, the run ends as after a cost stop)."""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, scheduler.request_stop)
    except (NotImplementedError, RuntimeError):  # no signal handlers on this platform
        pass
    return await scheduler.run(resume=resume)


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
    fake_delay: Annotated[
        float,
        typer.Option(
            "--fake-delay",
            min=0,
            help="With --fake: seconds each fake attempt takes, with scripted progress.",
        ),
    ] = 0.0,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds per command.")] = (
        DEFAULT_TIMEOUT_S
    ),
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            help="Stop starting new attempts once the estimated cost passes this many USD.",
        ),
    ] = None,
    branch: Annotated[
        str,
        typer.Option("--branch", help="Branch to merge the work into (it must be checked out)."),
    ] = "main",
    repo: RepoOpt = Path("."),
) -> None:
    """Run a plan."""
    if max_cost is not None and max_cost <= 0:
        _fail("--max-cost must be greater than 0")
    if fake_delay > 0 and not fake:
        _fail("--fake-delay requires --fake")
    if review_tier not in TIER_LADDER:
        _fail(f"Unknown tier: {review_tier}")
    p = _load_plan_or_exit(plan)
    repo_path = repo.resolve()
    if resume:
        rid = run_id or latest_run(repo_path)
        if rid is None:
            _fail(f"No run to resume in {runs_root_of(repo_path)}")
    else:
        rid = run_id or new_run_id()
    typer.echo(f"Run {rid}: {len(p.orders)} orders, state in {run_dir_of(repo_path, rid)}")
    scheduler = Scheduler(
        p,
        repo_path,
        rid,
        _make_worker(fake, fake_delay) if fake_delay > 0 else _make_worker(fake),
        plan_path=_stored_plan_path(plan, repo_path),
        max_parallel=max_parallel,
        reviewer=_make_reviewer(review, fake),
        review_tier=cast(Tier, review_tier),
        timeout_s=timeout,
        max_cost_usd=max_cost,
        integration_branch=branch,
    )
    try:
        state = asyncio.run(_run_until_stopped(scheduler, resume))
    except (SchedulerError, StateError) as err:
        _fail(str(err))
    typer.echo("")
    typer.echo(report_text(state))
    paused = next((t for t in state.tasks.values() if t.status == "paused"), None)
    stopped = paused.error if paused is not None else scheduler.stop_reason
    if paused is not None or stopped is not None:
        hint = f"harness run {plan} --repo {repo_path} --resume"
        if branch != "main":
            hint += f" --branch {branch}"
        if review:
            hint += " --review"
        typer.echo(f"Run stopped: {stopped}. Resume with: {hint}")
        raise typer.Exit(code=3)
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


@app.command("ui")
def ui_cmd(
    repo: RepoOpt = Path("."),
    run_id: RunIdOpt = None,
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="Port on 127.0.0.1.")
    ] = 8765,
    read_only: Annotated[
        bool,
        typer.Option("--read-only", help="Only watch runs: no planner, no buttons, GET only."),
    ] = False,
    fake: Annotated[
        bool, typer.Option("--fake", help="Scripted planner and fake workers: no model.")
    ] = False,
    fake_delay: Annotated[
        float | None,
        typer.Option(
            "--fake-delay", min=0, help="With --fake: seconds per scripted step (default 2)."
        ),
    ] = None,
    max_cost: Annotated[
        float | None,
        typer.Option("--max-cost", help="Pass --max-cost USD to the runs started from the page."),
    ] = None,
) -> None:
    """Serve the office page on 127.0.0.1: watch runs, and talk to the planner (unless
    --read-only)."""
    if fake_delay is not None and not fake:
        _fail("--fake-delay requires --fake")
    if max_cost is not None and max_cost <= 0:
        _fail("--max-cost must be greater than 0")
    if read_only and (fake or max_cost is not None):
        _fail("--fake and --max-cost need the interactive page; drop --read-only")
    repo_path = repo.resolve()
    if not repo_path.is_dir():
        _fail(f"Not a directory: {repo_path}")
    office: Office | None = None
    if not read_only:
        if not fake:
            try:
                check_skills()
            except PlannerError as err:
                _fail(str(err))
        try:
            ensure_excluded(repo_path)
        except (WorktreeError, OSError):
            pass
        office = Office(
            repo_path,
            fake=fake,
            fake_delay=2.0 if fake_delay is None else fake_delay,
            max_cost=max_cost,
        )
        try:
            office.open(port)
        except OfficeError as err:
            _fail(str(err))
    try:
        if office is None:
            server = make_server(repo_path, port)
        else:
            server = make_server(repo_path, port, office=office, token=secrets.token_urlsafe(32))
    except OSError as err:
        if office is not None:
            office.close()
        _fail(f"Cannot listen on 127.0.0.1:{port}: {err}")
    url = f"http://127.0.0.1:{port}/"
    if run_id:
        url += f"#{run_id}"
    mode = "read-only" if office is None else "interactive"
    typer.echo(f"Serving {url} ({mode}; Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        still_running = office.close() if office is not None else None
        server.server_close()
    if still_running:
        typer.echo(
            f"Run {still_running} keeps running; check it with: "
            f"harness status --repo {repo_path} --run-id {still_running}"
        )


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
    description: Annotated[
        str | None, typer.Argument(help="Short project description (stage 1 only).")
    ] = None,
    stage: Annotated[
        int,
        typer.Option(
            "--stage",
            min=1,
            help="Stage of the approved project plan to turn into work orders.",
        ),
    ] = 1,
    repo: RepoOpt = Path("."),
) -> None:
    """Plan a project interactively under .harness/plans/: PROJECT_PLAN.md (stage 1), then
    stage-N/plan.json."""
    repo_path = repo.resolve()
    if not repo_path.is_dir():
        _fail(f"Not a directory: {repo_path}")
    if stage == 1 and (description is None or not description.strip()):
        _fail("A project description is required for stage 1.")
    if stage > 1 and description is not None:
        _fail("Pass no description with --stage above 1: the approved project plan is the input.")
    try:
        check_skills()
    except PlannerError as err:
        _fail(str(err))
    try:
        ensure_excluded(repo_path)
    except (WorktreeError, OSError):
        pass
    log = PlannerLog(repo_path, new_session_id())
    session = ObservedSession(
        _make_planner_session(repo_path),
        repo_path,
        log,
        lambda: "project" if stage == 1 else "staging",
    )
    log.emit(
        "planner_started",
        {
            "session_id": log.session_id,
            "source": "terminal",
            "pid": os.getpid(),
            "stage": stage,
            "description": description or "",
            "resumed_from_draft": False,
        },
    )
    try:
        result = asyncio.run(
            run_planner(description or "", repo_path, session, _ask, typer.echo, stage=stage)
        )
    except PlannerError as err:
        log.emit("planner_failed", {"reason": str(err), "cost_usd": session.cost_usd})
        _fail(str(err))
    except KeyboardInterrupt:
        log.emit(
            "planner_failed", {"reason": "interrupted: terminal", "cost_usd": session.cost_usd}
        )
        raise
    log.emit(
        "planner_done",
        {
            "plan_path": _stored_plan_path(result.plan_path, repo_path),
            "cost_usd": result.usage.cost_usd,
            "rounds": result.rounds,
        },
    )
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
