import json
import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo
from ui_fixtures import write_blocked_run

from harness.ui.demo import demo_stage_plan
from harness.ui.office import Office, OfficeConflict
from harness.ui.work_branch import base_of

STAGE1 = ".harness/plans/stage-1/plan.json"


class FakePopen:
    def __init__(self, argv, **kwargs) -> None:
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4242

    def poll(self):
        return None


class Launches:
    def __init__(self) -> None:
        self.made: list[FakePopen] = []

    def __call__(self, argv, **kwargs) -> FakePopen:
        p = FakePopen(argv, **kwargs)
        self.made.append(p)
        return p


def _write_plan(repo: Path, stage: int) -> str:
    d = repo / ".harness" / "plans" / f"stage-{stage}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(demo_stage_plan(stage)))
    return f".harness/plans/stage-{stage}/plan.json"


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture
def launches():
    return Launches()


@pytest.fixture
def make(launches):
    made: list[Office] = []

    def _make(repo: Path) -> Office:
        office = Office(repo, fake=True, fake_delay=0, launcher=launches)
        made.append(office)
        return office

    yield _make
    for o in made:
        o.close()


def test_launch_cuts_work_branch(tmp_path, make, launches):
    repo = init_repo(tmp_path / "repo")
    _write_plan(repo, 1)
    make(repo).launch_plan(STAGE1)
    argv = launches.made[0].argv
    rid = argv[argv.index("--run-id") + 1]
    assert git(repo, "branch", "--show-current") == f"work/{rid}"
    assert argv[-2:] == ["--branch", f"work/{rid}"]
    assert base_of(repo, f"work/{rid}") == "main"


def test_launch_keeps_checked_out_work_branch(tmp_path, make, launches):
    repo = init_repo(tmp_path / "repo")
    _write_plan(repo, 1)
    git(repo, "switch", "-c", "work/mine")
    make(repo).launch_plan(STAGE1)
    argv = launches.made[0].argv
    assert argv[-2:] == ["--branch", "work/mine"]
    assert git(repo, "branch", "--list", "work/*").replace("*", "").split() == ["work/mine"]


def test_detached_head_refuses(tmp_path, make, launches):
    repo = init_repo(tmp_path / "repo")
    _write_plan(repo, 1)
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(OfficeConflict) as exc:
        make(repo).launch_plan(STAGE1)
    assert str(exc.value) == "cannot start a work branch: check out a branch first"
    assert launches.made == []


def test_non_git_directory_has_no_branch(tmp_path, make, launches):
    repo = tmp_path / "plain"
    repo.mkdir()
    _write_plan(repo, 1)
    make(repo).launch_plan(STAGE1)
    assert "--branch" not in launches.made[0].argv


def test_resume_keeps_checked_out_branch(tmp_path, make, launches):
    repo = init_repo(tmp_path / "repo")
    git(repo, "switch", "-c", "work/x")
    write_blocked_run(repo)
    make(repo).resume_run()
    assert launches.made[0].argv[-3:] == ["--branch", "work/x", "--resume"]
    assert git(repo, "branch", "--show-current") == "work/x"


def test_run_argv_branch(tmp_path, make):
    office = make(init_repo(tmp_path / "repo"))
    assert office.run_argv("p.json", "r1", "work/r1")[-2:] == ["--branch", "work/r1"]
