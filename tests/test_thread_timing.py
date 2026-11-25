"""The Timing breakdown of an Orchestrator conversation, from synthetic logs with known
times, and its GET route."""

import json
import threading
from datetime import UTC, datetime, timedelta

from test_ui_server import _get

from harness.ui.server import make_server
from harness.ui.timing import build_thread_timing

T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
TH = "orch-20261123-120000-abcd"


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": (T0 + timedelta(seconds=s)).isoformat(), "run_id": rid, "task_id": None,
                    "type": typ, "payload": p})
        for s, rid, typ, p in rows
    ]  # fmt: skip
    path.write_text("\n".join(lines) + "\n")


def _logs(repo, old_chat=False):
    _write(repo / ".harness" / "planner" / "events.jsonl", [
        (0, TH, "orchestrator_started", {"pid": 1}),
        (0, TH, "orchestrator_user", {"text": "fix the typo"}),
        (2, TH, "orchestrator_turn", {"action": "start", "to": "haiku", "target_kind": "chat",
            "target_id": "c1", "routing_ms": 1800, "routing_connect_ms": 700,
            "routing_api_ms": 900, "dispatch_ms": 20}),
        (40, TH, "orchestrator_user", {"text": "now plan the big one"}),
        (41, TH, "orchestrator_turn", {"action": "escalate", "to": "opus", "target_kind": "opus",
            "target_id": "planner-1", "routing_ms": 900}),
        (41.5, "planner-1", "planner_message", {"phase": "project"}),
        (44, "planner-1", "planner_tool_finished", {"tool": "Read", "duration_ms": 500}),
        (51.5, "planner-1", "planner_reply", {"duration_ms": 10000, "connect_ms": 2000,
            "api_ms": 6000}),
        (53.5, "planner-1", "planner_message", {"phase": "staging"}),
        (56.5, "planner-1", "planner_reply", {"duration_ms": 3000, "api_ms": 3000}),
    ])  # fmt: skip
    reply = (
        {"text": "done"}
        if old_chat
        else {
            "text": "done",
            "duration_ms": 8000,
            "connect_ms": 1500,
            "api_ms": 4000,
            "git_ms": 600,
        }
    )
    _write(repo / ".harness" / "chats" / "events.jsonl", [
        (0.5, "c1", "chat_started", {"tier": "haiku"}),
        (0.5, "c1", "chat_user", {"text": "fix the typo"}),
        (5, "c1", "chat_tool", {"tool": "Edit", "duration_ms": 1000}),
        (11, "c1", "chat_reply", reply),
    ])  # fmt: skip


def test_breakdown_per_message_and_totals(tmp_path):
    _logs(tmp_path)
    t = build_thread_timing(tmp_path, TH)
    assert t is not None and len(t.exchanges) == 2
    first, second = t.exchanges
    assert first.to == "haiku" and first.turns == 1 and first.response_ms == 11000
    assert first.ms == {
        "routing": 1800,
        "handoff": 600,
        "startup": 1500,
        "model": 4000,
        "tools": 1000,
        "agent_other": 1500,
        "harness": 600,
    }
    assert first.routing_detail == {"routing_connect": 700, "routing_api": 900, "dispatch": 20}
    assert second.action == "escalate" and second.to == "opus" and second.turns == 2
    assert second.response_ms == 16500
    assert second.ms == {
        "routing": 900,
        "handoff": 600,
        "startup": 2000,
        "model": 9000,
        "tools": 500,
        "agent_other": 1500,
        "harness": 2000,
    }
    assert t.totals["model"] == 13000
    assert [(x.tool, x.count, x.total_ms) for x in t.tools] == [("Edit", 1, 1000), ("Read", 1, 500)]
    assert t.wall_ms == 56500
    assert build_thread_timing(tmp_path, "orch-nope") is None  # fmt: skip


def test_an_old_log_counts_unsplit_time_and_says_so(tmp_path):
    _logs(tmp_path, old_chat=True)
    first = build_thread_timing(tmp_path, TH).exchanges[0]
    assert first.ms["model"] == 0 and first.ms["tools"] == 1000
    assert first.ms["agent_other"] == 10500 - 1000 and first.ms["handoff"] == 0
    assert any("older log" in n for n in build_thread_timing(tmp_path, TH).notes)


def test_timing_route(tmp_path):
    _logs(tmp_path)
    srv = make_server(tmp_path, 0, poll_interval=0.05, heartbeat_s=0.2)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, _, body = _get(base + "/api/orchestrator/timing/" + TH)
        assert code == 200 and json.loads(body)["totals"]["routing"] == 2700
        assert _get(base + "/api/orchestrator/timing/orch-nope")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(5)
