"""Git worktrees for parallel tasks: create, commit, merge back, clean up."""

import shutil
import subprocess
from pathlib import Path
from typing import Literal

MergeResult = Literal["merged", "conflict"]


class WorktreeError(RuntimeError):
    """A git command failed in a way the caller must handle."""


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _check(proc: subprocess.CompletedProcess[str]) -> str:
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


class WorktreeManager:
    def __init__(self, repo: Path, run_id: str, integration_branch: str = "main") -> None:
        self.repo = Path(repo)
        self.run_id = run_id
        self.integration_branch = integration_branch

    def branch(self, task_id: str) -> str:
        """harness/<run_id>/<task_id>"""
        return f"harness/{self.run_id}/{task_id}"

    def path(self, task_id: str) -> Path:
        """<repo>/.harness/worktrees/<task_id>"""
        return self.repo / ".harness" / "worktrees" / task_id

    def _ensure_excluded(self) -> None:
        out = _check(_git("rev-parse", "--git-common-dir", cwd=self.repo)).strip()
        common = Path(out)
        if not common.is_absolute():
            common = self.repo / common
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if ".harness/" in existing.splitlines():
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + prefix + ".harness/\n")

    def create(self, task_id: str, base: str | None = None) -> Path:
        """git worktree add -b <branch> <path> <base or integration_branch>. Raise WorktreeError
        (with git's stderr) if git fails, e.g. the branch or path already exists. Return the path.
        """
        self._ensure_excluded()
        path = self.path(task_id)
        _check(
            _git(
                "worktree",
                "add",
                "-b",
                self.branch(task_id),
                str(path),
                base or self.integration_branch,
                cwd=self.repo,
            )
        )
        return path

    def commit(self, task_id: str, message: str) -> bool:
        """git add -A, then commit in the worktree. Return False (and commit nothing) if there
        are no staged changes (`git diff --cached --quiet` exits 0), True after committing."""
        wt = self.path(task_id)
        _check(_git("add", "-A", cwd=wt))
        diff = _git("diff", "--cached", "--quiet", cwd=wt)
        if diff.returncode == 0:
            return False
        if diff.returncode != 1:
            raise WorktreeError(diff.stderr.strip())
        _check(_git("commit", "-m", message, cwd=wt))
        return True

    def merge(self, task_id: str) -> MergeResult:
        """In the worktree: git rebase <integration_branch>. On failure: git rebase --abort and
        return "conflict". Then in <repo>: git merge --ff-only <branch>; raise WorktreeError if
        that fails. Return "merged"."""
        wt = self.path(task_id)
        status = _check(_git("status", "--porcelain", cwd=wt))
        if status.strip():
            raise WorktreeError(f"uncommitted changes in {task_id}")
        rebase = _git("rebase", self.integration_branch, cwd=wt)
        if rebase.returncode != 0:
            _git("rebase", "--abort", cwd=wt)
            return "conflict"
        _check(_git("merge", "--ff-only", self.branch(task_id), cwd=self.repo))
        return "merged"

    def discard(self, task_id: str) -> None:
        """Remove this task's worktree and branch if they exist, so the task can start over.
        Every git failure here is ignored (the worktree or branch may not exist)."""
        path = self.path(task_id)
        _git("worktree", "remove", "--force", str(path), cwd=self.repo)
        _git("branch", "-D", self.branch(task_id), cwd=self.repo)
        _git("worktree", "prune", cwd=self.repo)
        if path.exists():
            shutil.rmtree(path)

    def cleanup(self) -> None:
        """Remove this run's worktrees and branches and nothing of other runs: git worktree
        remove --force every worktree under <repo>/.harness/worktrees whose branch (from
        `git worktree list --porcelain`) is under refs/heads/harness/<run_id>/, delete every
        branch under refs/heads/harness/<run_id>/ (git branch -D), then git worktree prune."""
        root = (self.repo / ".harness" / "worktrees").resolve()
        prefix = f"refs/heads/harness/{self.run_id}/"
        listing = _check(_git("worktree", "list", "--porcelain", cwd=self.repo))
        for record in listing.split("\n\n"):
            path: str | None = None
            branch: str | None = None
            for line in record.splitlines():
                if line.startswith("worktree "):
                    path = line[len("worktree ") :]
                elif line.startswith("branch "):
                    branch = line[len("branch ") :]
            if path is None or branch is None or not branch.startswith(prefix):
                continue
            wt = Path(path).resolve()
            if root in wt.parents:
                _check(_git("worktree", "remove", "--force", str(wt), cwd=self.repo))
        refs = _check(
            _git(
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/harness/{self.run_id}/",
                cwd=self.repo,
            )
        )
        for name in refs.split():
            _check(_git("branch", "-D", name, cwd=self.repo))
        _check(_git("worktree", "prune", cwd=self.repo))
