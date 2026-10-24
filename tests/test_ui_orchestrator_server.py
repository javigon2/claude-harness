"""The Orchestrator POST endpoints of the interactive office server: the same safety checks as
the planner routes, plus the Orchestrator schema. Runs against the demo routing session only."""

import asyncio
import json
import threading

import pytest
from git_helpers import init_repo
from test_ui_office_server import Site, wait_for

from harness.models import Usage
from harness.planner import PlannerTurn
from harness.planner_log import read_planner_events
from harness.ui.office import Office

MESSAGE = "/api/orchestrator/message"
NEW = "/api/orchestrator/new"


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    s = Site(repo, Office(repo, fake=True, fake_delay=0))
    yield s
    s.stop()


def orch(site: Site) -> dict:
    return site.get_json("/api/office")["orchestrator"]


def orch_events(site: Site) -> list:
    return [e for e in read_planner_events(site.repo) if e.run_id.startswith("orch-")]


class GatedSession:
    """A routing session whose one turn waits for `gate` (at most 5 s)."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate

    async def send(self, text):
        await asyncio.to_thread(self.gate.wait, 5.0)
        body = json.dumps({"to": "haiku", "plan": "project", "reason": "small"})
        return PlannerTurn(text=f"```json\n{body}\n```", usage=Usage(cost_usd=0.02))

    async def close(self):
        pass


def test_message_then_new_over_http(site):
    code, _, body = site.post(MESSAGE, {"text": "haiku fix the typo"})
    assert code == 200
    assert body["ok"] is True
    assert body["office"]["mode"] == "interactive"
    assert body["office"]["orchestrator"]["thread_id"].startswith("orch-")
    wait_for(lambda: orch(site)["state"] == "idle" and orch(site)["holder"] == "haiku")
    assert orch(site)["cost_usd"] > 0

    code, _, body = site.post(NEW, {})
    assert code == 200
    assert body["ok"] is True
    assert body["office"]["orchestrator"]["thread_id"] is None
    assert body["office"]["orchestrator"]["transcript"] == []
    assert orch_events(site)[-1].type == "orchestrator_closed"


@pytest.mark.parametrize(
    "path, raw, message",
    [
        (MESSAGE, b"{}", "send exactly: text"),
        (MESSAGE, b'{"text": "x", "extra": 1}', "send exactly: text"),
        (MESSAGE, b'{"text": ""}', "non-empty string"),
        (MESSAGE, b'{"text": "   "}', "non-empty string"),
        (MESSAGE, b'{"text": 5}', "non-empty string"),
        (MESSAGE, json.dumps({"text": "x" * 20_001}).encode(), "at most"),
        (MESSAGE, b"[1]", "JSON object"),
        (MESSAGE, b"not json", "bad JSON"),
        (NEW, b'{"x": 1}', "this endpoint takes no fields"),
        (NEW, b"null", "JSON object"),
    ],
)
def test_bad_bodies_are_400(site, path, raw, message):
    code, _, body = site.post(path, raw=raw)
    assert code == 400
    assert message in body["error"]
    assert orch_events(site) == []


def test_conflicts_while_routing_are_409(tmp_path):
    repo = init_repo(tmp_path / "repo")
    gate = threading.Event()
    office = Office(
        repo,
        fake=True,
        fake_delay=0,
        orchestrator_session_factory=lambda r, p: GatedSession(gate),
    )
    s = Site(repo, office)
    try:
        assert s.post(MESSAGE, {"text": "one"})[0] == 200
        code, _, body = s.post(MESSAGE, {"text": "two"})
        assert code == 409
        assert body == {"error": "the orchestrator is still routing the last message"}
        code, _, body = s.post(NEW, {})
        assert code == 409
        assert body == {"error": "the orchestrator is still routing the last message"}
        gate.set()
        wait_for(lambda: orch(s)["state"] == "idle")
    finally:
        gate.set()
        s.stop()


def test_token_origin_and_unknown_routes(site):
    code, _, body = site.post(MESSAGE, {"text": "hi"}, token=None)
    assert code == 403 and body["error"] == "bad token"
    code, _, body = site.post(MESSAGE, {"text": "hi"}, origin="http://evil.example")
    assert code == 403 and body["error"] == "bad origin"
    assert site.post("/api/orchestrator/nope", {}, origin=None)[0] == 404
    assert site.post("/api/orchestrator/", {})[0] == 404
    assert site.post("/api/orchestrator", {})[0] == 404
    assert orch_events(site) == []


def test_office_view_carries_the_orchestrator(site, tmp_path):
    view = site.get_json("/api/office")
    assert view["orchestrator"]["state"] == "idle"
    assert view["orchestrator"]["thread_id"] is None
    assert view["total_cost_usd"] == 0.0
    ro = Site(site.repo, None)
    try:
        view = ro.get_json("/api/office")
        assert view["mode"] == "read-only"
        assert view["orchestrator"]["state"] == "idle"
        assert view["total_cost_usd"] == 0.0
        status, hdrs, out = ro.request(
            "POST",
            MESSAGE,
            b'{"text": "x"}',
            {"Origin": ro.origin, "X-Harness-Token": "x", "Content-Type": "application/json"},
        )
        assert status == 405
        assert json.loads(out) == {"error": "read-only"}
    finally:
        ro.stop()
