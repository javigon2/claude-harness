"""The Git view's local write actions on the open project: stage, unstage, commit, and
create, switch, merge and delete branches.

Every failure is a GitOpError whose message is a sentence for the user."""

from pathlib import Path

from harness.ui.git_cmd import check_path, out, require_repo, run
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
