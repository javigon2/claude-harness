"""The Dashboard: cost, time and latency numbers for one run, next to the average of the
other real runs in the repo.

Everything is derived from a run's state.json and events.jsonl. An attempt starts at
`attempt_started` and its worker ends at `worker_finished`; its latency is the time to the
first `worker_progress` of that attempt; verification runs from `verify_started` to
`verify_finished`. A run's `wall_s` runs from `run_started` to `run_finished` (to its last event
while it is still going) and so includes idle gaps; `active_s` is the sum of attempt durations
and is the steadier number to compare. Runs made with fake workers are left out of the
averages: their cost and times mean nothing."""

from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Literal

from pydantic import BaseModel, ConfigDict

from harness.models import Event
from harness.state import RunState, load_state, run_dir
from harness.titles import read_title
from harness.ui.view import _read_events, list_runs

Unit = Literal["usd", "s", "count", "pct", "ratio"]

FAKE_SUMMARY_PREFIX = "fake worker"


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    unit: Unit
    value: float | None
    average: float | None  # over the other real runs; None when there are none (cold start)
    lower_is_better: bool


class TierRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str
    attempts: int
    cost_usd: float
    tokens: int
    avg_attempt_s: float | None
    avg_latency_s: float | None
    avg_verify_s: float | None


class TaskRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str
    tier: str | None
    attempts: int
    cost_usd: float
    tokens: int
    active_s: float | None
    latency_s: float | None
    verify_s: float | None


class Dashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    title: str | None
    finished: bool
    fake: bool
    baseline_runs: int
    metrics: list[Metric]
    tiers: list[TierRow]
    tasks: list[TaskRow]


class _Attempt(BaseModel):
    task_id: str
    attempt: int
    tier: str | None = None
    duration_s: float | None = None
    latency_s: float | None = None
    verify_s: float | None = None


class RunStats(BaseModel):
    """One run reduced to numbers."""

    run_id: str
    finished: bool
    fake: bool
    values: dict[str, float | None]
    attempts: list[_Attempt]


# key, label, unit, lower_is_better
METRICS: list[tuple[str, str, Unit, bool]] = [
    ("cost_usd", "Total cost", "usd", True),
    ("cost_per_task", "Cost per task", "usd", True),
    ("wall_s", "Wall time (start to end)", "s", True),
    ("active_s", "Worker time (sum of attempts)", "s", True),
    ("verify_s", "Verification time", "s", True),
    ("avg_attempt_s", "Average attempt time", "s", True),
    ("avg_latency_s", "Average latency to first step", "s", True),
    ("tasks", "Tasks", "count", False),
    ("attempts", "Attempts", "count", True),
    ("attempts_per_task", "Attempts per task", "ratio", True),
    ("escalations", "Escalations", "count", True),
    ("first_try_pct", "Passed on first attempt", "pct", False),
    ("input_tokens", "Input tokens", "count", True),
    ("output_tokens", "Output tokens", "count", True),
    ("cache_read_tokens", "Cache-read tokens", "count", False),
]


def _secs(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds())


def _avg(xs: list[float]) -> float | None:
    return mean(xs) if xs else None


def _attempts_from(events: list[Event]) -> list[_Attempt]:
    """One record per (task, attempt) with the times its events give."""
    by_key: dict[tuple[str, int], _Attempt] = {}
    started: dict[tuple[str, int], datetime] = {}
    verify_start: dict[tuple[str, int], datetime] = {}
    for e in events:
        if e.task_id is None:
            continue
        n = e.payload.get("attempt")
        if not isinstance(n, int):
            continue
        key = (e.task_id, n)
        rec = by_key.get(key)
        if e.type == "attempt_started":
            by_key[key] = _Attempt(task_id=e.task_id, attempt=n, tier=e.payload.get("tier"))
            started[key] = e.ts
        elif rec is None:
            continue
        elif e.type == "worker_progress" and rec.latency_s is None and key in started:
            rec.latency_s = _secs(started[key], e.ts)
        elif e.type == "worker_finished" and key in started and rec.duration_s is None:
            rec.duration_s = _secs(started[key], e.ts)
        elif e.type == "verify_started":
            verify_start[key] = e.ts
        elif e.type == "verify_finished" and key in verify_start:
            rec.verify_s = _secs(verify_start[key], e.ts)
    return list(by_key.values())


def _is_fake(events: list[Event]) -> bool:
    summaries = [str(e.payload.get("summary", "")) for e in events if e.type == "worker_finished"]
    return bool(summaries) and all(s.startswith(FAKE_SUMMARY_PREFIX) for s in summaries)


