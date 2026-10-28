"""The data the Git view reads: commits, the graph, branches, diffs and the working status.

Every model forbids extra fields; the routes answer with `model_dump(mode="json")`."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CommitSummary(BaseModel):
    """One commit as a list row."""

    model_config = ConfigDict(extra="forbid")

    sha: str
    short: str
    parents: list[str]
    author: str
    email: str
    time: int  # author time, unix seconds
    subject: str


class GraphCommit(CommitSummary):
    """A graph row: the commit, its ref names and where it is drawn."""

    refs: list[str]  # %D items, e.g. "HEAD -> main", "origin/main", "tag: v1"
    lane: int
    edges: list[tuple[int, int]]  # (lane in this row, lane in the next row)


class GraphPage(BaseModel):
    """The newest `limit` commits in topo order; `more` when older ones exist."""

    model_config = ConfigDict(extra="forbid")

    commits: list[GraphCommit]
    lanes: int
    limit: int
    more: bool
    head: str | None  # full sha of HEAD, None before the first commit


class BranchInfo(BaseModel):
    """One local or remote-tracking branch."""

    model_config = ConfigDict(extra="forbid")

    name: str  # "main", "origin/main"
    kind: Literal["local", "remote"]
    sha: str
    subject: str
    time: int  # committer time of the tip, unix seconds
    current: bool
    upstream: str | None
    ahead: int  # vs upstream, 0 when there is none
    behind: int
    base: str | None  # git config branch.<name>.harnessbase
    base_ahead: int  # vs base, 0 when there is none
    base_behind: int
    work: bool  # name starts with "work/"


class BranchList(BaseModel):
    """Every branch, the checked-out one first."""

    model_config = ConfigDict(extra="forbid")

    current: str | None
    detached: bool
    branches: list[BranchInfo]
    remotes: list[str]


class FileChange(BaseModel):
    """One file in a commit or a compare."""

    model_config = ConfigDict(extra="forbid")

    path: str
    old_path: str | None  # renames and copies
    status: str  # A M D R C T
    added: int | None  # None for binary files
    deleted: int | None


class CommitDetail(CommitSummary):
    """A commit with its message body, files and patch against its first parent."""

    body: str
    refs: list[str]
    files: list[FileChange]
    patch: str
    truncated: bool


class Compare(BaseModel):
    """What `head` adds to `base` since they split: `git diff base...head`."""

    model_config = ConfigDict(extra="forbid")

    base: str
    head: str
    merge_base: str | None
    ahead: int  # commits in head that base lacks
    behind: int  # commits in base that head lacks
    commits: list[CommitSummary]
    files: list[FileChange]
    patch: str
    truncated: bool


class StatusEntry(BaseModel):
    """One path of `git status --porcelain=v1`."""

    model_config = ConfigDict(extra="forbid")

    path: str
    old_path: str | None
    staged: str  # porcelain X letter, " " when nothing is staged
    unstaged: str  # porcelain Y letter, " " when the work tree matches the index
    untracked: bool
    conflicted: bool


class WorkStatus(BaseModel):
    """The working tree of the open project."""

    model_config = ConfigDict(extra="forbid")

    branch: str | None
    detached: bool
    merging: bool
    entries: list[StatusEntry]


class FileDiff(BaseModel):
    """The diff of one working-tree file, staged or not."""

    model_config = ConfigDict(extra="forbid")

    path: str
    staged: bool
    patch: str
    truncated: bool
    binary: bool
