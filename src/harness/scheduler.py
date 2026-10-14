"""Scheduler: run a plan's waves in order, tasks in a wave in parallel, each in its own
worktree through the router, merging the ones that pass."""

import asyncio
import subprocess
from pathlib import Path

from harness.config import Tier
from harness.events import EventLog
from harness.models import Plan, TaskState, WorkOrder
from harness.plan import compute_waves
from harness.review import Reviewer
from harness.router import Router
from harness.state import STATE_FILE, RunState, load_state, run_dir, save_state
from harness.verify import DEFAULT_TIMEOUT_S
from harness.worker import Worker
from harness.worktree import WorktreeError, WorktreeManager


class SchedulerError(RuntimeError):
    """The run cannot start. The message says why."""


def _git_out(cwd: Path, *args: str) -> str:
    """stdout of `git <args>` in cwd, stripped. Raises WorktreeError(stderr.strip()) if git
    exits non-zero."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as err:
        raise WorktreeError(str(err)) from err
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip())
    return proc.stdout.strip()


class Scheduler:
    def __init__(
        self,
        plan: Plan,
        repo: Path,
        run_id: str,
        worker: Worker,
        *,
        plan_path: str = "",
        max_parallel: int = 4,
        reviewer: Reviewer | None = None,
        review_tier: Tier = "sonnet",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        integration_branch: str = "main",
    ) -> None:
        """Store the arguments on self under the same names (repo as Path(repo).resolve()).
        Also: self.run_dir = run_dir(self.repo, run_id); self.events = EventLog(self.run_dir,
        run_id); self.worktrees = WorktreeManager(self.repo, run_id, integration_branch);
        self.state: RunState | None = None. Raises ValueError if max_parallel < 1."""
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.plan = plan
        self.repo = Path(repo).resolve()
        self.run_id = run_id
        self.worker = worker
        self.plan_path = plan_path
        self.max_parallel = max_parallel
        self.reviewer = reviewer
        self.review_tier = review_tier
        self.timeout_s = timeout_s
        self.integration_branch = integration_branch
        self.run_dir = run_dir(self.repo, run_id)
        self.events = EventLog(self.run_dir, run_id)
        self.worktrees = WorktreeManager(self.repo, run_id, integration_branch)
        self.state: RunState | None = None
        self._stop_reason: str | None = None

    async def _emit(self, type_: str, payload: dict, task_id: str | None = None) -> None:
        await self.events.emit(type_, task_id=task_id, payload=payload)

    async def run(self, resume: bool = False) -> RunState:
        """Run the whole plan and return the final RunState (also saved to state.json).
        Algorithm in the `decision: scheduler algorithm` context item. Raises SchedulerError
        (before any task starts) and StateError (resume without a readable state.json);
        task-level failures never raise, they end in the task's state."""
        try:
            branch = await asyncio.to_thread(
                _git_out, self.repo, "rev-parse", "--abbrev-ref", "HEAD"
            )
        except WorktreeError as err:
            raise SchedulerError(
                f"{self.repo} is not a git repository with a commit: {err}"
            ) from err
        if branch != self.integration_branch:
            raise SchedulerError(
                f"{self.repo} is on branch {branch}, not {self.integration_branch}; "
                f"check out {self.integration_branch} first"
            )

        waves = compute_waves(self.plan)
        by_id = {o.id: o for o in self.plan.orders}

        if resume:
            old = load_state(self.run_dir)
            tasks: dict[str, TaskState] = {}
            for o in self.plan.orders:
                prev = old.tasks.get(o.id)
                if prev is not None and prev.status == "done":
                    tasks[o.id] = prev
                else:
                    tasks[o.id] = TaskState(task_id=o.id)
            self.state = old.model_copy(
                update={
                    "tasks": tasks,
                    "finished": False,
                    "stage": self.plan.stage,
                    "plan_path": self.plan_path or old.plan_path,
                }
            )
            for tid, ts in tasks.items():
                if ts.status != "done":
                    await asyncio.to_thread(self.worktrees.discard, tid)
        else:
            if (self.run_dir / STATE_FILE).exists():
                raise SchedulerError(
                    f"run {self.run_id} already exists in {self.run_dir}; "
                    "use --resume or another --run-id"
                )
            self.state = RunState(
                run_id=self.run_id,
                plan_path=self.plan_path,
                stage=self.plan.stage,
                tasks={o.id: TaskState(task_id=o.id) for o in self.plan.orders},
            )

        state = self.state
        await self._save()
        await self._emit(
            "run_started",
            {
                "plan_path": state.plan_path,
                "stage": self.plan.stage,
                "orders": len(self.plan.orders),
                "waves": waves,
                "max_parallel": self.max_parallel,
                "resume": resume,
                "review": self.reviewer is not None,
            },
        )

        sem = asyncio.Semaphore(self.max_parallel)
        lock = asyncio.Lock()

        for i, wave in enumerate(waves, start=1):
            await self._emit("wave_started", {"wave": i, "tasks": wave})
            runnable: list[WorkOrder] = []
            for tid in wave:
                ts = state.tasks[tid]
                if ts.status == "done":
                    await self._emit(
                        "task_skipped", {"reason": "done in an earlier run"}, task_id=tid
                    )
                    continue
                bad = next(
                    (d for d in by_id[tid].depends_on if state.tasks[d].status != "done"),
                    None,
                )
                if bad is not None:
                    ts.status = "blocked"
                    ts.error = f"dependency {bad} is {state.tasks[bad].status}"
                    await self._save()
                    await self._emit(
                        "task_blocked", {"reason": ts.error, "dependency": bad}, task_id=tid
                    )
                    continue
                runnable.append(by_id[tid])
            await asyncio.gather(*(self._run_task(o, sem, lock) for o in runnable))
            await self._emit(
                "wave_finished",
                {"wave": i, "statuses": {tid: state.tasks[tid].status for tid in wave}},
            )
            if self._stop_reason is not None:
                await self._emit("run_stopping", {"reason": self._stop_reason})
                break

        state.finished = True
        await self._save()
        statuses = [t.status for t in state.tasks.values()]
        await self._emit(
            "run_finished",
            {
                "done": statuses.count("done"),
                "failed": statuses.count("failed"),
                "blocked": statuses.count("blocked"),
                "paused": statuses.count("paused"),
                "stopped_reason": self._stop_reason,
                "cost_usd": sum(t.usage.cost_usd for t in state.tasks.values()),
            },
        )
        return state

    async def _save(self, _task: TaskState | None = None) -> None:
        """save_state(self.run_dir, self.state). Used as the router's on_state callback, so
        state.json is rewritten after every transition."""
        assert self.state is not None
        save_state(self.run_dir, self.state)

    async def _run_task(self, order: WorkOrder, sem: asyncio.Semaphore, lock: asyncio.Lock) -> None:
        """One task: worktree, router, commit and merge. Algorithm in the
        `decision: task algorithm` context item. Never raises except CancelledError."""
        assert self.state is not None
        async with sem:
            if self._stop_reason is not None:
                return
            ts = self.state.tasks[order.id]
            branch = self.worktrees.branch(order.id)

            async def fail(reason: str, attempts: int, tier: Tier | None) -> None:
                ts.status = "failed"
                ts.error = reason
                await self._save()
                await self._emit(
                    "task_failed",
                    {"attempts": attempts, "tier": tier, "reason": reason},
                    task_id=order.id,
                )

            try:
                async with lock:
                    path = await asyncio.to_thread(self.worktrees.create, order.id)
                    base = await asyncio.to_thread(_git_out, path, "rev-parse", "HEAD")
            except (WorktreeError, OSError) as err:
                await fail(f"worktree create failed: {err}", 0, None)
                return

            await self._emit(
                "task_started",
                {"worktree": str(path), "branch": branch, "base": base, "tier": order.tier},
                task_id=order.id,
            )

            router = Router(
                self.worker,
                self.events,
                reviewer=self.reviewer,
                review_tier=self.review_tier,
                timeout_s=self.timeout_s,
                on_state=self._save,
                should_stop=lambda: self._stop_reason,
            )
            await router.route(order, path, ts, base=base)
            if ts.status == "paused" and self._stop_reason is None:
                self._stop_reason = ts.error
            if ts.status != "done":
                return

            try:
                async with lock:
                    committed = await asyncio.to_thread(
                        self.worktrees.commit, order.id, f"harness: {order.id}: {order.title}"
                    )
                    result = await asyncio.to_thread(self.worktrees.merge, order.id)
            except (WorktreeError, OSError) as err:
                await fail(f"merge failed: {err}", ts.attempts, ts.tier_used)
                return

            if result == "conflict":
                reason = f"merge conflict with {self.integration_branch}; worktree kept at {path}"
                ts.status = "failed"
                ts.error = reason
                await self._save()
                await self._emit(
                    "merge_conflict", {"branch": branch, "worktree": str(path)}, task_id=order.id
                )
                await self._emit(
                    "task_failed",
                    {"attempts": ts.attempts, "tier": ts.tier_used, "reason": reason},
                    task_id=order.id,
                )
                return

            await self._emit("merged", {"branch": branch, "committed": committed}, task_id=order.id)
            async with lock:
                try:
                    await asyncio.to_thread(self.worktrees.discard, order.id)
                except OSError:
                    pass
