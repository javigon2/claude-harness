"""Scheduler: start each task as soon as its dependencies are done (at most max_parallel at
once), each in its own worktree through the router, merging the ones that pass."""

import asyncio
import os
import subprocess
import time
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

STOPPED_BY_USER = "stopped by user"


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
        max_cost_usd: float | None = None,
    ) -> None:
        """Store the arguments on self under the same names (repo as Path(repo).resolve()).
        Also: self.run_dir = run_dir(self.repo, run_id); self.events = EventLog(self.run_dir,
        run_id); self.worktrees = WorktreeManager(self.repo, run_id, integration_branch);
        self.state: RunState | None = None. Raises ValueError if max_parallel < 1 or if
        max_cost_usd is not None and <= 0."""
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        if max_cost_usd is not None and max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be greater than 0")
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
        self.max_cost_usd = max_cost_usd
        self.run_dir = run_dir(self.repo, run_id)
        self.events = EventLog(self.run_dir, run_id)
        self.worktrees = WorktreeManager(self.repo, run_id, integration_branch)
        self.state: RunState | None = None
        self._stop_reason: str | None = None
        self._budget_event: dict | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stop_requested: str | None = None

    async def _emit(self, type_: str, payload: dict, task_id: str | None = None) -> None:
        await self.events.emit(type_, task_id=task_id, payload=payload)

    @property
    def stop_reason(self) -> str | None:
        """Why the run stopped starting work (a rate limit, a cost cap), or None."""
        return self._stop_reason

    def request_stop(self, reason: str = STOPPED_BY_USER) -> None:
        """Set stop_reason if unset; cancel running tasks. A task cancelled this way becomes
        `paused` with `reason` as its error; tasks that already passed verification (being
        committed and merged) are left to finish. Call it from the event loop's thread."""
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stop_requested = reason
        for task in list(self._running.values()):
            task.cancel()

    def _should_stop(self) -> str | None:
        """Why no new attempt or task may start, or None. The first time the summed cost of
        all tasks (done tasks of an earlier run included) is above max_cost_usd it sets
        self._stop_reason and queues the `budget_exceeded` event for _flush_budget_event.
        Attempts already running finish, so the final cost can pass the cap by what was in
        flight."""
        if self._stop_reason is not None:
            return self._stop_reason
        if self.max_cost_usd is None or self.state is None:
            return None
        total = sum(t.usage.cost_usd for t in self.state.tasks.values())
        if total > self.max_cost_usd:
            self._stop_reason = (
                f"estimated cost ${total:.2f} passed the ${self.max_cost_usd:.2f} cap"
            )
            self._budget_event = {"cost_usd": total, "max_cost_usd": self.max_cost_usd}
        return self._stop_reason

    async def _flush_budget_event(self) -> None:
        """Emit the queued `budget_exceeded` event, once."""
        if self._budget_event is not None:
            payload, self._budget_event = self._budget_event, None
            await self._emit("budget_exceeded", payload)

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
                    "pid": os.getpid(),
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
                pid=os.getpid(),
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
        run_start = time.monotonic()

        # Longest chain of orders that wait on each order: the critical path goes first.
        height: dict[str, int] = {}

        def chain(tid: str) -> int:
            if tid not in height:
                below = [chain(o.id) for o in self.plan.orders if tid in o.depends_on]
                height[tid] = 1 + max(below, default=0)
            return height[tid]

        position = {o.id: n for n, o in enumerate(self.plan.orders)}
        queue = sorted(by_id, key=lambda tid: (-chain(tid), position[tid]))
        settled: set[str] = set()
        running: dict[str, asyncio.Task] = {}
        while True:
            for tid in queue:
                if tid in settled or tid in running:
                    continue
                ts = state.tasks[tid]
                if ts.status == "done":
                    settled.add(tid)
                    await self._emit(
                        "task_skipped", {"reason": "done in an earlier run"}, task_id=tid
                    )
                    continue
                deps = by_id[tid].depends_on
                if self._stop_reason is not None or not all(d in settled for d in deps):
                    continue
                bad = next((d for d in deps if state.tasks[d].status != "done"), None)
                if bad is not None:
                    settled.add(tid)
                    ts.status = "blocked"
                    ts.error = f"dependency {bad} is {state.tasks[bad].status}"
                    await self._save()
                    await self._emit(
                        "task_blocked", {"reason": ts.error, "dependency": bad}, task_id=tid
                    )
                    continue
                running[tid] = asyncio.create_task(self._run_task(by_id[tid], sem, lock))
            if not running:
                break
            finished, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for tid in [t for t, task in running.items() if task in finished]:
                settled.add(tid)
                running.pop(tid).result()
        if self._stop_reason is not None:
            await self._emit("run_stopping", {"reason": self._stop_reason})
        run_ms = round((time.monotonic() - run_start) * 1000)

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
                "duration_ms": run_ms,
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
        """One task, cancellable by request_stop until it has passed verification: a task
        cancelled that way becomes `paused` (event `task_paused`) and the cancellation is
        swallowed; any other cancellation propagates."""
        assert self.state is not None
        current = asyncio.current_task()
        assert current is not None
        self._running[order.id] = current
        try:
            await self._run_task_body(order, sem, lock)
        except asyncio.CancelledError:
            reason = self._stop_requested
            if reason is None:
                raise
            ts = self.state.tasks[order.id]
            ts.status = "paused"
            ts.error = reason
            await self._save()
            await self._emit(
                "task_paused",
                {"attempt": ts.attempts, "tier": ts.tier_used, "reason": reason, "resets_at": None},
                task_id=order.id,
            )
        finally:
            self._running.pop(order.id, None)

    async def _run_task_body(
        self, order: WorkOrder, sem: asyncio.Semaphore, lock: asyncio.Lock
    ) -> None:
        """One task: worktree, router, commit and merge. Algorithm in the
        `decision: task algorithm` context item. Never raises except CancelledError."""
        assert self.state is not None
        queued_at = time.monotonic()
        async with sem:
            queue_wait_ms = round((time.monotonic() - queued_at) * 1000)
            if self._should_stop() is not None:
                await self._flush_budget_event()
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

            worktree_start = time.monotonic()
            try:
                async with lock:
                    path = await asyncio.to_thread(self.worktrees.create, order.id)
                    base = await asyncio.to_thread(_git_out, path, "rev-parse", "HEAD")
            except (WorktreeError, OSError) as err:
                await fail(f"worktree create failed: {err}", 0, None)
                return

            await self._emit(
                "task_started",
                {
                    "worktree": str(path),
                    "branch": branch,
                    "base": base,
                    "tier": order.tier,
                    "queue_wait_ms": queue_wait_ms,
                    "worktree_ms": round((time.monotonic() - worktree_start) * 1000),
                },
                task_id=order.id,
            )

            router = Router(
                self.worker,
                self.events,
                reviewer=self.reviewer,
                review_tier=self.review_tier,
                timeout_s=self.timeout_s,
                on_state=self._save,
                should_stop=self._should_stop,
            )
            await router.route(order, path, ts, base=base)
            self._running.pop(order.id, None)  # verified: a stop no longer cancels the merge
            await self._flush_budget_event()
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
