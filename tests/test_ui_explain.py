"""Clicking a failed desk: a plain-English Haiku summary of the error, asked for once per error
text. The model call, its fallback and the cache; Office.explain_error; the POST route."""

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from git_helpers import init_repo
from test_ui_office_server import Site, wait_for

from harness.models import Usage
from harness.planner import PlannerTurn
from harness.ui.explain import SUMMARY_MAX_CHARS, ErrorExplainer, explain_error, haiku_explain
from harness.ui.office import Office, OfficeConflict

EXPLAIN = "/api/orchestrator/explain"


def _fake_query(text: str, calls: list | None = None):
    async def run(*, prompt, options):
        if calls is not None:
            calls.append(prompt)
        yield AssistantMessage(content=[TextBlock(text=text)], model="m")

    return run


async def _boom(*, prompt, options):
    raise RuntimeError("no network")
    yield  # pragma: no cover


def test_haiku_explain_reads_the_reply(tmp_path):
    got = asyncio.run(haiku_explain("raw", tmp_path, _fake_query("It ran out\nof budget.")))
    assert got == "It ran out of budget."


def test_explain_falls_back_to_the_raw_text(tmp_path):
    assert explain_error(tmp_path, "disk  full", query_fn=_boom) == "disk full"
    assert explain_error(tmp_path, "disk full", use_model=False) == "disk full"
    long = explain_error(tmp_path, "x " * 2000, use_model=False)
    assert len(long) == SUMMARY_MAX_CHARS and long.endswith("…")


def test_the_explainer_asks_once_per_error(tmp_path):
    calls: list = []
    ex = ErrorExplainer(tmp_path, query_fn=_fake_query("Plain words.", calls))
    assert ex.explain("boom") == "Plain words."
    assert ex.explain("boom") == "Plain words."
    ex.explain("other")
    assert calls == ["boom", "other"]


class FailingSession:
    async def send(self, text):
        return PlannerTurn(error="boom", usage=Usage(cost_usd=0.01))

    async def close(self):
        pass


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    office = Office(
        repo,
        fake=True,
        fake_delay=0,
        orchestrator_session_factory=lambda r, p: FailingSession(),
        error_explainer=lambda e: f"Plain: {e}",
    )
    s = Site(repo, office)
    yield s
    s.stop()


def test_explain_before_anything_failed_is_a_conflict(site):
    code, _, body = site.post(EXPLAIN, {"who": "orchestrator"})
    assert code == 409 and body["error"] == "Nothing has failed there."
    code, _, body = site.post(EXPLAIN, {"who": "opus"})
    assert code == 409


def test_explain_a_failed_orchestrator(site):
    site.post("/api/orchestrator/message", {"text": "do it"})
    wait_for(lambda: site.get_json("/api/office")["orchestrator"]["state"] == "failed")
    code, _, body = site.post(EXPLAIN, {"who": "orchestrator"})
    assert code == 200
    assert body == {
        "error": "orchestrator session failed: boom",
        "summary": "Plain: orchestrator session failed: boom",
    }


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"{}", "send exactly: who"),
        (b'{"who": "opus", "x": 1}', "send exactly: who"),
        (b'{"who": "worker"}', "who must be orchestrator or opus"),
        (b"[1]", "JSON object"),
    ],
)
def test_explain_rejects_bad_bodies(site, raw, message):
    code, _, body = site.post(EXPLAIN, raw=raw)
    assert code == 400 and message in body["error"]


def test_office_explain_error_without_a_failure(tmp_path):
    office = Office(init_repo(tmp_path / "r"), fake=True, error_explainer=lambda e: e)
    with pytest.raises(OfficeConflict, match="Nothing has failed"):
        office.explain_error("opus")
