"""Verifier: run an order's acceptance commands in its worktree, check that the changes stay
inside files_owned, and optionally ask the reviewer."""

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from harness.config import Tier
from harness.models import WorkOrder
from harness.review import Reviewer, ReviewResult

DEFAULT_TIMEOUT_S = 600.0

TAIL_LINES = 200

IGNORED_DIRS: tuple[str, ...] = ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv")

IGNORED_SUFFIXES: tuple[str, ...] = (".pyc",)

MAX_DIFF_CHARS = 200_000

FEEDBACK_HEADER = (
    "Verification failed. Fix the problems below, then run every acceptance command again."
)


class VerifyError(RuntimeError):
    """A git command needed for the diff check failed. The message says which and why."""


class CommandResult(BaseModel):
    """One acceptance command as the verifier ran it."""

    model_config = ConfigDict(extra="forbid")

    cmd: str
    exit_code: int | None
    timed_out: bool = False
    output: str = ""
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        """True only if the command exited 0 (so not timed out, and it started)."""
        return self.exit_code == 0 and not self.timed_out


class VerifyResult(BaseModel):
    """The verifier's verdict on one attempt. `passed` is the only thing the router trusts."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    commands: list[CommandResult] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    outside_files: list[str] = Field(default_factory=list)
    scope_error: str | None = None
    review: ReviewResult | None = None


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _wait_exit(proc: asyncio.subprocess.Process) -> int:
    # Process.wait() only resolves once every pipe is closed, so a background child holding
    # stdout open would block it. proc.returncode is set as soon as the shell itself exits.
    while proc.returncode is None:
        await asyncio.sleep(0.02)
    return proc.returncode


async def _read_all(stream: asyncio.StreamReader, buf: bytearray) -> None:
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        buf.extend(chunk)


async def run_command(cmd: str, cwd: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> CommandResult:
    """Run `cmd` through the shell in `cwd` with a timeout; capture the exit code and the
    last TAIL_LINES lines of combined stdout+stderr. Never raises except CancelledError
    (after killing the process group). Rules in the `decision: run_command` context item."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except OSError as err:
        return CommandResult(
            cmd=cmd,
            exit_code=None,
            timed_out=False,
            output=f"failed to start: {err}",
            duration_s=time.monotonic() - start,
        )

    buf = bytearray()
    assert proc.stdout is not None
    reader = asyncio.create_task(_read_all(proc.stdout, buf))
    timed_out = False
    try:
        try:
            await asyncio.wait_for(_wait_exit(proc), timeout_s)
        except TimeoutError:
            timed_out = True
            _kill_group(proc.pid)
            await _wait_exit(proc)
        _kill_group(proc.pid)
        try:
            await asyncio.wait_for(reader, 5.0)
        except TimeoutError:
            reader.cancel()
    except BaseException:
        _kill_group(proc.pid)
        reader.cancel()
        raise

    text = bytes(buf).decode("utf-8", errors="replace")
    output = "\n".join(text.splitlines()[-TAIL_LINES:])
    return CommandResult(
        cmd=cmd,
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        output=output,
        duration_s=time.monotonic() - start,
    )


