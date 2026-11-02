"""A cached, cheap summary of a repo for the planner's order prompts.

Cached in <repo>/.harness/planner/repo_digest.json, keyed on git HEAD plus a hash of
`git status --porcelain`, so it is rebuilt only when the commit or the working tree changes."""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

DIGEST_FILE = Path(".harness") / "planner" / "repo_digest.json"
MAX_TREE_LINES = 200

_MANIFESTS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")


@dataclass(frozen=True)
class Digest:
    text: str
    head: str
    reused: bool


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _head_lines(path: Path, n: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:n]
    except OSError:
        return None
    return "\n".join(lines) if lines else None


def _build(repo: Path) -> str:
    sections: list[str] = []
    files = _git(repo, "ls-files")
    if files and files.strip():
        names = sorted(files.splitlines())
        shown = names[:MAX_TREE_LINES]
        if len(names) > MAX_TREE_LINES:
            shown.append(f"... {len(names) - MAX_TREE_LINES} more files")
        sections.append("## Tree\n" + "\n".join(shown))
    for name in _MANIFESTS:
        body = _head_lines(repo / name, 60)
        if body is not None:
            sections.append(f"## Manifest\n{body}")
            break
    for name in ("README.md", "README"):
        body = _head_lines(repo / name, 40)
        if body is not None:
            sections.append(f"## README\n{body}")
            break
    log = _git(repo, "log", "--oneline", "-10")
    if log and log.strip():
        sections.append("## Recent commits\n" + log.strip())
    return "\n\n".join(sections)


def load_or_build(repo: Path) -> Digest:
    """The digest of `repo`: the cached one when git HEAD and the working-tree state both
    match, else a fresh one (cached, a failed write is ignored)."""
    repo = Path(repo)
    head = (_git(repo, "rev-parse", "HEAD") or "").strip() or "no-commit"
    status = [
        line
        for line in (_git(repo, "status", "--porcelain") or "").splitlines()
        if not line[3:].startswith(".harness")  # our own cache must not invalidate itself
    ]
    state = hashlib.sha256("\n".join(status).encode()).hexdigest()
    cache = repo / DIGEST_FILE
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and data.get("head") == head
            and data.get("state") == state
            and isinstance(data.get("text"), str)
        ):
            return Digest(text=data["text"], head=head, reused=True)
    except (OSError, ValueError):
        pass
    text = _build(repo)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"head": head, "state": state, "text": text}) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    return Digest(text=text, head=head, reused=False)
