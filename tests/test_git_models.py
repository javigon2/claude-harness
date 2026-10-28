import pydantic
import pytest

from harness.ui.git_models import (
    BranchInfo,
    CommitSummary,
    GraphCommit,
    WorkStatus,
)


def _graph_commit(**overrides) -> dict:
    data = {
        "sha": "a" * 40,
        "short": "aaaaaaa",
        "parents": [],
        "author": "T",
        "email": "t@example.com",
        "time": 1,
        "subject": "s",
        "refs": ["HEAD -> main"],
        "lane": 0,
        "edges": [(0, 0), (0, 1)],
    }
    data.update(overrides)
    return data


def test_graph_commit_dumps_edges_as_lists() -> None:
    commit = GraphCommit(**_graph_commit())
    dumped = commit.model_dump(mode="json")
    assert dumped["edges"] == [[0, 0], [0, 1]]


def test_commit_summary_rejects_extra_field() -> None:
    with pytest.raises(pydantic.ValidationError):
        CommitSummary(
            sha="a" * 40,
            short="aaaaaaa",
            parents=[],
            author="T",
            email="t@example.com",
            time=1,
            subject="s",
            x=1,
        )


def test_graph_commit_rejects_extra_field() -> None:
    with pytest.raises(pydantic.ValidationError):
        GraphCommit(**_graph_commit(x=1))


def test_branch_info_accepts_local_and_remote_rejects_tag() -> None:
    base = {
        "name": "main",
        "sha": "a" * 40,
        "subject": "s",
        "time": 1,
        "current": True,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "base": None,
        "base_ahead": 0,
        "base_behind": 0,
        "work": False,
    }
    assert BranchInfo(kind="local", **base).kind == "local"
    assert BranchInfo(kind="remote", **base).kind == "remote"
    with pytest.raises(pydantic.ValidationError):
        BranchInfo(kind="tag", **base)


def test_work_status_round_trips() -> None:
    status = WorkStatus(branch=None, detached=True, merging=False, entries=[])
    dump = status.model_dump()
    assert WorkStatus(**dump) == status
