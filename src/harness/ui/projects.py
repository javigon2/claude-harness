"""Choosing a project: list folders, describe a folder as a repo, remember recent projects.

Everything here is read-only except `remember`, which writes the recents file."""

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict

MAX_ENTRIES = 500
RECENT_MAX = 10


class ProjectError(Exception):
    """The folder cannot be used. The message is a sentence for the user; the server
    answers 400 with it."""


class DirEntry(BaseModel):
    """One subfolder in a listing."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str  # absolute
    is_git: bool  # <path>/.git exists


class DirListing(BaseModel):
    """The subfolders of `path`, case-insensitively sorted, at most MAX_ENTRIES."""

    model_config = ConfigDict(extra="forbid")

    path: str
    parent: str | None  # None at "/"
    entries: list[DirEntry]
    truncated: bool = False  # > MAX_ENTRIES


class RepoInfo(BaseModel):
    """What the page shows about the open folder."""

    model_config = ConfigDict(extra="forbid")

    path: str
    name: str
    is_git: bool
    has_commit: bool
    branch: str | None
    on_main: bool
    dirty: bool
    origin_path: str | None  # local_origin


def default_recent_path() -> Path:
    """~/.config/agent-orchestrator/recent.json"""
    return Path.home() / ".config" / "agent-orchestrator" / "recent.json"


def _absolute(path: Path) -> Path:
    """`path` made absolute and normalised ("..", "."), without resolving symlinks."""
    return Path(os.path.abspath(path))


def list_dir(path: Path, show_hidden: bool = False) -> DirListing:
    """The subfolders of `path` (symlinks to folders too), sorted case-insensitively; names
    starting with "." only when `show_hidden`. Raises ProjectError `not a folder: <path>` or
    `cannot read <path>: <reason>`."""
    here = _absolute(Path(path))
    if not here.is_dir():
        raise ProjectError(f"not a folder: {here}")
    entries: list[DirEntry] = []
    try:
        with os.scandir(here) as it:
            for item in it:
                if item.name.startswith(".") and not show_hidden:
                    continue
                try:
                    if not item.is_dir():
                        continue
                    is_git = os.path.exists(os.path.join(item.path, ".git"))
                except OSError:
                    continue
                entries.append(DirEntry(name=item.name, path=item.path, is_git=is_git))
    except OSError as err:
        raise ProjectError(f"cannot read {here}: {err.strerror or err}") from err
    entries.sort(key=lambda e: (e.name.lower(), e.name))
    parent = None if here.parent == here else str(here.parent)
    return DirListing(
        path=str(here),
        parent=parent,
        entries=entries[:MAX_ENTRIES],
        truncated=len(entries) > MAX_ENTRIES,
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """`git -C path <args>`, or None if git cannot run."""
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None


def local_origin(path: Path) -> Path | None:
    """The folder `origin` points to when it is one: a plain path (relative ones are taken
    from `path`) or a `file://` URL naming an existing folder. None otherwise."""
    here = _absolute(Path(path))
    proc = _git(here, "config", "--get", "remote.origin.url")
    if proc is None or proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    if not url:
        return None
    if url.startswith("file://"):
        url = unquote(urlsplit(url).path)
    elif "://" in url or (":" in url and not url.startswith(("/", "."))):
        return None  # a remote (https://, ssh://, host:path)
    target = _absolute(here / url)
    return target if target.is_dir() else None


def repo_info(path: Path) -> RepoInfo:
    """Describe the folder as a project. Never raises: whatever git cannot tell is the
    "no repo" value."""
    here = _absolute(Path(path))
    info = RepoInfo(
        path=str(here),
        name=here.name or str(here),
        is_git=False,
        has_commit=False,
        branch=None,
        on_main=False,
        dirty=False,
        origin_path=None,
    )
    try:
        if not here.is_dir() or not (here / ".git").exists():
            return info
        info.is_git = True
        head = _git(here, "rev-parse", "--verify", "-q", "HEAD")
        info.has_commit = head is not None and head.returncode == 0
        branch = _git(here, "symbolic-ref", "--short", "-q", "HEAD")
        if branch is not None and branch.returncode == 0 and branch.stdout.strip():
            info.branch = branch.stdout.strip()
        info.on_main = info.branch == "main"
        status = _git(here, "status", "--porcelain")
        info.dirty = status is not None and status.returncode == 0 and bool(status.stdout.strip())
        origin = local_origin(here)
        info.origin_path = str(origin) if origin is not None else None
    except OSError:
        pass
    return info


def load_recent(recent_path: Path) -> list[str]:
    """The remembered project folders, newest first, without the ones that no longer exist.
    A missing or unreadable file is []."""
    try:
        data = json.loads(Path(recent_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and Path(p).is_dir()][:RECENT_MAX]


def remember(recent_path: Path, project: Path) -> None:
    """Move `project` to the front of the recents file (made if missing), keeping RECENT_MAX.
    A file that cannot be written is ignored: recents are a convenience."""
    recent_path = Path(recent_path)
    entry = str(_absolute(Path(project)))
    kept = [p for p in load_recent(recent_path) if p != entry]
    try:
        recent_path.parent.mkdir(parents=True, exist_ok=True)
        recent_path.write_text(json.dumps([entry, *kept][:RECENT_MAX], indent=2) + "\n")
    except OSError:
        pass
