import subprocess
import time
from pathlib import Path

import pytest

from harness import verify as verify_mod
from harness.models import Budget, WorkOrder
from harness.review import FakeReviewer, ReviewResult, fake_review
from harness.verify import (
    FEEDBACK_HEADER,
    MAX_DIFF_CHARS,
    CommandResult,
    VerifyResult,
    changed_files,
    diff_text,
    files_outside,
    format_feedback,
    is_ignored,
    run_command,
    verify,
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
    (repo / "other.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _order(acceptance: list[str] | None = None, files_owned: list[str] | None = None) -> WorkOrder:
    return WorkOrder(
        id="t1",
        title="t",
        goal="g",
        tier="haiku",
        depends_on=[],
        files_owned=files_owned or ["a.py"],
        files_readonly=[],
        context=[],
        steps=[],
        constraints=[],
        acceptance=acceptance or ["true"],
        budget=Budget(max_turns=5, max_attempts=2),
        out_of_scope=[],
    )


# --- run_command ---------------------------------------------------------------------------


async def test_run_command_ok(tmp_path: Path) -> None:
    r = await run_command("echo hi", tmp_path)
    assert r.exit_code == 0
    assert r.output == "hi"
    assert r.passed
    assert not r.timed_out


async def test_run_command_stderr_and_exit(tmp_path: Path) -> None:
    r = await run_command("echo oops >&2; exit 3", tmp_path)
    assert r.exit_code == 3
    assert "oops" in r.output
    assert not r.passed


async def test_run_command_timeout(tmp_path: Path) -> None:
    r = await run_command("sleep 5", tmp_path, timeout_s=0.5)
    assert r.timed_out
    assert r.exit_code is None
    assert r.duration_s < 4
    assert not r.passed


async def test_run_command_tail(tmp_path: Path) -> None:
    r = await run_command("for i in $(seq 0 299); do echo line $i; done", tmp_path)
    lines = r.output.splitlines()
    assert len(lines) == 200
    assert lines[0] == "line 100"
    assert lines[-1] == "line 299"


async def test_run_command_missing_cwd(tmp_path: Path) -> None:
    r = await run_command("echo hi", tmp_path / "nope")
    assert r.exit_code is None
    assert r.output.startswith("failed to start: ")
    assert not r.passed


async def test_run_command_background_child(tmp_path: Path) -> None:
    start = time.monotonic()
    r = await run_command("sleep 30 & echo bg", tmp_path)
    assert time.monotonic() - start < 10
    assert r.exit_code == 0
    assert "bg" in r.output


async def test_run_command_strips_virtual_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/x")
    r = await run_command('test -z "$VIRTUAL_ENV"', tmp_path)
    assert r.exit_code == 0


# --- diff check ----------------------------------------------------------------------------


async def test_untracked_owned_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("print(1)\n")
    res = await verify(_order(), repo)
    assert res.changed_files == ["a.py"]
    assert res.outside_files == []
    assert res.passed


async def test_tracked_edit_outside(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("changed\n")
    res = await verify(_order(), repo)
    assert res.outside_files == ["README.md"]
    assert not res.passed


async def test_deletion_counts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "other.py").unlink()
    res = await verify(_order(), repo)
    assert res.outside_files == ["other.py"]
    assert not res.passed


def test_committed_change_after_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _out(repo, "rev-parse", "HEAD")
    (repo / "other.py").write_text("x = 2\n")
    subprocess.run(["git", "commit", "-am", "c2"], cwd=repo, check=True, capture_output=True)
    assert changed_files(repo) == []
    assert changed_files(repo, base) == ["other.py"]


def test_ignored_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("secret.txt\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "gi"], cwd=repo, check=True, capture_output=True)
    for rel in (
        "__pycache__/m.cpython-312.pyc",
        ".pytest_cache/x",
        ".ruff_cache/x",
        ".venv/x",
        "secret.txt",
    ):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("junk\n")
    assert changed_files(repo) == []


def test_is_ignored() -> None:
    assert is_ignored("pkg/__pycache__/m.py")
    assert is_ignored("a.pyc")
    assert not is_ignored("src/a.py")


def test_files_outside_normalizes() -> None:
    assert files_outside(["./a.py"], ["a.py"]) == []
    assert files_outside(["b.py", "a.py", "b.py"], ["./a.py"]) == ["b.py"]


async def test_bad_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    res = await verify(_order(), repo, base="nope")
    assert res.scope_error is not None
    assert res.scope_error.startswith("git diff failed: ")
    assert not res.passed
    assert res.changed_files == []


def test_diff_text(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("line one\nnew line\n")
    (repo / "a.py").write_text("brand new content\n")
    text = diff_text(repo)
    assert "+new line" in text
    assert "brand new content" in text


def test_diff_text_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert diff_text(repo) == ""


def test_diff_text_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(verify_mod, "MAX_DIFF_CHARS", 50)
    (repo / "a.py").write_text("y" * 500 + "\n")
    text = diff_text(repo)
    assert text.endswith("\n[diff truncated at 50 characters]\n")
    assert len(text) == 50 + len("\n[diff truncated at 50 characters]\n")
    assert MAX_DIFF_CHARS == 200_000


# --- verify --------------------------------------------------------------------------------


async def test_verify_all_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    res = await verify(_order(["true", "echo ok"]), repo)
    assert res.passed
    assert len(res.commands) == 2
    assert format_feedback(res) == ""


async def test_verify_runs_all_commands(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    res = await verify(_order(["exit 1", "echo second"]), repo)
    assert not res.passed
    assert [c.exit_code for c in res.commands] == [1, 0]
    assert res.commands[1].output == "second"


async def test_verify_reviewer_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("hello reviewer\n")
    reviewer = FakeReviewer([fake_review("pass")])
    res = await verify(_order(), repo, reviewer=reviewer, review_tier="opus")
    assert res.passed
    assert res.review is not None and res.review.verdict == "pass"
    assert len(reviewer.calls) == 1
    call = reviewer.calls[0]
    assert call.tier == "opus"
    assert call.order_id == "t1"
    assert call.cwd == repo
    assert "hello reviewer" in call.diff


async def test_verify_reviewer_fail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x\n")
    reviewer = FakeReviewer([fake_review("fail", ["missing tests"])])
    res = await verify(_order(), repo, reviewer=reviewer)
    assert not res.passed
    fb = format_feedback(res)
    assert "## Reviewer verdict: fail\n\n- missing tests" in fb


async def test_reviewer_not_called_on_command_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    reviewer = FakeReviewer(default=fake_review("pass"))
    res = await verify(_order(["exit 2"]), repo, reviewer=reviewer)
    assert not res.passed
    assert reviewer.calls == []
    assert res.review is None


async def test_reviewer_not_called_on_outside_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("bad\n")
    reviewer = FakeReviewer(default=fake_review("pass"))
    res = await verify(_order(), repo, reviewer=reviewer)
    assert not res.passed
    assert reviewer.calls == []


@pytest.mark.parametrize("verdict", ["error", "unknown"])
async def test_reviewer_error_or_unknown_passes(tmp_path: Path, verdict: str) -> None:
    repo = _repo(tmp_path)
    reviewer = FakeReviewer([fake_review(verdict)])  # type: ignore[arg-type]
    res = await verify(_order(), repo, reviewer=reviewer)
    assert res.passed
    assert res.review is not None and res.review.verdict == verdict


async def test_reviewer_raises(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    reviewer = FakeReviewer([ValueError("kaboom")])
    res = await verify(_order(), repo, reviewer=reviewer)
    assert res.passed
    assert res.review is not None
    assert res.review.verdict == "error"
    assert res.review.error == "ValueError: kaboom"


# --- format_feedback -----------------------------------------------------------------------


def _failed(**kwargs: object) -> VerifyResult:
    return VerifyResult(passed=False, **kwargs)  # type: ignore[arg-type]


def test_feedback_example() -> None:
    res = _failed(commands=[CommandResult(cmd="uv run pytest -q", exit_code=1, output="E boom")])
    assert format_feedback(res) == (
        "Verification failed. Fix the problems below, then run every acceptance command again."
        "\n\n## `uv run pytest -q` exited 1\n\n```text\nE boom\n```"
    )


def test_feedback_skips_passed_commands() -> None:
    res = _failed(
        commands=[
            CommandResult(cmd="ok", exit_code=0, output="fine"),
            CommandResult(cmd="bad", exit_code=2, output="err"),
        ]
    )
    assert format_feedback(res) == FEEDBACK_HEADER + "\n\n## `bad` exited 2\n\n```text\nerr\n```"


def test_feedback_timeout() -> None:
    res = _failed(commands=[CommandResult(cmd="sleep 9", exit_code=None, timed_out=True)])
    assert format_feedback(res) == (
        FEEDBACK_HEADER + "\n\n## `sleep 9` timed out\n\n```text\n(no output)\n```"
    )


def test_feedback_failed_to_start() -> None:
    res = _failed(commands=[CommandResult(cmd="x", exit_code=None, output="failed to start: boom")])
    assert format_feedback(res) == (
        FEEDBACK_HEADER + "\n\n## `x` failed to start\n\n```text\nfailed to start: boom\n```"
    )


def test_feedback_empty_output() -> None:
    res = _failed(commands=[CommandResult(cmd="false", exit_code=1, output="  \n")])
    assert format_feedback(res) == (
        FEEDBACK_HEADER + "\n\n## `false` exited 1\n\n```text\n(no output)\n```"
    )


def test_feedback_outside_files() -> None:
    res = _failed(outside_files=["README.md", "b.py"])
    assert format_feedback(res) == (
        FEEDBACK_HEADER + "\n\n## Files changed outside files_owned\n\n- README.md\n- b.py"
        "\n\nUndo these changes. Only the files in files_owned may change."
    )


def test_feedback_scope_error() -> None:
    res = _failed(scope_error="git diff failed: fatal: bad revision 'nope'")
    assert format_feedback(res) == (
        FEEDBACK_HEADER + "\n\n## Diff check failed\n\ngit diff failed: fatal: bad revision 'nope'"
    )


def test_feedback_review_no_reasons() -> None:
    res = _failed(review=ReviewResult(verdict="fail"))
    assert format_feedback(res) == (
        FEEDBACK_HEADER + "\n\n## Reviewer verdict: fail\n\n(no reasons given)"
    )


def test_feedback_passed_is_empty() -> None:
    assert format_feedback(VerifyResult(passed=True)) == ""
