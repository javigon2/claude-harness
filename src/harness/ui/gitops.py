"""The two git actions the page offers on the open project: Initialize (make it a git repo
on `main` with a first commit) and Bring into origin (fast-forward a local origin to main).

Every failure is a GitOpError whose message is a sentence for the user."""

import subprocess
import tempfile
from pathlib import Path

from harness.ui.projects import local_origin
from harness.worktree import WorktreeError, ensure_excluded

INIT_MESSAGE = "Initial commit"
PREVIEW_MAX = 2000
HARNESS_DIR = ".harness"


class GitOpError(Exception):
    """A git action was refused or failed. The message is a sentence for the user; the server
    answers 409 with it."""


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as err:
        raise GitOpError(f"cannot run git: {err}") from err


def _ok(proc: subprocess.CompletedProcess[str]) -> str:
    """stdout of a finished git command, or GitOpError(stderr)."""
    if proc.returncode != 0:
        raise GitOpError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return proc.stdout


def _has_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _listing(args: list[str]) -> list[str]:
    names = _ok(_git(*args)).split("\0")
    return [n for n in names if n and n != HARNESS_DIR and not n.startswith(HARNESS_DIR + "/")]


def init_preview(path: Path) -> tuple[list[str], bool]:
    """(files, truncated): what the first commit would hold, sorted, without `.harness/`:
    tracked and untracked files that git does not ignore. At most PREVIEW_MAX names;
    `truncated` is true when there were more. Changes nothing in the folder."""
    path = Path(path)
    flags = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    if _has_repo(path):
        files = _listing(["-C", str(path), *flags])
    else:
        with tempfile.TemporaryDirectory() as tmp:
            git_dir = str(Path(tmp) / "git")
            _ok(_git("init", "--bare", "-q", git_dir))
            files = _listing([f"--git-dir={git_dir}", f"--work-tree={path}", *flags])
    files = sorted(set(files))
    return files[:PREVIEW_MAX], len(files) > PREVIEW_MAX


def _branch(path: Path) -> str | None:
    proc = _git("-C", str(path), "symbolic-ref", "--short", "-q", "HEAD")
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _has_commit(path: Path) -> bool:
    return _git("-C", str(path), "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def _short_head(path: Path) -> str:
    return _ok(_git("-C", str(path), "rev-parse", "--short", "HEAD")).strip()


def init_project(path: Path) -> str:
    """Make `path` a git repo on `main` with a commit and return the short HEAD sha.

    No `.git`: `git init -b main`. `.harness/` is excluded first. No commit yet: stage
    everything and commit INIT_MESSAGE (an empty folder gets an empty commit); an unborn
    branch other than main becomes main. A repo with commits that is not on `main`: create
    `main` here, or switch to it when it exists and the tree is clean. A repo on `main` with
    a commit has nothing to initialize."""
    path = Path(path)
    if not path.is_dir():
        raise GitOpError(f"not a folder: {path}")
    here = str(path)
    if not _has_repo(path):
        _ok(_git("init", "-b", "main", here))
    try:
        ensure_excluded(path)
    except (WorktreeError, OSError) as err:
        raise GitOpError(f"cannot exclude .harness/ in {path}: {err}") from err
    branch = _branch(path)
    if not _has_commit(path):
        if branch != "main":
            _ok(_git("-C", here, "symbolic-ref", "HEAD", "refs/heads/main"))
        _ok(_git("-C", here, "add", "-A"))
        _ok(_git("-C", here, "commit", "--allow-empty", "-q", "-m", INIT_MESSAGE))
        return _short_head(path)
    if branch == "main":
        raise GitOpError("nothing to initialize: the project is a git repo on main with a commit")
    main_exists = _git("-C", here, "rev-parse", "--verify", "-q", "refs/heads/main").returncode == 0
    if not main_exists:
        _ok(_git("-C", here, "switch", "-c", "main"))
        return _short_head(path)
    if _ok(_git("-C", here, "status", "--porcelain")).strip():
        raise GitOpError(f"commit or stash your changes on {branch or 'the detached HEAD'} first")
    _ok(_git("-C", here, "switch", "main"))
    return _short_head(path)


def pull_into_origin(clone: Path) -> str:
    """Fast-forward the local `origin` of `clone` to the clone's main with
    `git -C <origin> pull --ff-only <clone> main`; returns git's last output line. Raises
    GitOpError if origin is not a local folder, is not on main, or git refuses."""
    clone = Path(clone)
    origin = local_origin(clone)
    if origin is None:
        raise GitOpError("origin is not a local folder")
    on = _branch(origin)
    if on != "main":
        raise GitOpError(f"origin is on {on or 'no branch'}, not main")
    out = _ok(_git("-C", str(origin), "pull", "--ff-only", str(clone.resolve()), "main"))
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else "Already up to date."
