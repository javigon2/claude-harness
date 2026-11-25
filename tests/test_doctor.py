import subprocess
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.doctor import Check, render, run_checks


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(".harness/\n")
    return repo


def make_skills(root: Path) -> Path:
    for name in ("plan-project", "plan-stage"):
        d = root / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("x")
    return root


def by_name(checks: list[Check]) -> dict[str, bool]:
    return {c.name: c.ok for c in checks}


def test_all_ok(tmp_path):
    repo = make_repo(tmp_path)
    skills = make_skills(tmp_path / "root")
    assert all(c.ok for c in run_checks(repo, skills))


def test_dirty_tree_and_missing_exclude_and_skills(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text("")
    (repo / "f.txt").write_text("x")
    res = by_name(run_checks(repo, tmp_path / "nowhere"))
    assert res["working tree is clean"] is False
    assert res[".harness/ is git-excluded"] is False
    assert res["planner skills exist"] is False
    assert res["repo is a git repo"] is True


def test_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = by_name(run_checks(plain, make_skills(tmp_path / "root")))
    assert res["repo is a git repo"] is False
    assert res["working tree is clean"] is False


def test_missing_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.doctor.shutil.which", lambda name: None)
    res = by_name(run_checks(make_repo(tmp_path), make_skills(tmp_path / "root")))
    assert res["git on PATH"] is False
    assert res["uv on PATH"] is False


def test_render():
    out = render([Check("a", True, "f1"), Check("b", False, "do it")])
    assert out.splitlines() == ["OK   a", "FAIL b -- fix: do it"]


def test_cli_exit_codes(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = CliRunner().invoke(app, ["doctor", "--repo", str(plain)])
    assert result.exit_code == 1
    assert "FAIL repo is a git repo" in result.output
