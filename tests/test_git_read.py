import subprocess
from pathlib import Path

import pytest
from git_helpers import init_repo

from harness.ui.git_cmd import NoRepoError
from harness.ui.git_read import read_branches, read_graph


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, msg: str) -> None:
    (repo / name).write_text(msg, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "o.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    r = init_repo(tmp_path / "p")
    git(r, "remote", "add", "origin", str(origin))
    git(r, "push", "-q", "-u", "origin", "main")
    git(r, "switch", "-q", "-c", "work/x")
    commit(r, "w1.txt", "w1")
    git(r, "branch", "harness/r1/t1")
    git(r, "switch", "-q", "main")
    commit(r, "b.txt", "b")
    git(r, "merge", "-q", "--no-ff", "work/x", "-m", "m")
    git(r, "tag", "v1")
    return r


def by_subject(page):
    return {c.subject: c for c in page.commits}


def test_graph_subjects_and_refs(repo):
    page = read_graph(repo)
    assert [c.subject for c in page.commits] == ["m", "w1", "b", "init"]
    s = by_subject(page)
    assert s["m"].refs == ["HEAD -> main", "tag: v1"]
    assert s["w1"].refs == ["work/x"]
    assert s["init"].refs == ["origin/main"]
    assert len(s["m"].parents) == 2
    assert page.more is False
    assert page.head == git(repo, "rev-parse", "HEAD")


def test_graph_shows_two_lanes_when_branches_diverge(tmp_path):
    repo = init_repo(tmp_path / "diverged")
    git(repo, "switch", "-q", "-c", "scratch/graph-test")
    commit(repo, "branch.txt", "branch commit")
    git(repo, "switch", "-q", "main")
    commit(repo, "main.txt", "main commit")

    page = read_graph(repo)
    tips = by_subject(page)
    assert page.lanes == 2
    assert tips["main commit"].refs == ["HEAD -> main"]
    assert tips["branch commit"].refs == ["scratch/graph-test"]
    assert tips["main commit"].parents == tips["branch commit"].parents


def test_graph_harness_flag(repo):
    s = by_subject(read_graph(repo, harness=True))
    assert s["w1"].refs == ["work/x", "harness/r1/t1"]


def test_graph_limit(repo):
    page = read_graph(repo, limit=2)
    assert len(page.commits) == 2
    assert page.more is True


def test_graph_empty_repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    page = read_graph(tmp_path)
    assert page.commits == []
    assert page.head is None


def test_graph_no_repo(tmp_path):
    with pytest.raises(NoRepoError):
        read_graph(tmp_path)
    with pytest.raises(NoRepoError):
        read_branches(tmp_path)


def test_branches(repo):
    bl = read_branches(repo)
    assert bl.current == "main"
    assert bl.detached is False
    assert bl.remotes == ["origin"]
    first = bl.branches[0]
    assert first.name == "main" and first.current
    assert first.upstream == "origin/main"
    assert first.ahead == 3
    kinds = {b.name: b.kind for b in bl.branches}
    assert kinds["origin/main"] == "remote"
    assert "harness/r1/t1" not in kinds
    assert "harness/r1/t1" in {b.name for b in read_branches(repo, harness=True).branches}


def test_branch_base(repo):
    git(repo, "config", "branch.work/x.harnessbase", "main")
    expect = git(repo, "rev-list", "--left-right", "--count", "main...work/x").split()
    w = next(b for b in read_branches(repo).branches if b.name == "work/x")
    assert w.work is True
    assert w.base == "main"
    assert (w.base_behind, w.base_ahead) == (int(expect[0]), int(expect[1]))


def test_detached(repo):
    git(repo, "switch", "-q", "--detach", "HEAD~1")
    bl = read_branches(repo)
    assert bl.current is None
    assert bl.detached is True
