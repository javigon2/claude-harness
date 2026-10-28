"""Work branches: agents commit on a `work/*` branch cut from the branch the user had
checked out. That branch is its base, kept in git config as `branch.<name>.harnessbase`;
Approve merges the work branch back into it.

Every failure is a GitOpError whose message is a sentence for the user."""

from pathlib import Path

from harness.ui.git_cmd import check_branch_name, out, require_repo, run
from harness.ui.git_write import _current, _dirty, _has_head, _local
from harness.ui.gitops import GitOpError

WORK_PREFIX = "work/"
BASE_KEY = "harnessbase"
SUFFIX_MAX = 99


def current_branch(repo: Path) -> str | None:
    """The checked-out branch, None when HEAD is detached. Raises NoRepoError outside a
    repo."""
    require_repo(repo)
    return _current(repo)


def base_of(repo: Path, name: str) -> str | None:
    """The base recorded for branch `name` (`git config --get branch.<name>.harnessbase`,
    stripped), None when it is unset or empty."""
    proc = run(repo, "config", "--get", f"branch.{name}.{BASE_KEY}", ok=(0, 1))
    value = proc.stdout.strip()
    return value or None


def ensure_work_branch(repo: Path, label: str) -> str:
    """Return the work branch to commit on; it is checked out on return. A checked-out
    `work/*` branch is kept as it is. Otherwise `work/<label>` (or `work/<label>-2`, `-3`, ...
    when taken) is created at HEAD and checked out, and the branch that was checked out is
    recorded as its base. Uncommitted changes stay in the working tree."""
    require_repo(repo)
    current = _current(repo)
    if current is None:
        raise GitOpError("check out a branch first")
    if not _has_head(repo):
        raise GitOpError("the project has no commit yet; initialize it first")
    if current.startswith(WORK_PREFIX):
        return current
    name = check_branch_name(WORK_PREFIX + label)
    n = 2
    while _local(repo, name):
        if n > SUFFIX_MAX:
            raise GitOpError(f"too many work branches named {WORK_PREFIX}{label}")
        name = f"{WORK_PREFIX}{label}-{n}"
        n += 1
    run(repo, "switch", "-q", "-c", name)
    run(repo, "config", f"branch.{name}.{BASE_KEY}", current)
    return name


def approve(repo: Path, name: str) -> str:
    """Merge the work branch `name` into its recorded base with a merge commit
    `Merge <name> into <base>` and return the short sha of that commit. The base stays
    checked out afterwards. A conflict is aborted and the branch that was checked out is
    checked out again, so nothing changes."""
    require_repo(repo)
    check_branch_name(name)
    if not name.startswith(WORK_PREFIX) or not _local(repo, name):
        raise GitOpError(f"{name} is not a work branch")
    base = base_of(repo, name)
    if base is None:
        raise GitOpError(f"{name} has no base branch recorded")
    if not _local(repo, base):
        raise GitOpError(f"no local branch named {base}")
    count = out(repo, "rev-list", "--count", f"refs/heads/{base}..refs/heads/{name}").strip()
    if count == "0":
        raise GitOpError(f"nothing to approve: {name} has no commits that {base} lacks")
    original = _current(repo)
    if original is None:
        raise GitOpError("check out a branch first")
    if _dirty(repo):
        raise GitOpError("commit or stash your changes first")
    if original != base:
        run(repo, "switch", "-q", base)
    try:
        proc = run(repo, "merge", "--no-ff", "-m", f"Merge {name} into {base}", name, ok=(0, 1))
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
                f"{name} does not merge cleanly into {base} and git merge --abort failed: {err}"
            ) from err
        raise GitOpError(
            f"{name} does not merge cleanly into {base}: conflict in {', '.join(files)}; "
            "nothing was changed"
        )
    except GitOpError:
        if original != base:
            try:
                run(repo, "switch", "-q", original)
            except GitOpError:
                pass
        raise
