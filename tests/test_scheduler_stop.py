import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from git_helpers import init_repo

from harness.events import read_events
from harness.models import Budget, Plan, WorkOrder
from harness.scheduler import STOPPED_BY_USER, Scheduler
from harness.state import load_state
from harness.worker import FakeWorker, fake_result


def _order(oid: str, deps: tuple[str, ...] = ()) -> WorkOrder:
    return WorkOrder(
        id=oid,
        title=f"task {oid}",
        goal=f"write {oid}.txt",
        tier="haiku",
        depends_on=list(deps),
        files_owned=[f"{oid}.txt"],
        files_readonly=[],
        context=[],
        steps=[],
        constraints=[],
        acceptance=[f"test -f {oid}.txt"],
        budget=Budget(max_turns=5, max_attempts=1),
        out_of_scope=[],
    )


def _plan() -> Plan:
    return Plan(stage="stop", orders=[_order("a"), _order("b", ("a",))])


def _write(order: WorkOrder, tier, cwd: Path, feedback):
    (cwd / f"{order.id}.txt").write_text(f"{order.id}\n")
    return fake_result(summary="wrote it")


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_stop_mid_wave_pauses_the_task_and_leaves_later_waves(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    worker = FakeWorker(default=_write, delay_s=30)
    sched = Scheduler(_plan(), repo, "r1", worker, plan_path="plan.json")
    run = asyncio.create_task(sched.run())
    await _until(lambda: sched.state is not None and sched.state.tasks["a"].status == "running")

    sched.request_stop()
    state = await asyncio.wait_for(run, 10)

    assert state.finished is True
    assert state.pid is not None
    assert state.tasks["a"].status == "paused"
    assert state.tasks["a"].error == STOPPED_BY_USER
    assert state.tasks["b"].status == "pending"
    assert sched.stop_reason == STOPPED_BY_USER
    assert [c.order_id for c in worker.calls] == ["a"]
    assert load_state(sched.run_dir) == state
    assert sched.worktrees.path("a").exists()
    events = read_events(sched.run_dir / "events.jsonl")
    paused = [e for e in events if e.type == "task_paused"]
    assert [e.task_id for e in paused] == ["a"]
    assert paused[0].payload["reason"] == STOPPED_BY_USER
    finished = [e for e in events if e.type == "run_finished"][0]
    assert finished.payload["paused"] == 1
    assert finished.payload["stopped_reason"] == STOPPED_BY_USER


async def test_resume_after_stop_runs_everything(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    slow = Scheduler(_plan(), repo, "r1", FakeWorker(default=_write, delay_s=30))
    run = asyncio.create_task(slow.run())
    await _until(lambda: slow.state is not None and slow.state.tasks["a"].status == "running")
    slow.request_stop()
    await asyncio.wait_for(run, 10)

    resumed = Scheduler(_plan(), repo, "r1", FakeWorker(default=_write))
    state = await resumed.run(resume=True)

    assert [t.status for t in state.tasks.values()] == ["done", "done"]
    assert state.finished is True
    assert resumed.stop_reason is None


async def test_stop_before_any_task_starts_runs_nothing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    worker = FakeWorker(default=_write)
    sched = Scheduler(_plan(), repo, "r1", worker)
    sched.request_stop("custom reason")

    state = await sched.run()

    assert worker.calls == []
    assert sched.stop_reason == "custom reason"
    assert state.finished is True
    assert [t.status for t in state.tasks.values()] == ["pending", "pending"]


def test_sigterm_ends_a_cli_run_with_exit_3(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    plan = tmp_path / "plan.json"
    plan.write_text(_plan().model_dump_json(), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "harness", "run", str(plan), "--repo", str(repo)]
        + ["--fake", "--fake-delay", "2", "--run-id", "r1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    state_file = repo / ".harness" / "runs" / "r1" / "state.json"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data["tasks"]["a"]["status"] == "running":
                break
        time.sleep(0.05)
    else:
        proc.kill()
        raise AssertionError("run never started")
    assert data["pid"] == proc.pid

    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)

    assert proc.returncode == 3, out
    assert "Run stopped: stopped by user" in out
    state = load_state(repo / ".harness" / "runs" / "r1")
    assert state.finished is True
    assert state.tasks["a"].status == "paused"
    assert state.tasks["b"].status == "pending"
