"""Read the commit graph and the branches of a project."""

import re
from pathlib import Path

from harness.ui.git_cmd import HARNESS_PREFIX, GitOpError, out, require_repo, run
from harness.ui.git_graph import layout
from harness.ui.git_models import BranchInfo, BranchList, GraphCommit, GraphPage

FIELD = "\x1f"
RECORD = "\x1e"
LOG_FORMAT = "%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s%x1e"
BRANCH_FORMAT = (
    "%(refname)%1f%(objectname)%1f%(upstream:short)%1f%(upstream:track)"
    "%1f%(committerdate:unix)%1f%(subject)"
)


def _refs(raw: str, harness: bool) -> list[str]:
    items = []
    for item in raw.split(", "):
        if not item:
            continue
        name = item.removeprefix("HEAD -> ").removeprefix("tag: ")
        if item != "HEAD" and name.endswith("/HEAD"):
            continue
        if not harness and name.startswith(HARNESS_PREFIX):
            continue
        items.append(item)
    return items


def read_graph(repo: Path, limit: int = 500, harness: bool = False) -> GraphPage:
    """The newest `limit` commits reachable from local branches, remote-tracking branches,
    tags and HEAD, in topo order, laid out by git_graph.layout. `harness/*` branches are
    left out unless `harness`. Raises NoRepoError outside a repo."""
    require_repo(repo)
    proc = run(repo, "rev-parse", "--verify", "-q", "HEAD", ok=(0, 1))
    head = proc.stdout.strip() or None
    if head is None:
        return GraphPage(commits=[], lanes=0, limit=limit, more=False, head=None)
    excl = [] if harness else ["--exclude=harness/*"]
    text = out(
        repo,
        "log",
        "--no-color",
        "--topo-order",
        f"--max-count={limit + 1}",
        f"--format={LOG_FORMAT}",
        *excl,
        "--branches",
        *excl,
        "--remotes",
        "--tags",
        "HEAD",
        "--",
    )
    records = [r.strip("\n") for r in text.split(RECORD)]
    records = [r for r in records if r]
    more = len(records) > limit
    records = records[:limit]
    parsed = []
    for rec in records:
        sha, parents, author, email, at, refs, subject = rec.split(FIELD, 6)
        parsed.append((sha, parents.split(), author, email, int(at), refs, subject))
    placed, lanes = layout([(p[0], p[1]) for p in parsed])
    commits = [
        GraphCommit(
            sha=sha,
            short=sha[:7],
            parents=parents,
            author=author,
            email=email,
            time=at,
            subject=subject,
            refs=_refs(refs, harness),
            lane=lane,
            edges=edges,
        )
        for (sha, parents, author, email, at, refs, subject), (lane, edges) in zip(
            parsed, placed, strict=True
        )
    ]
    return GraphPage(commits=commits, lanes=lanes, limit=limit, more=more, head=head)


def read_branches(repo: Path, harness: bool = False) -> BranchList:
    """Local and remote-tracking branches with ahead/behind against their upstream and,
    for branches with a recorded harnessbase, against that base. Raises NoRepoError
    outside a repo."""
    require_repo(repo)
    proc = run(repo, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
    if proc.returncode == 1:
        current, detached = None, True
    else:
        current, detached = proc.stdout.strip(), False
    text = out(repo, "for-each-ref", f"--format={BRANCH_FORMAT}", "refs/heads", "refs/remotes")

    bases: dict[str, str] = {}
    cfg = run(repo, "config", "--get-regexp", r"^branch\..*\.harnessbase$", ok=(0, 1))
    for line in cfg.stdout.splitlines():
        key, _, value = line.partition(" ")
        key = key.removeprefix("branch.").removesuffix(".harnessbase")
        if value:
            bases[key] = value.strip()

    infos: list[BranchInfo] = []
    for line in text.splitlines():
        parts = line.split(FIELD)
        if len(parts) < 6:
            continue
        ref, sha, upstream, track, when, subject = parts[:6]
        if ref.startswith("refs/heads/"):
            kind, name = "local", ref.removeprefix("refs/heads/")
            if not harness and name.startswith(HARNESS_PREFIX):
                continue
        elif ref.startswith("refs/remotes/"):
            kind, name = "remote", ref.removeprefix("refs/remotes/")
            if name.endswith("/HEAD"):
                continue
        else:
            continue
        a = re.search(r"ahead (\d+)", track)
        b = re.search(r"behind (\d+)", track)
        base = bases.get(name) if kind == "local" else None
        base_ahead = base_behind = 0
        if base is not None:
            try:
                counts = run(
                    repo, "rev-list", "--left-right", "--count", "--end-of-options",
                    f"{base}...{name}",
                ).stdout.split()
                base_behind, base_ahead = int(counts[0]), int(counts[1])
            except (GitOpError, ValueError, IndexError):
                base_ahead = base_behind = 0
        infos.append(
            BranchInfo(
                name=name,
                kind=kind,
                sha=sha,
                subject=subject,
                time=int(when),
                current=kind == "local" and name == current,
                upstream=upstream or None,
                ahead=int(a.group(1)) if a else 0,
                behind=int(b.group(1)) if b else 0,
                base=base,
                base_ahead=base_ahead,
                base_behind=base_behind,
                work=kind == "local" and name.startswith("work/"),
            )
        )

    def rank(i: BranchInfo) -> tuple[int, int, str]:
        group = 0 if i.current else (1 if i.kind == "local" else 2)
        return (group, -i.time, i.name)

    infos.sort(key=rank)
    remotes = out(repo, "remote").split()
    return BranchList(current=current, detached=detached, branches=infos, remotes=remotes)
