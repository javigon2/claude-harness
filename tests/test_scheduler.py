import asyncio
import subprocess
from pathlib import Path

import pytest

from harness.config import Tier
from harness.events import read_events
from harness.models import Budget, Plan, WorkOrder
from harness.scheduler import Scheduler, SchedulerError
from harness.state import StateError, load_state
from harness.worker import FakeWorker, WorkerResult, fake_result


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("line one\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _order(
    oid: str, deps: tuple[str, ...] = (), tier: Tier = "haiku", attempts: int = 1
) -> WorkOrder:
    acceptance = f"test -f {oid}.txt" + "".join(f" && test -f {d}.txt" for d in deps)
    return WorkOrder(
        id=oid,
        title=f"task {oid}",
        goal=f"write {oid}.txt",
        tier=tier,
        depends_on=list(deps),
        files_owned=[f"{oid}.txt"],
        files_readonly=[],
        context=[],
        steps=[],
        constraints=[],
        acceptance=[acceptance],
        budget=Budget(max_turns=5, max_attempts=attempts),
        out_of_scope=[],
    )


def _dag() -> Plan:
    return Plan(
        stage="test",
        orders=[
            _order("a"),
            _order("b"),
            _order("c"),
            _order("d", ("a",)),
            _order("e", ("c",)),
            _order("f", ("d", "e")),
        ],
    )


def _write(order: WorkOrder, tier: Tier, cwd: Path, feedback: str | None) -> WorkerResult:
    (cwd / f"{order.id}.txt").write_text(f"{order.id}\n")
    return fake_result(summary="wrote it", cost_usd=0.5)


def _noop(order: WorkOrder, tier: Tier, cwd: Path, feedback: str | None) -> WorkerResult:
    return fake_result(status="failed")


def _types(run_dir: Path) -> list[tuple[str, str | None]]:
    return [(e.type, e.task_id) for e in read_events(run_dir / "events.jsonl")]


async def test_dag_with_failing_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worker = FakeWorker(script={"c": [_noop]}, default=_write)
    sched = Scheduler(_dag(), repo, "r1", worker, plan_path="plan.json")
    state = await sched.run()

    st = state.tasks
    assert [st[t].status for t in "abd"] == ["done"] * 3
    assert st["c"].status == "failed"
    assert st["e"].status == "blocked"
    assert st["e"].error == "dependency c is failed"
    assert st["f"].status == "blocked"
    assert st["f"].error == "dependency e is blocked"
    assert state.finished is True

    called = [c.order_id for c in worker.calls]
    assert "e" not in called and "f" not in called
    assert max(called.index(x) for x in "abc") < called.index("d")

    for t in "abd":
        assert _out(repo, "show", f"main:{t}.txt") == f"{t}\n"
    # d's acceptance required a.txt in its worktree
    assert st["d"].history[-1].passed

    assert load_state(sched.run_dir) == state

    types = _types(sched.run_dir)
    assert types[0] == ("run_started", None)
    assert [t for t, _ in types].count("wave_started") == 3
    assert {tid for t, tid in types if t == "merged"} == {"a", "b", "d"}
    assert {tid for t, tid in types if t == "task_blocked"} == {"e", "f"}
    events = read_events(sched.run_dir / "events.jsonl")
    finished = [e for e in events if e.type == "run_finished"]
    assert len(finished) == 1
    assert finished[0].payload["done"] == 3
    assert finished[0].payload["failed"] == 1
    assert finished[0].payload["blocked"] == 2
    assert finished[0].payload["cost_usd"] == pytest.approx(1.5)
    started = events[0].payload
    assert started["waves"] == [["a", "b", "c"], ["d", "e"], ["f"]]
    assert started["plan_path"] == "plan.json"
    assert started["resume"] is False and started["review"] is False

    # merged worktrees removed, failed one kept
    assert not sched.worktrees.path("a").exists()
    assert sched.worktrees.path("c").exists()