def is_ignored(path: str) -> bool:
    """True if any component of the repo-relative POSIX `path` is in IGNORED_DIRS, or the
    path ends with a suffix in IGNORED_SUFFIXES."""
    return any(part in IGNORED_DIRS for part in PurePosixPath(path).parts) or path.endswith(
        IGNORED_SUFFIXES
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def _git_out(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise VerifyError(f"git {args[0]} failed: {stderr.strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def _split_z(text: str) -> list[str]:
    return [p for p in text.split("\0") if p]


def _tracked_untracked(cwd: Path, base: str) -> tuple[list[str], list[str]]:
    tracked = _split_z(_git_out(cwd, "diff", "--name-only", "--no-renames", "-z", base))
    untracked = _split_z(_git_out(cwd, "ls-files", "--others", "--exclude-standard", "-z"))
    return (
        sorted({p for p in tracked if not is_ignored(p)}),
        sorted({p for p in untracked if not is_ignored(p)}),
    )


def changed_files(cwd: Path, base: str = "HEAD") -> list[str]:
    """Every path changed in the worktree `cwd` relative to commit `base`: tracked changes
    (committed or not, including deletions) plus untracked files that .gitignore does not
    exclude, minus is_ignored() paths. Sorted, unique, repo-relative POSIX paths as git
    prints them. Raises VerifyError if a git command fails. Rules in the
    `decision: diff check` context item."""
    tracked, untracked = _tracked_untracked(cwd, base)
    return sorted(set(tracked + untracked))


def files_outside(changed: Sequence[str], files_owned: Sequence[str]) -> list[str]:
    """The paths in `changed` that are not in `files_owned`, comparing
    str(PurePosixPath(p)) of both sides (so "./a.py" matches "a.py"). Returns the
    changed paths as given, sorted, unique."""
    owned = {str(PurePosixPath(p)) for p in files_owned}
    return sorted({p for p in changed if str(PurePosixPath(p)) not in owned})


def diff_text(cwd: Path, base: str = "HEAD") -> str:
    """A unified diff of every change changed_files() reports, for the reviewer. Raises
    VerifyError if a git command fails. Rules in the `decision: diff check` context item."""
    tracked, untracked = _tracked_untracked(cwd, base)
    parts: list[str] = []
    if tracked:
        parts.append(
            _git_out(
                cwd, "diff", "--no-color", "--no-ext-diff", "--no-renames", base, "--", *tracked
            )
        )
    for path in untracked:
        proc = _git(
            cwd, "diff", "--no-color", "--no-ext-diff", "--no-index", "--", "/dev/null", path
        )
        if proc.returncode not in (0, 1):
            stderr = proc.stderr.decode("utf-8", errors="replace")
            raise VerifyError(f"git diff --no-index failed: {stderr.strip()}")
        parts.append(proc.stdout.decode("utf-8", errors="replace"))
    text = "".join(parts)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n[diff truncated at {MAX_DIFF_CHARS} characters]\n"
    return text


async def verify(
    order: WorkOrder,
    cwd: Path,
    *,
    base: str = "HEAD",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    reviewer: Reviewer | None = None,
    review_tier: Tier = "sonnet",
) -> VerifyResult:
    """Verify one attempt of `order` in the worktree `cwd`. Algorithm in the
    `decision: verify algorithm` context item. Never raises except CancelledError."""
    commands = [await run_command(c, cwd, timeout_s) for c in order.acceptance]
    scope_error: str | None
    try:
        changed = changed_files(cwd, base)
        scope_error = None
    except VerifyError as err:
        changed = []
        scope_error = str(err)
    outside = files_outside(changed, order.files_owned)
    ok = all(c.passed for c in commands) and scope_error is None and not outside

    review: ReviewResult | None = None
    if ok and reviewer is not None:
        try:
            diff = diff_text(cwd, base)
        except VerifyError as err:
            review = ReviewResult(verdict="error", error=str(err))
        else:
            try:
                review = await reviewer.review(order, diff, review_tier, cwd)
            except Exception as err:
                review = ReviewResult(verdict="error", error=f"{type(err).__name__}: {err}"[:2000])

    passed = ok and (review is None or review.verdict != "fail")
    return VerifyResult(
        passed=passed,
        commands=commands,
        changed_files=changed,
        outside_files=outside,
        scope_error=scope_error,
        review=review,
    )


def format_feedback(result: VerifyResult) -> str:
    """The retry feedback for the next attempt, or "" if result.passed. Exact format in the
    `decision: feedback format` context item."""
    if result.passed:
        return ""
    sections = [FEEDBACK_HEADER]
    for c in result.commands:
        if c.passed:
            continue
        if c.timed_out:
            status = "timed out"
        elif c.exit_code is None:
            status = "failed to start"
        else:
            status = f"exited {c.exit_code}"
        out = c.output if c.output.strip() else "(no output)"
        sections.append(f"## `{c.cmd}` {status}\n\n```text\n{out}\n```")
    if result.scope_error:
        sections.append(f"## Diff check failed\n\n{result.scope_error}")
    if result.outside_files:
        sections.append(
            "## Files changed outside files_owned\n\n"
            + "\n".join(f"- {p}" for p in result.outside_files)
            + "\n\nUndo these changes. Only the files in files_owned may change."
        )
    if result.review is not None and result.review.verdict == "fail":
        sections.append(
            "## Reviewer verdict: fail\n\n"
            + ("\n".join(f"- {r}" for r in result.review.reasons) or "(no reasons given)")
        )
    return "\n\n".join(sections)
