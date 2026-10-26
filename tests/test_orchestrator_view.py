import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from harness.models import Event
from harness.planner_log import PLANNER_EVENTS
from harness.ui.chat import CHAT_EVENTS
from harness.ui.orchestrator import (
    OrchEntry,
    OrchestratorView,
    handoff_brief,
    new_thread_id,
    orchestrator_view_from_log,
    rank,
    routing_line,
    tail_text,
)

T0 = datetime(2026, 10, 23, 10, 0, 0, tzinfo=UTC)
TH = "orch-20261023-100000"
ME = os.getpid()


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for sec, run_id, type_, payload in rows:
            e = Event(ts=T0 + timedelta(seconds=sec), run_id=run_id, type=type_, payload=payload)
            f.write(e.model_dump_json() + "\n")


def planner(tmp_path, rows):
    write(tmp_path / PLANNER_EVENTS, rows)


def chat(tmp_path, rows):
    write(tmp_path / CHAT_EVENTS, rows)


def turn(to="haiku", kind="chat", tid="chat-1", cost=0.5, reason="small", text="Sent to haiku"):
    return {
        "action": "start",
        "to": to,
        "plan": "quick",
        "reason": reason,
        "target_kind": kind,
        "target_id": tid,
        "cost_usd": cost,
        "text": text,
    }


def dead_pid():
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    return int(done.stdout)


def test_no_log(tmp_path):
    assert orchestrator_view_from_log(tmp_path) == OrchestratorView()


def test_cost_across_threads(tmp_path):
    planner(
        tmp_path,
        [
            (0, "orch-a", "orchestrator_started", {"pid": ME}),
            (1, "orch-a", "orchestrator_turn", turn(cost=0.25)),
            (2, "orch-a", "orchestrator_closed", {"reason": "new request"}),
            (3, TH, "orchestrator_started", {"pid": ME}),
            (4, TH, "orchestrator_failed", {"reason": "boom", "cost_usd": 0.5}),
            (5, TH, "orchestrator_turn", {"text": "x", "cost_usd": True}),
        ],
    )
    v = orchestrator_view_from_log(tmp_path)
    assert v.cost_usd == 0.75
    assert v.thread_id == TH


def test_closed_thread(tmp_path):
    planner(
        tmp_path,
        [
            (0, TH, "orchestrator_started", {"pid": ME}),
            (1, TH, "orchestrator_user", {"text": "hi"}),
            (2, TH, "orchestrator_turn", turn(cost=0.5)),
            (3, TH, "orchestrator_closed", {"reason": "new request"}),
        ],
    )
    v = orchestrator_view_from_log(tmp_path)
    assert v.thread_id is None
    assert v.transcript == []
    assert v.cost_usd == 0.5


def test_merged_order_and_holder(tmp_path):
    planner(
        tmp_path,
        [
            (0, TH, "orchestrator_started", {"pid": ME}),
            (10, TH, "orchestrator_user", {"text": "fix it"}),
            (12, TH, "orchestrator_turn", turn(text="Sent to haiku: small")),
        ],
    )
    chat(
        tmp_path,
        [
            (1, "chat-1", "chat_started", {"tier": "haiku", "pid": ME}),
            (5, "chat-1", "chat_reply", {"text": "old reply"}),
            (11, "chat-1", "chat_reply", {"text": "early reply"}),
            (20, "chat-1", "chat_reply", {"text": "late reply"}),
        ],
    )
    v = orchestrator_view_from_log(tmp_path)
    assert [(e.who, e.text) for e in v.transcript] == [
        ("You", "fix it"),
        ("Orchestrator", "Sent to haiku: small"),
        ("haiku", "early reply"),
        ("haiku", "late reply"),
    ]
    assert v.reason == "small"
    assert v.holder == "haiku"
    assert v.holder_id == "chat-1"
    assert v.state == "idle"
    assert v.transcript[2].blocks


def test_holder_none_after_chat_closed(tmp_path):
    planner(
        tmp_path,
        [
            (0, TH, "orchestrator_started", {"pid": ME}),
            (1, TH, "orchestrator_user", {"text": "go"}),
            (2, TH, "orchestrator_turn", turn()),
        ],
    )
    chat(
        tmp_path,
        [
            (0, "chat-1", "chat_started", {"tier": "haiku", "pid": ME}),
            (3, "chat-1", "chat_closed", {}),
        ],
    )
    v = orchestrator_view_from_log(tmp_path)
    assert v.holder is None and v.holder_id is None