def run_stats(repo: Path, run_id: str) -> RunStats:
    """The numbers of one run. Raises what load_state raises for an unreadable run."""
    directory = run_dir(repo, run_id)
    state: RunState = load_state(directory)
    events = _read_events(directory / "events.jsonl")
    attempts = _attempts_from(events)

    starts = [e.ts for e in events if e.type == "run_started"]
    ends = [e.ts for e in events if e.type == "run_finished"]
    wall: float | None = None
    if starts:
        wall = _secs(starts[0], ends[-1] if ends else events[-1].ts)

    tasks = list(state.tasks.values())
    cost = sum(t.usage.cost_usd for t in tasks)
    durations = [a.duration_s for a in attempts if a.duration_s is not None]
    latencies = [a.latency_s for a in attempts if a.latency_s is not None]
    verifies = [a.verify_s for a in attempts if a.verify_s is not None]
    n_tasks = len(tasks)
    n_attempts = sum(t.attempts for t in tasks)
    first_try = sum(1 for t in tasks if t.status == "done" and t.attempts == 1)
    values: dict[str, float | None] = {
        "cost_usd": cost,
        "cost_per_task": cost / n_tasks if n_tasks else None,
        "wall_s": wall,
        "active_s": sum(durations) if durations else None,
        "verify_s": sum(verifies) if verifies else None,
        "avg_attempt_s": _avg(durations),
        "avg_latency_s": _avg(latencies),
        "tasks": float(n_tasks),
        "attempts": float(n_attempts),
        "attempts_per_task": n_attempts / n_tasks if n_tasks else None,
        "escalations": float(sum(1 for e in events if e.type == "escalated")),
        "first_try_pct": 100.0 * first_try / n_tasks if n_tasks else None,
        "input_tokens": float(sum(t.usage.input_tokens for t in tasks)),
        "output_tokens": float(sum(t.usage.output_tokens for t in tasks)),
        "cache_read_tokens": float(sum(t.usage.cache_read_tokens for t in tasks)),
    }
    return RunStats(
        run_id=run_id,
        finished=bool(state.finished),
        fake=_is_fake(events),
        values=values,
        attempts=attempts,
    )


def _baseline(repo: Path, run_id: str) -> tuple[dict[str, float | None], int]:
    """The average of every metric over the other finished, non-fake runs, and how many
    runs that is. A run that cannot be read is skipped."""
    others: list[RunStats] = []
    for other in list_runs(repo):
        if other == run_id:
            continue
        try:
            stats = run_stats(repo, other)
        except Exception:
            continue
        if stats.finished and not stats.fake:
            others.append(stats)
    averages: dict[str, float | None] = {}
    for key, _label, _unit, _lower in METRICS:
        xs = [s.values[key] for s in others if s.values.get(key) is not None]
        averages[key] = _avg([x for x in xs if x is not None])
    return averages, len(others)


def build_dashboard(repo: Path, run_id: str) -> Dashboard:
    """The Dashboard of `run_id`. Raises FileNotFoundError for an unknown run."""
    if run_id not in list_runs(repo):
        raise FileNotFoundError(run_id)
    repo = Path(repo)
    stats = run_stats(repo, run_id)
    averages, count = _baseline(repo, run_id)
    metrics = [
        Metric(
            key=key,
            label=label,
            unit=unit,
            value=stats.values.get(key),
            average=averages.get(key),
            lower_is_better=lower,
        )
        for key, label, unit, lower in METRICS
    ]

    state = load_state(run_dir(repo, run_id))
    by_task: dict[str, list[_Attempt]] = {}
    for a in stats.attempts:
        by_task.setdefault(a.task_id, []).append(a)

    def _sum(xs: list[float | None]) -> float | None:
        got = [x for x in xs if x is not None]
        return sum(got) if got else None

    tasks = [
        TaskRow(
            task_id=t.task_id,
            status=t.status,
            tier=t.tier_used,
            attempts=t.attempts,
            cost_usd=t.usage.cost_usd,
            tokens=t.usage.input_tokens + t.usage.output_tokens,
            active_s=_sum([a.duration_s for a in by_task.get(t.task_id, [])]),
            latency_s=_avg(
                [a.latency_s for a in by_task.get(t.task_id, []) if a.latency_s is not None]
            ),
            verify_s=_sum([a.verify_s for a in by_task.get(t.task_id, [])]),
        )
        for t in state.tasks.values()
    ]

    tier_rows: list[TierRow] = []
    for tier in ("haiku", "sonnet", "opus"):
        mine = [a for a in stats.attempts if a.tier == tier]
        if not mine:
            continue
        cost = 0.0
        tokens = 0
        for t in state.tasks.values():
            for rec in t.history:
                if rec.tier == tier:
                    cost += rec.usage.cost_usd + rec.review_usage.cost_usd
                    tokens += rec.usage.input_tokens + rec.usage.output_tokens
        tier_rows.append(
            TierRow(
                tier=tier,
                attempts=len(mine),
                cost_usd=cost,
                tokens=tokens,
                avg_attempt_s=_avg([a.duration_s for a in mine if a.duration_s is not None]),
                avg_latency_s=_avg([a.latency_s for a in mine if a.latency_s is not None]),
                avg_verify_s=_avg([a.verify_s for a in mine if a.verify_s is not None]),
            )
        )

    return Dashboard(
        run_id=run_id,
        title=read_title(repo, run_id),
        finished=stats.finished,
        fake=stats.fake,
        baseline_runs=count,
        metrics=metrics,
        tiers=tier_rows,
        tasks=tasks,
    )


