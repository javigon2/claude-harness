import asyncio
from pathlib import Path

from claude_agent_sdk import AssistantMessage, TextBlock

from harness.planner_log import PlannerLog
from harness.titles import (
    clean_title,
    ensure_request_title,
    ensure_title,
    fallback_title,
    haiku_title,
    read_title,
    read_titles,
    request_text,
    write_title,
)


def _launched(repo: Path, session: str, run_id: str, text: str | None) -> None:
    log = PlannerLog(repo, session)
    if text is not None:
        log.emit("planner_user", {"text": text})
    log.emit("run_launched", {"run_id": run_id, "plan_path": "p", "pid": 1})


def test_clean_title():
    assert clean_title('  Title: "Fix the login redirect."\nmore') == "Fix the login redirect"
    assert clean_title("**Add a dashboard**") == "Add a dashboard"
    assert clean_title("") == ""
    assert len(clean_title("x " * 100)) <= 60


def test_fallback_title_takes_first_words():
    assert fallback_title("one two three four five six seven eight nine") == (
        "one two three four five six seven…"
    )
    assert fallback_title("short ask\nsecond line") == "short ask"
    assert fallback_title("") == ""


def test_request_text_follows_the_launch_event(tmp_path):
    _launched(tmp_path, "planner-1", "run-a", "Make the UI look like ServiceNow")
    _launched(tmp_path, "planner-2", "run-b", None)
    assert request_text(tmp_path, "run-a") == "Make the UI look like ServiceNow"
    assert request_text(tmp_path, "run-b") is None
    assert request_text(tmp_path, "run-zzz") is None


def test_ensure_title_uses_fallback_without_a_model(tmp_path):
    _launched(tmp_path, "planner-1", "run-a", "Add a dashboard with cost and latency totals")
    title = ensure_title(tmp_path, "run-a", use_model=False)
    assert title == "Add a dashboard with cost and latency…"
    assert read_title(tmp_path, "run-a") == title
    assert read_titles(tmp_path, ["run-a", "run-b"]) == {"run-a": title}


def test_ensure_title_keeps_an_existing_title(tmp_path):
    _launched(tmp_path, "planner-1", "run-a", "anything")
    write_title(tmp_path, "run-a", "Kept title")
    assert ensure_title(tmp_path, "run-a", use_model=False) == "Kept title"


def test_ensure_title_without_a_request_makes_no_title(tmp_path):
    assert ensure_title(tmp_path, "run-x", use_model=False) is None
    assert read_title(tmp_path, "run-x") is None


def _fake_query(text: str):
    async def run(*, prompt, options):
        yield AssistantMessage(content=[TextBlock(text=text)], model="m")

    return run


def test_haiku_title_reads_the_reply(tmp_path):
    got = asyncio.run(haiku_title("a request", tmp_path, _fake_query('Title: "Fix login."')))
    assert got == "Fix login"


def test_haiku_title_survives_a_failing_call(tmp_path):
    async def boom(*, prompt, options):
        raise RuntimeError("no network")
        yield  # pragma: no cover

    assert asyncio.run(haiku_title("a request", tmp_path, boom)) == ""


def test_ensure_title_with_model_falls_back_when_the_call_fails(tmp_path):
    async def boom(*, prompt, options):
        raise RuntimeError("no network")
        yield  # pragma: no cover

    _launched(tmp_path, "planner-1", "run-a", "Fix it")
    assert ensure_title(tmp_path, "run-a", query_fn=boom) == "Fix it"


def test_ensure_request_title_asks_the_injected_query_and_stores_it(tmp_path):
    got = ensure_request_title(
        tmp_path,
        "orch-1",
        "Please fix the login redirect",
        query_fn=_fake_query("Fix login redirect."),
    )
    assert got == "Fix login redirect"
    assert read_title(tmp_path, "orch-1") == "Fix login redirect"


def test_ensure_request_title_falls_back_without_a_model_or_on_failure(tmp_path):
    async def boom(*, prompt, options):
        raise RuntimeError("no network")
        yield  # pragma: no cover

    text = "one two three four five six seven eight"
    assert ensure_request_title(tmp_path, "orch-1", text, use_model=False) == (
        "one two three four five six seven…"
    )
    assert ensure_request_title(tmp_path, "planner-2", "Fix it", query_fn=boom) == "Fix it"


def test_ensure_request_title_keeps_an_existing_title_and_skips_empty_text(tmp_path):
    write_title(tmp_path, "orch-1", "Kept")
    never = _fake_query("Other")
    assert ensure_request_title(tmp_path, "orch-1", "x", query_fn=never) == "Kept"
    assert ensure_request_title(tmp_path, "orch-2", "   ", query_fn=never) is None
    assert read_title(tmp_path, "orch-2") is None
