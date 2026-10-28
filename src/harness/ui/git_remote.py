"""Fetch from and push to the open project's remotes. Never forced.

Every failure is a GitOpError whose message is a sentence for the user."""

import subprocess
from pathlib import Path

from harness.ui.git_cmd import NETWORK_TIMEOUT_S, check_branch_name, out, require_repo, run
from harness.ui.gitops import GitOpError


def _check_remote(repo: Path, remote: str) -> str:
    """`remote` if it is one of the lines `git remote` prints; else GitOpError
    `unknown remote: <remote>`."""
    if remote not in out(repo, "remote").splitlines():
        raise GitOpError(f"unknown remote: {remote}")
    return remote


def _last_line(proc: subprocess.CompletedProcess[str], default: str) -> str:
    """The last line containing " -> " in git's stdout then stderr (stripped); else the last
    line equal to "Everything up-to-date"; else `default`."""
    lines = [ln.strip() for ln in (proc.stdout + "\n" + proc.stderr).splitlines()]
    lines = [ln for ln in lines if ln]
    arrows = [ln for ln in lines if " -> " in ln]
    if arrows:
        return arrows[-1]
    if "Everything up-to-date" in lines:
        return "Everything up-to-date"
    return default


def fetch(repo: Path, remote: str) -> str:
    """`git fetch --prune <remote>`; returns _last_line(proc, f"{remote} is up to date")."""
    require_repo(repo)
    _check_remote(repo, remote)
    proc = run(repo, "fetch", "--prune", remote, timeout=NETWORK_TIMEOUT_S)
    return _last_line(proc, f"{remote} is up to date")


def push(repo: Path, remote: str, branch: str) -> str:
    """`git push --set-upstream <remote> refs/heads/<branch>:refs/heads/<branch>`; returns
    _last_line(proc, "Everything up-to-date")."""
    require_repo(repo)
    _check_remote(repo, remote)
    check_branch_name(branch)
    ref = run(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", ok=(0, 1))
    if ref.returncode == 1:
        raise GitOpError(f"no local branch named {branch}")
    proc = run(
        repo,
        "push",
        "--set-upstream",
        remote,
        f"refs/heads/{branch}:refs/heads/{branch}",
        ok=(0, 1),
        timeout=NETWORK_TIMEOUT_S,
    )
    if proc.returncode == 1:
        if "[rejected]" in proc.stderr:
            raise GitOpError(
                f"{remote} has commits that {branch} lacks; fetch and merge {remote}/{branch} first"
            )
        raise GitOpError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return _last_line(proc, "Everything up-to-date")
