"""Read one commit, compare two refs, and diff one working-tree file."""

from pathlib import Path

from harness.ui.git_cmd import cap, check_path, out, require_repo, resolve, run
from harness.ui.git_models import CommitDetail, CommitSummary, Compare, FileChange, FileDiff

_DIFF = ("diff", "--no-color", "--no-ext-diff", "-M")


def _count(text: str) -> int | None:
    return None if text == "-" else int(text)


def _files(repo: Path, *revs: str) -> list[FileChange]:
    """The files a diff between `revs` touches, with line counts."""
    names = out(repo, *_DIFF, "--name-status", "-z", *revs, "--").split("\0")
    nums = out(repo, *_DIFF, "--numstat", "-z", *revs, "--").split("\0")
    counts: dict[str, tuple[int | None, int | None]] = {}
    i = 0
    while i < len(nums):
        entry = nums[i]
        i += 1
        if not entry:
            continue
        parts = entry.split("\t", 2)
        if len(parts) < 3:
            continue
        if parts[2] == "":
            i += 1  # old path
            path = nums[i] if i < len(nums) else ""
            i += 1
        else:
            path = parts[2]
        counts[path] = (_count(parts[0]), _count(parts[1]))
    files: list[FileChange] = []
    i = 0
    while i < len(names):
        status = names[i]
        i += 1
        if not status:
            continue
        old: str | None = None
        if status[0] in "RC":
            old = names[i]
            i += 1
        path = names[i]
        i += 1
        added, deleted = counts.get(path, (None, None))
        files.append(
            FileChange(path=path, old_path=old, status=status[0], added=added, deleted=deleted)
        )
    return files


def read_commit(repo: Path, rev: str) -> CommitDetail:
    """The commit `rev` names, with files and patch against its first parent (a root commit:
    against the empty tree). Raises NoRepoError outside a repo, GitOpError for a bad rev."""
    require_repo(repo)
    sha = resolve(repo, rev)
    fmt = "--format=%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s%x1f%b"
    raw = out(repo, "show", "-s", "--no-color", fmt, sha)
    f = raw.split("\x1f", 7)
    f += [""] * (8 - len(f))
    parents = f[1].split()
    base = parents[0] if parents else out(repo, "hash-object", "-t", "tree", "/dev/null").strip()
    files = _files(repo, base, sha)
    patch, truncated = cap(out(repo, *_DIFF, base, sha, "--"))
    return CommitDetail(
        sha=f[0].strip(),
        short=f[0].strip()[:7],
        parents=parents,
        author=f[2],
        email=f[3],
        time=int(f[4]),
        subject=f[6],
        body=f[7].strip("\n"),
        refs=[r for r in f[5].split(", ") if r],
        files=files,
        patch=patch,
        truncated=truncated,
    )


def read_compare(repo: Path, base: str, head: str) -> Compare:
    """What `head` adds to `base` since they split. Raises NoRepoError outside a repo,
    GitOpError for a bad rev."""
    require_repo(repo)
    b = resolve(repo, base)
    h = resolve(repo, head)
    mb = run(repo, "merge-base", b, h, ok=(0, 1))
    merge_base = mb.stdout.strip() or None if mb.returncode == 0 else None
    counts = out(repo, "rev-list", "--left-right", "--count", f"{b}...{h}").split()
    behind, ahead = int(counts[0]), int(counts[1])
    log = out(
        repo,
        "log",
        "--no-color",
        "--max-count=200",
        "--format=%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%s%x1e",
        f"{b}..{h}",
        "--",
    )
    commits: list[CommitSummary] = []
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        p = rec.split("\x1f", 5)
        commits.append(
            CommitSummary(
                sha=p[0],
                short=p[0][:7],
                parents=p[1].split(),
                author=p[2],
                email=p[3],
                time=int(p[4]),
                subject=p[5],
            )
        )
    files: list[FileChange] = []
    patch, truncated = "", False
    if merge_base is not None:
        files = _files(repo, f"{b}...{h}")
        patch, truncated = cap(out(repo, *_DIFF, f"{b}...{h}", "--"))
    return Compare(
        base=base,
        head=head,
        merge_base=merge_base,
        ahead=ahead,
        behind=behind,
        commits=commits,
        files=files,
        patch=patch,
        truncated=truncated,
    )


def read_file_diff(repo: Path, path: str, staged: bool) -> FileDiff:
    """The staged (index vs HEAD) or unstaged (work tree vs index) diff of `path`; an
    untracked file diffs against /dev/null. Raises NoRepoError, or GitOpError
    `bad path: <path>`."""
    require_repo(repo)
    check_path(path)
    base = ("diff", "--no-color", "--no-ext-diff")
    if staged:
        text = out(repo, *base, "--cached", "--", path)
    elif not out(repo, "ls-files", "--", path) and (repo / path).is_file():
        text = run(repo, *base, "--no-index", "--", "/dev/null", path, ok=(0, 1)).stdout
    else:
        text = out(repo, *base, "--", path)
    binary = any(line.startswith("Binary files ") for line in text.splitlines())
    patch, truncated = cap(text)
    return FileDiff(path=path, staged=staged, patch=patch, truncated=truncated, binary=binary)
