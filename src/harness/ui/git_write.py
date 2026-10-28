"""The Git view's local write actions on the open project: stage, unstage, commit, and
create, switch, merge and delete branches.

Every failure is a GitOpError whose message is a sentence for the user."""

from pathlib import Path

from harness.ui.git_cmd import (
    HARNESS_PREFIX,
    check_branch_name,
    check_path,
    out,
    require_repo,
    resolve,
    run,
)
from harness.ui.gitops import GitOpError

EXCLUDE_HARNESS = ":(exclude).harness"


def _paths(paths: list[str]) -> list[str]:
    """`paths` after check_path on each; also GitOpError `bad path: <path>` for a path that
    starts with ":" (git pathspec magic). An empty list is GitOpError
    `choose at least one file`."""
    if not paths:
        raise GitOpError("choose at least one file")
    for path in paths:
        check_path(path)
        if path.startswith(":"):
            raise GitOpError(f"bad path: {path}")
    return list(paths)


def _has_head(repo: Path) -> bool:
    """True when HEAD names a commit (`git rev-parse --verify -q HEAD` exits 0)."""
    return run(repo, "rev-parse", "--verify", "-q", "HEAD", ok=(0, 1)).returncode == 0


def stage(repo: Path, paths: list[str]) -> None:
    """`git add -A -- <paths> :(exclude).harness`."""
    require_repo(repo)
    checked = _paths(paths)
    run(repo, "add", "-A", "--", *checked, EXCLUDE_HARNESS)


def unstage(repo: Path, paths: list[str]) -> None:
    """`git restore --staged -- <paths> :(exclude).harness`; before the first commit
    `git rm --cached -r -q -- <paths> :(exclude).harness`."""
    require_repo(repo)
    checked = _paths(paths)
    if _has_head(repo):
        run(repo, "restore", "--staged", "--", *checked, EXCLUDE_HARNESS)
    else:
        run(repo, "rm", "--cached", "-r", "-q", "--", *checked, EXCLUDE_HARNESS)


def commit(repo: Path, message: str) -> str:
    """Commit what is staged with `message`; return the new short HEAD sha."""
    require_repo(repo)
    if not message.strip():
        raise GitOpError("write a commit message")
    if run(repo, "diff", "--cached", "--quiet", ok=(0, 1)).returncode == 0:
        raise GitOpError("nothing staged to commit")
    run(repo, "commit", "-q", "-m", message)
    return out(repo, "rev-parse", "--short", "HEAD").strip()


_RUNS = "harness/ branches belong to runs"


def _current(repo: Path) -> str | None:
    """The checked-out branch (`git symbolic-ref --short -q HEAD`), None when detached."""
    proc = run(repo, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else None


def _local(repo: Path, name: str) -> bool:
    """True when `refs/heads/<name>` exists."""
    return run(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}", ok=(0, 1)).returncode == 0


def create_branch(repo: Path, name: str, start: str) -> None:
    """Create branch `name` at the commit `start` names and check it out."""
    require_repo(repo)
    check_branch_name(name)
    if name.startswith(HARNESS_PREFIX):
        raise GitOpError(_RUNS)
    sha = resolve(repo, start)
    if _local(repo, name):
        raise GitOpError(f"a branch named {name} already exists")
    run(repo, "switch", "-c", name, sha)


def switch_branch(repo: Path, name: str) -> None:
    """Check out the local branch `name`."""
    require_repo(repo)
    check_branch_name(name)
    if name.startswith(HARNESS_PREFIX):
        raise GitOpError(_RUNS)
    if not _local(repo, name):
        raise GitOpError(f"no local branch named {name}")
    run(repo, "switch", name)


def _dirty(repo: Path) -> bool:
    status = out(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    for field in status.split("\0"):
        if not field:
            continue
        path = field[3:]
        if path == ".harness" or path.startswith(".harness/"):
            continue
        return True
    return False


def merge_branch(repo: Path, name: str) -> str:
    """Merge the local or remote-tracking branch `name` into the checked-out branch with a
    merge commit; return the short HEAD sha. A conflict is aborted, so nothing changes."""
    require_repo(repo)
    check_branch_name(name)
    if _current(repo) is None:
        raise GitOpError("check out a branch first")
    if _dirty(repo):
        raise GitOpError("commit or stash your changes first")
    known = any(
        run(repo, "rev-parse", "--verify", "-q", ref, ok=(0, 1)).returncode == 0
        for ref in (f"refs/heads/{name}", f"refs/remotes/{name}")
    )
    if not known:
        raise GitOpError(f"unknown branch: {name}")
    proc = run(repo, "merge", "--no-ff", "--no-edit", name, ok=(0, 1))
    if proc.returncode == 0:
        return out(repo, "rev-parse", "--short", "HEAD").strip()
    listing = out(repo, "diff", "--name-only", "--diff-filter=U", "-z")
    files = [f for f in listing.split("\0") if f]
    if not files:
        raise GitOpError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    try:
        run(repo, "merge", "--abort")
    except GitOpError as err:
        raise GitOpError(
            f"{name} does not merge cleanly and git merge --abort failed: {err}"
        ) from err
    raise GitOpError(
        f"{name} does not merge cleanly: conflict in {', '.join(files)}; nothing was changed"
    )


def delete_branch(repo: Path, name: str) -> None:
    """Delete the local branch `name` if it is fully merged."""
    require_repo(repo)
    check_branch_name(name)
    if name.startswith(HARNESS_PREFIX):
        raise GitOpError(_RUNS)
    if name == _current(repo):
        raise GitOpError("cannot delete the current branch")
    if not _local(repo, name):
        raise GitOpError(f"no local branch named {name}")
    proc = run(repo, "branch", "-d", "--", name, ok=(0, 1))
    if proc.returncode == 1:
        if "not fully merged" in proc.stderr:
            raise GitOpError(f"{name} is not fully merged; merge it first")
        raise GitOpError(proc.stderr.strip() or "git failed")