def test_opus_target(tmp_path):
    planner(
        tmp_path,
        [
            (0, TH, "orchestrator_started", {"pid": ME}),
            (1, TH, "orchestrator_user", {"text": "plan it"}),
            (2, "planner-1", "planner_started", {}),
            (3, TH, "orchestrator_turn", turn("opus", "opus", "planner-1")),
            (4, "planner-1", "planner_reply", {"text": "questions?"}),
        ],
    )
    v = orchestrator_view_from_log(tmp_path)
    assert [e.who for e in v.transcript] == ["You", "Orchestrator", "opus"]
    assert v.holder == "opus" and v.holder_id == "planner-1"
    planner(tmp_path, [(5, "planner-1", "planner_done", {})])
    v = orchestrator_view_from_log(tmp_path)
    assert v.holder is None and v.holder_id is None
    assert len(v.transcript) == 3


def test_state_routing_idle_failed(tmp_path):
    rows = [
        (0, TH, "orchestrator_started", {"pid": ME}),
        (1, TH, "orchestrator_user", {"text": "hi"}),
    ]
    planner(tmp_path, rows)
    assert orchestrator_view_from_log(tmp_path).state == "routing"

    d = tmp_path / "dead"
    planner(
        d,
        [
            (0, TH, "orchestrator_started", {"pid": dead_pid()}),
            (1, TH, "orchestrator_user", {"text": "hi"}),
        ],
    )
    assert orchestrator_view_from_log(d).state == "idle"

    f = tmp_path / "failed"
    planner(
        f,
        [
            (0, TH, "orchestrator_started", {"pid": ME}),
            (1, TH, "orchestrator_user", {"text": "hi"}),
            (2, TH, "orchestrator_failed", {"reason": "nope", "cost_usd": 0.1}),
        ],
    )
    v = orchestrator_view_from_log(f)
    assert v.state == "failed"
    assert v.error == "nope"
    assert v.transcript[-1].text == "nope"


def test_reason_is_last_turn(tmp_path):
    planner(
        tmp_path,
        [
            (0, TH, "orchestrator_started", {"pid": ME}),
            (1, TH, "orchestrator_user", {"text": "a"}),
            (2, TH, "orchestrator_turn", turn(reason="first")),
            (3, TH, "orchestrator_user", {"text": "b"}),
            (4, TH, "orchestrator_turn", turn(reason="second")),
        ],
    )
    assert orchestrator_view_from_log(tmp_path).reason == "second"


def test_helpers():
    assert [rank("haiku"), rank("sonnet"), rank("opus")] == [0, 1, 2]
    assert new_thread_id().startswith("orch-")
    assert tail_text([]) == "(none)"
    entries = [
        OrchEntry(role="user", who="You", ts="t", text=f"m{i}  a\n b") for i in range(12)
    ] + [OrchEntry(role="agent", who="haiku", ts="t", text="x" * 600)]
    lines = tail_text(entries).split("\n")
    assert len(lines) == 12
    assert lines[0] == "You: m1 a b"
    assert lines[-1] == "haiku: " + "x" * 500
    brief = handoff_brief(entries[:1], "go", "haiku").split("\n")
    assert brief == [
        "You are taking over this work from haiku in the harness office.",
        "Conversation so far:",
        "You: m0 a b",
        "",
        "The user now says:",
        "go",
    ]


def test_routing_line():
    assert routing_line("start", "opus", "quick", "big") == "Sent to opus (quick plan): big"
    assert routing_line("start", "sonnet", "quick", "r") == "Sent to sonnet: r"
    assert routing_line("escalate", "opus", "project", "r") == "Moved up to opus (project plan): r"
    assert routing_line("escalate", "sonnet", "quick", "r") == "Moved up to sonnet: r"
    assert routing_line("forward", "opus", "project", "x") == "Passed to opus."
    assert routing_line("forward", "sonnet", "quick", "x") == "Passed to sonnet."
