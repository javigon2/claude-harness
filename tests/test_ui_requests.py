"""Requests: the dropdown entries rebuilt from the planner and chat logs, the titles of the
runs they launched, and the read-only past-thread route."""

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ui_fixtures import write_blocked_run, write_happy_run

from harness.models import Event
from harness.planner_log import PLANNER_EVENTS
from harness.titles import write_title
from harness.ui.chat import CHAT_EVENTS
from harness.ui.orchestrator import orchestrator_view_from_log
from harness.ui.requests import list_requests, request_thread, run_titles
from harness.ui.server import make_server

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
DEAD = 2**22 + 12345  # no such process
ALIVE = os.getpid()


def _put(repo: Path, rel: Path, rows: list[tuple[str, str, dict, int]]) -> None:
    """Append events (run_id, type, payload, minutes after T0) to a log, then a damaged line."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        for run_id, type_, payload, minutes in rows:
            event = Event(
                ts=T0 + timedelta(minutes=minutes), run_id=run_id, type=type_, payload=payload
            )
            out.write(event.model_dump_json() + "\n")
        out.write('{"ts": "2026-10-26T10:00:00Z", "run_id": "orch-bad", "type": "orchestrator_us\n')
        out.write("not json at all\n")


def turn(to: str, kind: str, target: str, cost: float) -> dict:
    return {
        "text": f"Sent to {to}",
        "to": to,
        "reason": "r",
        "target_kind": kind,
        "target_id": target,
        "cost_usd": cost,
    }


def write_logs(repo: Path) -> None:
    write_happy_run(repo)  # finished, every task done
    write_blocked_run(repo)  # finished with a blocked task
    planner: list[tuple[str, str, dict, int]] = [
        # A: a closed thread that went to a haiku chat
        ("orch-A", "orchestrator_started", {"pid": DEAD}, 0),
        ("orch-A", "orchestrator_user", {"text": "fix the login redirect bug"}, 1),
        ("orch-A", "orchestrator_turn", turn("haiku", "chat", "chat-1", 0.01), 2),
        ("orch-A", "orchestrator_closed", {"reason": "new request"}, 20),
        # B: thread to Opus; its plan launched two runs (one resumed: logged twice)
        ("orch-B", "orchestrator_started", {"pid": DEAD}, 60),
        ("orch-B", "orchestrator_user", {"text": "build the dashboard page"}, 61),
        ("orch-B", "orchestrator_turn", turn("opus", "opus", "planner-B", 0.02), 62),
        ("planner-B", "planner_started", {"pid": ALIVE, "description": "build it"}, 62),
        ("planner-B", "planner_user", {"text": "build the dashboard page"}, 62),
        ("planner-B", "planner_reply", {"text": "Here is a plan", "cost_usd": 0.5}, 63),
        ("planner-B", "plan_ready", {"path": "p", "cost_usd": 0.5}, 64),
        ("planner-B", "planner_done", {"plan_path": "p", "cost_usd": 0.9}, 65),
        ("planner-B", "run_launched", {"run_id": "happy1"}, 66),
        ("planner-B", "run_launched", {"run_id": "blocked1"}, 67),
        ("planner-B", "run_launched", {"run_id": "blocked1", "resume": True}, 68),
        # C: a plan started straight from the composer, waiting for the user
        ("planner-C", "planner_started", {"pid": ALIVE, "description": "plan the migration"}, 120),
        ("planner-C", "planner_user", {"text": "plan the migration"}, 120),
        ("planner-C", "planner_reply", {"text": "Which database?", "cost_usd": 0.3}, 121),
        # D: the live thread, routing right now
        ("orch-D", "orchestrator_started", {"pid": ALIVE}, 180),
        ("orch-D", "orchestrator_user", {"text": "what does the router do?"}, 181),
        # E: a failed direct plan
        ("planner-E", "planner_started", {"pid": ALIVE, "description": "plan E"}, 90),
        ("planner-E", "planner_failed", {"reason": "boom", "cost_usd": 0.1}, 91),
        # not requests: a thread without a message, a line with a bad payload
        ("orch-F", "orchestrator_started", {"pid": DEAD}, 200),
        ("orch-G", "orchestrator_user", {"text": 5}, 201),
    ]  # fmt: skip
    chat: list[tuple[str, str, dict, int]] = [
        ("chat-1", "chat_started", {"tier": "haiku", "pid": DEAD}, 2),
        ("chat-1", "chat_user", {"text": "fix the login redirect bug"}, 2),
        ("chat-1", "chat_reply", {"text": "done", "cost_usd": 0.05}, 3),
        ("chat-1", "chat_closed", {"reason": "closed by the user"}, 4),
    ]
    _put(repo, PLANNER_EVENTS, planner)
    _put(repo, CHAT_EVENTS, chat)


@pytest.fixture
def repo(tmp_path):
    write_logs(tmp_path)
    return tmp_path


def by_id(repo: Path) -> dict:
    return {r.id: r for r in list_requests(repo)}


def test_requests_are_listed_newest_first_one_per_request(repo):
    got = list_requests(repo)
    assert [r.id for r in got] == ["orch-D", "planner-C", "planner-E", "orch-B", "orch-A"]
    assert got[0].first_message == "what does the router do?"
    assert got[0].started_at.startswith("2026-10-26T13:01")


def test_a_thread_to_a_chat(repo):
    a = by_id(repo)["orch-A"]
    assert (a.kind, a.state, a.runs) == ("chat", "done", [])
    assert a.first_message == "fix the login redirect bug"
    assert a.title == "fix the login redirect bug"  # no stored title: the first words
    assert a.cost_usd == pytest.approx(0.06)  # the routing turn plus the chat


def test_a_thread_to_a_plan_with_several_runs(repo):
    b = by_id(repo)["orch-B"]
    assert b.kind == "plan"
    assert b.runs == ["happy1", "blocked1"]  # oldest first, the resumed launch once
    assert b.state == "attention"  # the latest run stopped with a blocked task
    assert b.cost_usd > 0.92  # routing + planning + the runs' own cost
    assert "planner-B" not in by_id(repo)  # its session is part of the thread


def test_direct_plan_sessions_and_live_states(repo):
    got = by_id(repo)
    assert (got["planner-C"].kind, got["planner-C"].state) == ("plan", "waiting")
    assert got["planner-C"].first_message == "plan the migration"
    assert got["planner-C"].cost_usd == pytest.approx(0.3)
    assert got["planner-E"].state == "attention"
    assert got["orch-D"].state == "running"


def test_stored_titles_replace_the_first_words(repo):
    write_title(repo, "orch-A", "Fix the login redirect")
    assert by_id(repo)["orch-A"].title == "Fix the login redirect"


def test_damaged_and_missing_logs_never_raise(tmp_path):
    assert list_requests(tmp_path) == []
    (tmp_path / PLANNER_EVENTS).parent.mkdir(parents=True)
    (tmp_path / PLANNER_EVENTS).write_bytes(b'\xff\xfe{"x"\n{}\n')
    assert list_requests(tmp_path) == []


def test_runs_show_their_requests_title(repo):
    write_title(repo, "orch-B", "Build the dashboard")
    write_title(repo, "happy1", "Own title")
    write_title(repo, "other", "Kept")
    titles = run_titles(repo, ["happy1", "blocked1", "other"], list_requests(repo))
    assert titles == {
        "happy1": "Build the dashboard",
        "blocked1": "Build the dashboard",
        "other": "Kept",
    }


def test_a_past_thread_is_rebuilt_by_id_read_only(repo):
    assert orchestrator_view_from_log(repo).thread_id == "orch-F"  # the last thread started
    past = orchestrator_view_from_log(repo, "orch-A")
    assert past.thread_id == "orch-A"
    assert (past.state, past.holder, past.error) == ("idle", None, None)
    assert [(e.role, e.who) for e in past.transcript] == [
        ("user", "You"),
        ("orchestrator", "Orchestrator"),
        ("agent", "haiku"),
    ]
    assert past.cost_usd == pytest.approx(0.01)
    assert orchestrator_view_from_log(repo, "orch-nope").thread_id is None


def test_request_thread_for_threads_sessions_and_unknown_ids(repo):
    assert [e.who for e in request_thread(repo, "orch-B").transcript][:2] == ["You", "Orchestrator"]
    plan = request_thread(repo, "planner-C")
    assert [(e.role, e.who) for e in plan.transcript] == [("user", "You"), ("agent", "opus")]
    assert request_thread(repo, "planner-zzz") is None
    assert request_thread(repo, "orch-zzz") is None
    assert request_thread(repo, "../etc/passwd") is None


@pytest.fixture
def site(repo):
    srv = make_server(repo, 0, poll_interval=0.05)  # no office: read-only, no token
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join(5)


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_api_runs_keeps_its_keys_and_gains_requests(site, repo):
    write_title(repo, "orch-B", "Build the dashboard")
    code, body = get(site + "/api/runs")
    assert code == 200
    assert set(body) == {"runs", "latest", "titles", "requests"}
    assert body["runs"] == ["happy1", "blocked1"]
    assert body["titles"] == {"happy1": "Build the dashboard", "blocked1": "Build the dashboard"}
    assert [r["id"] for r in body["requests"]][-2:] == ["orch-B", "orch-A"]
    assert set(body["requests"][0]) == {
        "id", "title", "first_message", "started_at", "kind", "state", "cost_usd", "runs",
    }  # fmt: skip


def test_past_thread_route_works_read_only(site):
    code, body = get(site + "/api/orchestrator/thread/orch-A")
    assert code == 200
    assert body["thread_id"] == "orch-A"
    assert [e["who"] for e in body["transcript"]] == ["You", "Orchestrator", "haiku"]
    assert body["transcript"][2]["blocks"]  # rendered like the live thread
    code, body = get(site + "/api/orchestrator/thread/planner-C")
    assert code == 200 and len(body["transcript"]) == 2
    assert get(site + "/api/orchestrator/thread/orch-nope")[0] == 404
    req = urllib.request.Request(
        site + "/api/orchestrator/thread/orch-A", data=b"{}", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code in (403, 404, 405)
