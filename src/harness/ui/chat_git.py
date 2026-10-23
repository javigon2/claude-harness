"""Git helpers for agent chats: snapshot the dirty files, find what one turn changed, and
commit only those paths on the current branch."""

import hashlib
import subprocess
from pathlib import Path

from harness.worktree import WorktreeError


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def _check(proc: subprocess.CompletedProcess[str]) -> str:
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def _digest(repo: Path, path: str) -> str | None:
    try:
        return hashlib.sha256(Path(repo, path).read_bytes()).hexdigest()
    except OSError:
        return None


def dirty_snapshot(repo: Path) -> dict[str, str | None]:
    """{path: sha256 hex digest of the file's bytes, or None when it cannot be read (deleted)}
    for every entry of `git status --porcelain=v1 -z --untracked-files=all` run in `repo`.
    Paths are repo-relative as git prints them. For a rename or copy entry (status letter R
    or C in the first column) the new path is kept and the original path field that follows
    it is skipped. Raises WorktreeError (git's stderr, else stdout, stripped) if git exits
    non-zero."""
    out = _check(_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    fields = out.split("\0")
    snapshot: dict[str, str | None] = {}
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            i += 1
        path = entry[3:]
        snapshot[path] = _digest(repo, path)
    return snapshot


def changed_paths(before: dict[str, str | None], after: dict[str, str | None]) -> list[str]:
    """The sorted keys of `after` that are missing from `before` or map to a different value
    there."""
    return sorted(p for p, v in after.items() if p not in before or before[p] != v)


def commit_paths(repo: Path, paths: list[str], message: str) -> str | None:
    """Commit exactly `paths` on the current branch: `git add -A -- <paths>`, then
    `git diff --cached --quiet -- <paths>` (exit 0: nothing staged, return None; exit 1: go
    on), then `git commit -m <message> -- <paths>`, then return the stripped stdout of
    `git rev-parse --short HEAD`. Returns None without running git when `paths` is empty.
    Any other non-zero git exit raises WorktreeError (stderr, else stdout, stripped)."""
    if not paths:
        return None
    _check(_git(repo, "add", "-A", "--", *paths))
    diff = _git(repo, "diff", "--cached", "--quiet", "--", *paths)
    if diff.returncode == 0:
        return None
    if diff.returncode != 1:
        _check(diff)
    _check(_git(repo, "commit", "-m", message, "--", *paths))
    return _check(_git(repo, "rev-parse", "--short", "HEAD")).strip()


def chat_commit_message(tier: str, text: str) -> str:
    """f"chat {tier}: {line}" where line is the first line of `text` that is not blank,
    stripped and cut to its first 60 characters; f"chat {tier}: changes" when every line is
    blank."""
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            return f"chat {tier}: {line[:60]}"
    return f"chat {tier}: changes"
