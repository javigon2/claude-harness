"""Run git for the Git view and check what the page sends before it reaches git.

Every failure is a GitOpError whose message is a sentence for the user."""

import os
import subprocess
from pathlib import Path, PurePosixPath

from harness.ui.gitops import GitOpError

GIT_TIMEOUT_S = 30.0
NETWORK_TIMEOUT_S = 120.0
PATCH_MAX = 200_000
HARNESS_PREFIX = "harness/"
NAME_MAX = 200


class NoRepoError(GitOpError):
    """The project is not a git repository. Routes answer 409."""


def run(
    repo: Path, *args: str, ok: tuple[int, ...] = (0,), timeout: float = GIT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """`git -C <repo> <args>` with stdin closed, GIT_TERMINAL_PROMPT=0, GIT_OPTIONAL_LOCKS=0
    and LC_ALL=C added to the environment. Raises GitOpError `cannot run git: <err>` when git
    cannot start (OSError), `git timed out after <n> s` after `timeout` seconds (<n> is
    f"{timeout:g}"), and, when the exit code is not in `ok`, git's stderr stripped, else its
    stdout stripped, else `git failed`."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except OSError as err:
        raise GitOpError(f"cannot run git: {err}") from err
    except subprocess.TimeoutExpired as err:
        raise GitOpError(f"git timed out after {timeout:g} s") from err
    if proc.returncode not in ok:
        message = proc.stderr.strip() or proc.stdout.strip() or "git failed"
        raise GitOpError(message)
    return proc


def out(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> str:
    """The stdout of run(repo, *args, timeout=timeout)."""
    return run(repo, *args, timeout=timeout).stdout


def require_repo(repo: Path) -> None:
    """Raise NoRepoError `not a git repository: <repo>` (the path as given) unless
    `git rev-parse --git-dir` succeeds in `repo`."""
    try:
        run(repo, "rev-parse", "--git-dir")
    except GitOpError as err:
        raise NoRepoError(f"not a git repository: {repo}") from err


def check_branch_name(name: str) -> str:
    """Return `name` if it is a valid branch name; else GitOpError
    `not a valid branch name: <name>`. Refused before git runs: empty, longer than NAME_MAX,
    a leading "-", a NUL. Then `git check-ref-format --branch <name>` must exit 0."""
    bad = f"not a valid branch name: {name}"
    if not name or len(name) > NAME_MAX or name.startswith("-") or "\0" in name:
        raise GitOpError(bad)
    try:
        run(Path("."), "check-ref-format", "--branch", name)
    except GitOpError as err:
        raise GitOpError(bad) from err
    return name


def resolve(repo: Path, rev: str) -> str:
    """The full sha of the commit `rev` names; else GitOpError
    `unknown commit or branch: <rev>`. Refused before git runs: empty, longer than NAME_MAX,
    a leading "-", whitespace, a NUL. Then
    `git rev-parse --verify -q --end-of-options <rev>^{commit}` must exit 0 with output."""
    unknown = f"unknown commit or branch: {rev}"
    if (
        not rev
        or len(rev) > NAME_MAX
        or rev.startswith("-")
        or any(ch.isspace() for ch in rev)
        or "\0" in rev
    ):
        raise GitOpError(unknown)
    proc = run(
        repo,
        "rev-parse",
        "--verify",
        "-q",
        "--end-of-options",
        f"{rev}^{{commit}}",
        ok=(0, 1, 128),
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise GitOpError(unknown)
    return sha


def check_path(path: str) -> str:
    """Return `path` if it is a safe repo-relative path; else GitOpError `bad path: <path>`.
    Refused: empty, a NUL, absolute, any ".." part, ".harness" or anything under
    ".harness/"."""
    bad = f"bad path: {path}"
    if not path or "\0" in path:
        raise GitOpError(bad)
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts:
        raise GitOpError(bad)
    if path == ".harness" or path.startswith(".harness/"):
        raise GitOpError(bad)
    return path


def cap(text: str) -> tuple[str, bool]:
    """(the first PATCH_MAX characters of `text`, len(text) > PATCH_MAX)."""
    return text[:PATCH_MAX], len(text) > PATCH_MAX