class ProjectRow(BaseModel):
    """One project (a repo folder) in the Overview. Fake runs are left out of every figure."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    runs: int
    run_cost_usd: float  # workers and reviews
    planning_cost_usd: float  # planner, Orchestrator and agent chats
    cost_usd: float  # run_cost_usd + planning_cost_usd
    wall_s: float  # sum of the runs' start-to-end times
    active_s: float  # sum of the runs' worker time


class Overview(BaseModel):
    """What the harness has spent across projects: the totals and one row per project."""

    model_config = ConfigDict(extra="forbid")

    total_cost_usd: float
    run_cost_usd: float
    planning_cost_usd: float
    total_wall_s: float
    total_active_s: float
    total_runs: int
    projects: list[ProjectRow]


def _cost_of(payload: dict) -> float:
    v = payload.get("cost_usd")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return 0.0
    return float(v)


def planning_cost(repo: Path) -> float:
    """What planning and chatting cost in `repo`: each planner session's last (running)
    cost, the Orchestrator's turns and the agent chats' replies, read from the planner and
    chat logs. 0.0 when there are no logs."""
    planner = _read_events(Path(repo) / ".harness" / "planner" / "events.jsonl")
    chats = _read_events(Path(repo) / ".harness" / "chats" / "events.jsonl")
    sessions: dict[str, float] = {}
    total = 0.0
    for e in planner:
        if e.run_id.startswith("planner-"):
            sessions[e.run_id] = max(sessions.get(e.run_id, 0.0), _cost_of(e.payload))
        elif e.run_id.startswith("orch-") and e.type in (
            "orchestrator_turn",
            "orchestrator_failed",
        ):
            total += _cost_of(e.payload)
    total += sum(sessions.values())
    total += sum(_cost_of(e.payload) for e in chats if e.type == "chat_reply")
    return total


def project_row(repo: Path) -> ProjectRow:
    """The Overview row of one project folder. A run that cannot be read is skipped."""
    repo = Path(repo)
    runs = run_cost = wall = active = 0.0
    for run_id in list_runs(repo):
        try:
            stats = run_stats(repo, run_id)
        except Exception:
            continue
        if stats.fake:
            continue
        runs += 1
        run_cost += stats.values.get("cost_usd") or 0.0
        wall += stats.values.get("wall_s") or 0.0
        active += stats.values.get("active_s") or 0.0
    planning = planning_cost(repo)
    return ProjectRow(
        name=repo.name or str(repo),
        path=str(repo),
        runs=int(runs),
        run_cost_usd=run_cost,
        planning_cost_usd=planning,
        cost_usd=run_cost + planning,
        wall_s=wall,
        active_s=active,
    )


def build_overview(repos: list[Path]) -> Overview:
    """The Overview over `repos` (duplicates and missing folders dropped), costliest first."""
    seen: set[Path] = set()
    rows: list[ProjectRow] = []
    for repo in repos:
        repo = Path(repo).resolve()
        if repo in seen or not repo.is_dir():
            continue
        seen.add(repo)
        rows.append(project_row(repo))
    rows.sort(key=lambda r: r.cost_usd, reverse=True)
    return Overview(
        total_cost_usd=sum(r.cost_usd for r in rows),
        run_cost_usd=sum(r.run_cost_usd for r in rows),
        planning_cost_usd=sum(r.planning_cost_usd for r in rows),
        total_wall_s=sum(r.wall_s for r in rows),
        total_active_s=sum(r.active_s for r in rows),
        total_runs=sum(r.runs for r in rows),
        projects=rows,
    )
