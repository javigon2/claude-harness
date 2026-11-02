"""Pre-run environment checks behind `harness doctor`."""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.planner import HARNESS_ROOT, PlannerError, check_skills


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    fix: str


def _git(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def _tool(name: str, fix: str) -> Check:
    return Check(f"{name} on PATH", shutil.which(name) is not None, fix)


def _is_repo(repo: Path) -> bool:
    if shutil.which("git") is None or not repo.is_dir():
        return False
    return _git("rev-parse", "--git-dir", repo=repo).returncode == 0


def _clean_tree(repo: Path) -> bool:
    proc = _git("status", "--porcelain", repo=repo)
    return proc.returncode == 0 and not proc.stdout.strip()


def _harness_excluded(repo: Path) -> bool:
    """True when git ignores .harness/ (info/exclude, .gitignore or a global ignore)."""
    return _git("check-ignore", "-q", ".harness/x", repo=repo).returncode == 0


def run_checks(repo: Path, skills_root: Path = HARNESS_ROOT) -> list[Check]:
    """Run every check against `repo`; never raises."""
    repo = Path(repo)
    checks = [
        _tool("git", "install git and make sure it is on your PATH"),
        _tool("uv", "install uv: https://docs.astral.sh/uv/"),
    ]
    is_repo = _is_repo(repo)
    checks.append(Check("repo is a git repo", is_repo, f"run `git init` in {repo}"))
    checks.append(
        Check(
            "working tree is clean",
            is_repo and _clean_tree(repo),
            "commit or stash your changes (`git stash -u`)",
        )
    )
    try:
        check_skills(skills_root)
        skills_ok = True
    except PlannerError:
        skills_ok = False
    checks.append(
        Check(
            "planner skills exist",
            skills_ok,
            f"restore plan-project and plan-stage under {skills_root / '.claude' / 'skills'}",
        )
    )
    checks.append(
        Check(
            ".harness/ is git-excluded",
            is_repo and _harness_excluded(repo),
            "add `.harness/` to .git/info/exclude",
        )
    )
    return checks


def render(checks: list[Check]) -> str:
    """One line per check: `OK   <name>` or `FAIL <name> -- fix: <fix>`."""
    return "\n".join(f"OK   {c.name}" if c.ok else f"FAIL {c.name} -- fix: {c.fix}" for c in checks)
