import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness.config import Tier
from harness.events import EventLog, read_events
from harness.models import AttemptRecord, TaskState, WorkOrder
from harness.review import FakeReviewer, fake_review
from harness.router import Router, replan_note, tier_for_attempt
from harness.verify import FEEDBACK_HEADER, CommandResult, VerifyResult, format_feedback
from harness.worker import FakeWorker, WorkerResult, fake_result

NEXT_STEP = (
    "Next step: re-plan this order. Split it, paste the missing context, or fix its "
    "acceptance commands."
)


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


def _order(tier: Tier = "haiku", max_attempts: int = 3, **overrides: Any) -> WorkOrder:
    data: dict[str, Any] = {
        "id": "t1",
        "title": "Title",
        "goal": "Goal",
        "tier": tier,
        "depends_on": [],
        "files_owned": ["done.txt"],
        "files_readonly": [],
        "context": [],
        "steps": [],
        "constraints": [],
        "acceptance": ["test -f done.txt"],
        "budget": {"max_turns": 5, "max_attempts": max_attempts},
        "out_of_scope": [],
    }
    data.update(overrides)
    return WorkOrder.model_validate(data)


def _writes(status: str = "done", **kwargs: Any):
    def step(order, tier, cwd, feedback) -> WorkerResult:
        (cwd / "done.txt").write_text("ok\n")
        return fake_result(status, **kwargs)

    return step


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = _repo(tmp_path)
        self.log = EventLog(tmp_path / "run", "r1")
        self.statuses: list[str] = []

    async def cb(self, state: TaskState) -> None:
        self.statuses.append(state.status)

    def router(self, worker: FakeWorker, **kwargs: Any) -> Router:
        return Router(worker, self.log, on_state=self.cb, **kwargs)

    def types(self) -> list[str]:
        return [e.type for e in read_events(self.log.path)]

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in read_events(self.log.path) if e.type == type_]


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


# --- tier_for_attempt -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "attempt", "expected"),
    [
        ("haiku", 1, "haiku"),
        ("haiku", 2, "sonnet"),
        ("haiku", 3, "opus"),
        ("haiku", 4, "opus"),
        ("sonnet", 1, "sonnet"),
        ("sonnet", 2, "opus"),
        ("opus", 1, "opus"),
        ("opus", 2, "opus"),
        ("opus", 3, "opus"),
    ],
)
def test_tier_for_attempt(start: Tier, attempt: int, expected: Tier):
    assert tier_for_attempt(start, attempt) == expected


def test_tier_for_attempt_zero_raises():
    with pytest.raises(ValueError):
        tier_for_attempt("haiku", 0)


# --- replan_note ----------------------------------------------------------------------


def _record(n: int, tier: Tier, feedback: str) -> AttemptRecord:
    return AttemptRecord(
        attempt=n, tier=tier, worker_status="failed", passed=False, feedback=feedback
    )


def test_replan_note_exact_text():
    history = [_record(1, "sonnet", "first"), _record(2, "opus", "line a\nline b")]
    assert replan_note(_order(), history) == (
        "Task t1 is blocked after 2 attempts (tiers: sonnet, opus); the last one ran on opus.\n"
        "Last verifier output:\n"
        "line a\n"
        "line b\n" + NEXT_STEP
    )


def test_replan_note_empty_feedback_says_none():
    history = [_record(1, "opus", "   ")]
    assert replan_note(_order(), history) == (
        "Task t1 is blocked after 1 attempts (tiers: opus); the last one ran on opus.\n"
        "Last verifier output:\n"
        "(none)\n" + NEXT_STEP
    )


def test_replan_note_empty_history_says_none():
    assert replan_note(_order(), []).splitlines()[2] == "(none)"


def test_replan_note_keeps_first_40_feedback_lines():
    feedback = "\n".join(f"l{i}" for i in range(100))
    lines = replan_note(_order(), [_record(1, "opus", feedback)]).splitlines()
    assert lines[2:-1] == [f"l{i}" for i in range(40)]
    assert lines[-1] == NEXT_STEP


# --- route ----------------------------------------------------------------------------


async def test_pass_on_first_try(env: _Env):
    worker = FakeWorker({"t1": [_writes()]})
    state = await env.router(worker).route(_order(), env.repo, TaskState(task_id="t1"))
    assert state.status == "done"
    assert state.attempts == 1
    assert state.tier_used == "haiku"
    assert len(worker.calls) == 1
    assert worker.calls[0].retry_feedback is None
    assert env.types() == [
        "attempt_started",
        "worker_finished",
        "verify_started",
        "verify_finished",
        "task_done",
    ]
    assert env.statuses == ["running", "verifying", "done"]


async def test_escalation_haiku_fails_then_sonnet_passes(env: _Env):
    worker = FakeWorker({"t1": [fake_result("done"), _writes()]})
    state = await env.router(worker).route(_order(), env.repo, TaskState(task_id="t1"))
    assert state.status == "done"
    assert state.attempts == 2
    assert state.tier_used == "sonnet"
    assert [c.tier for c in worker.calls] == ["haiku", "sonnet"]
    feedback = worker.calls[1].retry_feedback
    assert feedback is not None
    assert feedback == state.history[0].feedback
    assert feedback.startswith(FEEDBACK_HEADER)
    assert "`test -f done.txt` exited 1" in feedback
    assert env.events("escalated") == [{"attempt": 2, "from_tier": "haiku", "to_tier": "sonnet"}]
    assert env.statuses == ["running", "verifying", "running", "verifying", "done"]


async def test_budget_exhausted_below_opus_fails(env: _Env):
    worker = FakeWorker(default=fake_result("done"))
    state = await env.router(worker).route(
        _order(max_attempts=2), env.repo, TaskState(task_id="t1")
    )
    assert state.status == "failed"
    assert state.error == "budget exhausted after 2 attempts; last tier sonnet"
    assert state.replan_note is None
    assert env.events("task_failed") == [{"attempts": 2, "tier": "sonnet", "reason": state.error}]
    assert env.events("task_blocked") == []
    assert env.statuses[-1] == "failed"


async def test_blocked_after_failing_on_opus(env: _Env):
    worker = FakeWorker(default=fake_result("done"))
    state = await env.router(worker).route(
        _order(max_attempts=3), env.repo, TaskState(task_id="t1")
    )
    assert [c.tier for c in worker.calls] == ["haiku", "sonnet", "opus"]
    assert state.status == "blocked"
    assert state.replan_note == replan_note(_order(), state.history)
    assert state.replan_note.startswith("Task t1 is blocked after 3 attempts")
    assert env.events("task_blocked") == [
        {"attempts": 3, "tier": "opus", "replan_note": state.replan_note}
    ]
    assert env.events("task_failed") == []
    assert len(env.events("escalated")) == 2


async def test_opus_order_stays_on_opus(env: _Env):
    worker = FakeWorker(default=fake_result("done"))
    state = await env.router(worker).route(
        _order(tier="opus", max_attempts=2), env.repo, TaskState(task_id="t1")
    )
    assert [c.tier for c in worker.calls] == ["opus", "opus"]
    assert env.events("escalated") == []
    assert state.status == "blocked"


async def test_done_claim_with_failing_acceptance_is_not_done(env: _Env):
    worker = FakeWorker(default=fake_result("done"))
    state = await env.router(worker).route(
        _order(max_attempts=1), env.repo, TaskState(task_id="t1")
    )
    assert state.status == "failed"
    assert state.history[0].worker_status == "done"
    assert state.history[0].passed is False


async def test_failed_claim_with_passing_acceptance_is_done(env: _Env):
    worker = FakeWorker({"t1": [_writes("failed")]})
    state = await env.router(worker).route(_order(), env.repo, TaskState(task_id="t1"))
    assert state.status == "done"
    assert state.history[0].worker_status == "failed"
    assert state.history[0].passed is True


async def test_raising_worker_counts_as_error_attempt_and_escalates(env: _Env):
    worker = FakeWorker({"t1": [RuntimeError("boom"), _writes()]})
    state = await env.router(worker).route(_order(), env.repo, TaskState(task_id="t1"))
    assert state.status == "done"
    assert [c.tier for c in worker.calls] == ["haiku", "sonnet"]
    assert state.history[0].worker_status == "error"
    finished = env.events("worker_finished")
    assert finished[0]["status"] == "error"
    assert finished[0]["error"] == "RuntimeError: boom"


async def test_usage_summed_across_attempts_and_review(env: _Env):
    worker = FakeWorker(
        {
            "t1": [
                fake_result("done", cost_usd=0.1, input_tokens=10, output_tokens=1),
                _writes(cost_usd=0.2, input_tokens=20, output_tokens=2),
            ]
        }
    )
    reviewer = FakeReviewer(default=fake_review("pass", cost_usd=0.05))
    state = await env.router(worker, reviewer=reviewer).route(
        _order(), env.repo, TaskState(task_id="t1")
    )
    assert state.status == "done"
    assert state.usage.input_tokens == 30
    assert state.usage.output_tokens == 3
    # The reviewer only runs once acceptance passes, so only on attempt 2.
    review_cost = 0.05 * len(reviewer.calls)
    assert state.usage.cost_usd == pytest.approx(0.3 + review_cost)
    assert sum(r.usage.cost_usd for r in state.history) == pytest.approx(state.usage.cost_usd)
    assert env.events("task_done")[0]["cost_usd"] == pytest.approx(state.usage.cost_usd)


async def test_reviewer_fail_then_pass_retries_with_review_feedback(env: _Env):
    worker = FakeWorker(default=_writes())
    reviewer = FakeReviewer([fake_review("fail", ["missing test"]), fake_review("pass")])
    state = await env.router(worker, reviewer=reviewer).route(
        _order(), env.repo, TaskState(task_id="t1")
    )
    assert state.status == "done"
    assert state.attempts == 2
    feedback = worker.calls[1].retry_feedback
    assert feedback is not None
    assert "## Reviewer verdict: fail" in feedback
    assert "- missing test" in feedback
    assert env.events("verify_finished")[0]["review_verdict"] == "fail"
    assert env.events("verify_finished")[0]["review_reasons"] == ["missing test"]


async def test_history_has_one_record_per_attempt(env: _Env):
    worker = FakeWorker({"t1": [fake_result("failed", summary="nope"), _writes(summary="yes")]})
    state = await env.router(worker).route(_order(), env.repo, TaskState(task_id="t1"))
    assert [(r.attempt, r.tier, r.worker_status, r.passed) for r in state.history] == [
        (1, "haiku", "failed", False),
        (2, "sonnet", "done", True),
    ]
    assert state.history[0].summary == "nope"
    assert state.history[0].feedback.startswith(FEEDBACK_HEADER)
    assert state.history[1].feedback == ""


async def test_monkeypatched_verify_drives_the_outcome(env: _Env, monkeypatch):
    calls: list[str] = []
    failing = VerifyResult(passed=False, commands=[CommandResult(cmd="x", exit_code=2, output="E")])

    async def fake_verify(order, cwd, **kwargs):
        calls.append(order.id)
        return failing

    monkeypatch.setattr("harness.router.verify", fake_verify)
    worker = FakeWorker(default=_writes())
    state = await env.router(worker).route(
        _order(max_attempts=1), env.repo, TaskState(task_id="t1")
    )
    assert calls == ["t1"]
    assert state.status == "failed"
    assert state.history[0].feedback == format_feedback(failing)
