"""The whole Orchestrator loop over HTTP: a real interactive server and the demo sessions.
Route to a haiku chat, escalate to an Opus quick plan, check the costs, start a new request,
and see the costs survive a restart. No model is called."""

import pytest
from git_helpers import init_repo
from test_ui_office_server import Site, wait_for

from harness.planner_log import read_planner_events
from harness.ui.office import Office
from harness.worktree import ensure_excluded

MESSAGE = "/api/orchestrator/message"
NEW = "/api/orchestrator/new"


@pytest.fixture
def repo(tmp_path):
    r = init_repo(tmp_path / "repo")
    ensure_excluded(r)
    return r


def make_office(repo) -> Office:
    return Office(repo, fake=True, fake_delay=0, auto_advance=False)


def office(site: Site) -> dict:
    return site.get_json("/api/office")


def orch(site: Site) -> dict:
    return office(site)["orchestrator"]


def total_rule(view: dict) -> float:
    return (
        view["orchestrator"]["cost_usd"]
        + view["cost_usd"]
        + sum(c["cost_usd"] for c in view["chats"])
    )


def test_route_escalate_new_and_restart(repo):
    site = Site(repo, make_office(repo))
    try:
        # 1. route to haiku
        status, _, body = site.post(MESSAGE, {"text": "haiku fix the typo"})
        assert status == 200
        assert body["ok"] is True
        wait_for(
            lambda: (
                orch(site)["state"] == "idle"
                and orch(site)["holder"] == "haiku"
                and any(c["state"] == "waiting" for c in office(site)["chats"])
            ),
            10,
        )
        view = office(site)
        chat = view["chats"][0]
        assert chat["state"] == "waiting"
        assert view["orchestrator"]["holder_id"] == chat["chat_id"]
        assert "- haiku fix the typo" in (repo / "notes.md").read_text(encoding="utf-8")
        assert view["orchestrator"]["cost_usd"] == pytest.approx(0.01)
        assert chat["cost_usd"] == pytest.approx(0.002)
        assert view["total_cost_usd"] == pytest.approx(0.012)
        got = [(e["role"], e["who"], e["text"]) for e in view["orchestrator"]["transcript"]]
        assert got == [
            ("user", "You", "haiku fix the typo"),
            ("orchestrator", "Orchestrator", "Sent to haiku: demo routing: haiku"),
            ("agent", "haiku", "Done (haiku demo): noted it in notes.md."),
        ]

        # 2. escalate to an Opus quick plan
        status, _, _ = site.post(MESSAGE, {"text": "opus quick redo this across modules"})
        assert status == 200
        wait_for(lambda: office(site)["state"] == "ready" and orch(site)["state"] == "idle", 10)
        view = office(site)
        assert view["plan_kind"] == "quick"
        assert view["quick_plan"].startswith(".harness/plans/quick-")
        assert view["orchestrator"]["holder"] == "opus"
        assert view["orchestrator"]["holder_id"] == view["session_id"]
        assert [c["state"] for c in view["chats"] if c["chat_id"] == chat["chat_id"]] == ["closed"]
        events = read_planner_events(repo)
        users = [e for e in events if e.type == "planner_user"]
        assert any(
            e.payload["text"].startswith("You are taking over this work from haiku") for e in users
        )
        turns = [e for e in events if e.type == "orchestrator_turn"]
        assert [(e.payload["action"], e.payload["to"]) for e in turns] == [
            ("start", "haiku"),
            ("escalate", "opus"),
        ]
        assert all("cost_usd" in e.payload for e in turns)
        got = [(e["role"], e["who"], e["text"]) for e in view["orchestrator"]["transcript"]]
        assert ("user", "You", "opus quick redo this across modules") in got
        assert (
            "orchestrator",
            "Orchestrator",
            "Moved up to opus (quick plan): demo routing: opus",
        ) in got
        assert any(r == "agent" and w == "opus" and t.startswith("Quick plan: ") for r, w, t in got)

        # costs
        assert view["orchestrator"]["cost_usd"] == pytest.approx(0.02)
        assert view["orchestrator"]["cost_usd"] == pytest.approx(
            sum(e.payload["cost_usd"] for e in turns)
        )
        assert view["cost_usd"] == pytest.approx(0.01)
        assert view["total_cost_usd"] == pytest.approx(0.032)
        assert view["total_cost_usd"] == pytest.approx(total_rule(view))

        # 3. new request
        status, _, body = site.post(NEW, {})
        assert status == 200
        o = body["office"]["orchestrator"]
        assert o["thread_id"] is None
        assert o["transcript"] == []
        assert o["holder"] is None
        assert o["cost_usd"] == pytest.approx(0.02)
    finally:
        site.stop()

    # 4. restart
    site = Site(repo, make_office(repo))
    try:
        view = office(site)
        assert view["orchestrator"]["cost_usd"] == pytest.approx(0.02)
        assert view["orchestrator"]["thread_id"] is None
        assert view["total_cost_usd"] == pytest.approx(total_rule(view))
    finally:
        site.stop()

    site = Site(repo, None)
    try:
        view = office(site)
        assert view["mode"] == "read-only"
        assert view["orchestrator"]["cost_usd"] == pytest.approx(0.02)
        assert view["orchestrator"]["thread_id"] is None
        assert view["total_cost_usd"] == pytest.approx(total_rule(view))
    finally:
        site.stop()
