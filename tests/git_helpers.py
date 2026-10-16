"""A git-inited temp repo for tests that run the harness against a real repo."""

import subprocess
from pathlib import Path


def init_repo(repo: Path) -> Path:
    """`git init -b main` in `repo` (made if missing) with one commit, so worktrees can branch."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("demo repo\n", encoding="utf-8")
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo
