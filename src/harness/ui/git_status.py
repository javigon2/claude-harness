"""Read the working-tree status of a project."""

from pathlib import Path

from harness.ui.git_cmd import out, require_repo, run
from harness.ui.git_models import StatusEntry, WorkStatus

CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def read_status(repo: Path) -> WorkStatus:
    """Every changed, staged, untracked or conflicted path except those under .harness/.
    Raises NoRepoError outside a repo."""
    require_repo(repo)
    text = out(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = text.split("\0")
    entries: list[StatusEntry] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y = entry[0], entry[1]
        path = entry[3:]
        old_path: str | None = None
        if x in "RC":
            if i < len(fields):
                old_path = fields[i]
            i += 1
        if path == ".harness" or path.startswith(".harness/"):
            continue
        code = x + y
        entries.append(
            StatusEntry(
                path=path,
                old_path=old_path,
                staged=x,
                unstaged=y,
                untracked=code == "??",
                conflicted=code in CONFLICT_CODES,
            )
        )

    symbolic = run(repo, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
    if symbolic.returncode == 1:
        branch: str | None = None
        detached = True
    else:
        branch = symbolic.stdout.strip()
        detached = False

    merge_head = run(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", ok=(0, 1))
    merging = merge_head.returncode == 0

    return WorkStatus(
        branch=branch,
        detached=detached,
        merging=merging,
        entries=entries,
    )