def _concurrency_worker(cap: int) -> tuple[FakeWorker, dict[str, int]]:
    counts = {"now": 0, "max": 0}

    async def step(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        counts["now"] += 1
        counts["max"] = max(counts["max"], counts["now"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while counts["max"] < cap and loop.time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        counts["now"] -= 1
        return _write(order, tier, cwd, fb)

    return FakeWorker(default=step), counts


@pytest.mark.parametrize("cap", [2, 4])
async def test_parallelism_bounded(tmp_path: Path, cap: int) -> None:
    repo = _repo(tmp_path)
    plan = Plan(stage="p", orders=[_order(x) for x in "wxyz"])
    worker, counts = _concurrency_worker(cap)
    state = await Scheduler(plan, repo, "r", worker, max_parallel=cap).run()
    assert all(t.status == "done" for t in state.tasks.values())
    assert counts["max"] == cap


async def test_state_saved_during_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    seen: list[str] = []
    sched: Scheduler

    def step(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        seen.append(load_state(sched.run_dir).tasks[order.id].status)
        return _write(order, tier, cwd, fb)

    sched = Scheduler(Plan(stage="s", orders=[_order("a")]), repo, "r", FakeWorker(default=step))
    await sched.run()
    assert seen == ["running"]


async def test_resume(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def fail_c(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        (cwd / "scratch.txt").write_text("leftover\n")
        return fake_result(status="failed")

    first = Scheduler(_dag(), repo, "r1", FakeWorker(script={"c": [fail_c]}, default=_write))
    await first.run()
    old_c = first.worktrees.path("c")
    assert (old_c / "scratch.txt").exists()

    fresh: list[bool] = []

    def pass_c(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        fresh.append(cwd == old_c and not (cwd / "scratch.txt").exists())
        return _write(order, tier, cwd, fb)

    worker = FakeWorker(script={"c": [pass_c]}, default=_write)
    second = Scheduler(_dag(), repo, "r1", worker)
    state = await second.run(resume=True)

    assert sorted(c.order_id for c in worker.calls) == ["c", "e", "f"]
    assert all(t.status == "done" for t in state.tasks.values())
    assert fresh == [True]
    skipped = {tid for t, tid in _types(second.run_dir) if t == "task_skipped"}
    assert skipped == {"a", "b", "d"}
    for t in "abcdef":
        assert _out(repo, "show", f"main:{t}.txt") == f"{t}\n"
    assert load_state(second.run_dir) == state


async def test_wrong_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "other"], cwd=repo, check=True, capture_output=True)
    sched = Scheduler(_dag(), repo, "r", FakeWorker(default=_write))
    with pytest.raises(SchedulerError, match="on branch other, not main"):
        await sched.run()


async def test_not_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    sched = Scheduler(_dag(), plain, "r", FakeWorker(default=_write))
    with pytest.raises(SchedulerError, match="not a git repository"):
        await sched.run()


async def test_run_exists(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = Plan(stage="s", orders=[_order("a")])
    await Scheduler(plan, repo, "r", FakeWorker(default=_write)).run()
    worker = FakeWorker(default=_write)
    with pytest.raises(SchedulerError, match="already exists"):
        await Scheduler(plan, repo, "r", worker).run()
    assert worker.calls == []


async def test_resume_without_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sched = Scheduler(_dag(), repo, "nope", FakeWorker(default=_write))
    with pytest.raises(StateError):
        await sched.run(resume=True)


async def test_merge_conflict(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def step(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        (repo / "a.txt").write_text("main version\n")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "m"], cwd=repo, check=True, capture_output=True)
        return _write(order, tier, cwd, fb)

    sched = Scheduler(Plan(stage="s", orders=[_order("a")]), repo, "r", FakeWorker(default=step))
    state = await sched.run()
    ts = state.tasks["a"]
    assert ts.status == "failed"
    assert ts.error is not None and ts.error.startswith("merge conflict with main")
    assert sched.worktrees.path("a").exists()
    types = [t for t, _ in _types(sched.run_dir)]
    assert "merge_conflict" in types
    assert types.index("merge_conflict") < len(types) - 1 - types[::-1].index("task_failed")


async def test_no_change_task_done(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    order = _order("a").model_copy(update={"acceptance": ["true"]})
    sched = Scheduler(Plan(stage="s", orders=[order]), repo, "r", FakeWorker(default=_noop))
    state = await sched.run()
    assert state.tasks["a"].status == "done"
    merged = [e for e in read_events(sched.run_dir / "events.jsonl") if e.type == "merged"]
    assert merged[0].payload == {"branch": "harness/r/a", "committed": False}
    assert not sched.worktrees.path("a").exists()


def test_max_parallel_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Scheduler(_dag(), tmp_path, "r", FakeWorker(), max_parallel=0)


async def test_rate_limit_stops_later_waves_and_resume_finishes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def limited(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        return WorkerResult(
            status="error",
            error="rate limited",
            rate_limited=True,
            rate_limit_resets_at=1760000000,
        )

    worker = FakeWorker(script={"c": [limited]}, default=_write)
    sched = Scheduler(_dag(), repo, "r1", worker, max_parallel=1)
    state = await sched.run()

    st = state.tasks
    assert [st[t].status for t in "ab"] == ["done", "done"]
    assert st["c"].status == "paused"
    assert st["c"].attempts == 0
    assert [st[t].status for t in "def"] == ["pending"] * 3
    assert sorted(c.order_id for c in worker.calls) == ["a", "b", "c"]
    events = list(read_events(sched.run_dir / "events.jsonl"))
    types = [e.type for e in events]
    assert types.count("run_stopping") == 1
    assert types.index("run_stopping") > types.index("wave_finished")
    assert "wave_started" in types and types.count("wave_started") == 1
    finished = events[-1]
    assert finished.type == "run_finished"
    assert finished.payload["paused"] == 1
    assert finished.payload["stopped_reason"] == st["c"].error
    assert sched.worktrees.path("c").exists()

    worker2 = FakeWorker(default=_write)
    second = Scheduler(_dag(), repo, "r1", worker2, max_parallel=1)
    state2 = await second.run(resume=True)
    assert all(t.status == "done" for t in state2.tasks.values())
    assert sorted(c.order_id for c in worker2.calls) == ["c", "d", "e", "f"]
    assert state2.tasks["c"].attempts == 1


def _flat(*oids: str, attempts: int = 1) -> Plan:
    return Plan(stage="test", orders=[_order(o, attempts=attempts) for o in oids])


def _costly(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
    (cwd / f"{order.id}.txt").write_text(f"{order.id}\n")
    return fake_result(cost_usd=0.4)


def test_max_cost_must_be_positive(tmp_path: Path) -> None:
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            Scheduler(_flat("a"), tmp_path, "r", FakeWorker(), max_cost_usd=bad)


async def test_cost_cap_stops_before_fourth_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worker = FakeWorker(default=_costly)
    sched = Scheduler(
        _flat("a", "b", "c", "d", "e"), repo, "r1", worker, max_parallel=1, max_cost_usd=1.0
    )
    state = await sched.run()

    assert [c.order_id for c in worker.calls] == ["a", "b", "c"]
    assert [state.tasks[t].status for t in "abc"] == ["done"] * 3
    assert [state.tasks[t].status for t in "de"] == ["pending"] * 2
    events = list(read_events(sched.run_dir / "events.jsonl"))
    types = [e.type for e in events]
    assert types.count("budget_exceeded") == 1
    budget = next(e for e in events if e.type == "budget_exceeded")
    assert budget.payload["max_cost_usd"] == 1.0
    assert budget.payload["cost_usd"] == pytest.approx(1.2)
    assert types.count("run_stopping") == 1
    reason = "estimated cost $1.20 passed the $1.00 cap"
    assert events[-1].payload["stopped_reason"] == reason
    assert events[-1].payload["paused"] == 0


async def test_cost_cap_pauses_task_between_attempts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def failing(order: WorkOrder, tier: Tier, cwd: Path, fb: str | None) -> WorkerResult:
        return fake_result(status="failed", cost_usd=0.4)

    worker = FakeWorker(default=failing)
    sched = Scheduler(_flat("a", "b", attempts=5), repo, "r1", worker, max_parallel=1)
    sched.max_cost_usd = 1.0
    state = await sched.run()

    assert len(worker.calls) == 3
    assert state.tasks["a"].status == "paused"
    assert state.tasks["a"].attempts == 3
    assert state.tasks["a"].error == "estimated cost $1.20 passed the $1.00 cap"
    assert state.tasks["b"].status == "pending"
    types = [e.type for e in read_events(sched.run_dir / "events.jsonl")]
    assert types.count("budget_exceeded") == 1
    assert types.count("task_paused") == 1

    worker2 = FakeWorker(default=_write)
    second = Scheduler(_flat("a", "b", attempts=5), repo, "r1", worker2, max_parallel=1)
    state2 = await second.run(resume=True)
    assert all(t.status == "done" for t in state2.tasks.values())


async def test_no_cost_cap_never_stops(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worker = FakeWorker(default=_costly)
    sched = Scheduler(_flat("a", "b", "c", "d", "e"), repo, "r1", worker, max_parallel=1)
    state = await sched.run()
    assert all(t.status == "done" for t in state.tasks.values())
    types = [e.type for e in read_events(sched.run_dir / "events.jsonl")]
    assert "budget_exceeded" not in types and "run_stopping" not in types
