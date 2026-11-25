import asyncio
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from harness.config import MODEL_IDS
from harness.planner_log import PlannerLog
from harness.titles import (
    TITLE_SYSTEM_PROMPT,
    TITLES_DIR,
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


def _assistant(text: str, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


def _result(result: str | None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        result=result,
    )


def _fake(*messages, seen: dict | None = None):
    async def run(*, prompt, options):
        if seen is not None:
            seen["prompt"] = prompt
            seen["options"] = options
        for m in messages:
            yield m

    return run


def test_clean_title_edge_cases() -> None:
    assert clean_title("x" * 100) == "x" * 59 + "…"
    assert len(clean_title("x" * 100)) == 60
    assert clean_title("Title:   Hello world!?") == "Hello world"
    assert clean_title("TITLE: `Add CI`") == "Add CI"
    assert clean_title("\n\n  Second line wins\nthird") == "Second line wins"
    assert clean_title("...") == ""


def test_fallback_title_edge_cases() -> None:
    assert fallback_title("  \n  ") == ""
    assert fallback_title("a b c d e f g") == "a b c d e f g"
    assert fallback_title("Fix the bug.") == "Fix the bug"


def test_write_read_titles(tmp_path: Path) -> None:
    write_title(tmp_path, "w", "  padded  \n")
    assert (tmp_path / TITLES_DIR / "w.txt").read_text(encoding="utf-8") == "padded\n"
    (tmp_path / TITLES_DIR / "blank.txt").write_text("  \n", encoding="utf-8")
    assert read_title(tmp_path, "blank") is None
    assert read_titles(tmp_path, ["w", "missing"]) == {"w": "padded"}
    assert read_titles(tmp_path, []) == {}


def test_request_text_blank_user_falls_back_to_description(tmp_path: Path) -> None:
    log = PlannerLog(tmp_path, "planner-1")
    log.emit("planner_started", {"description": "  Desc text  "})
    log.emit("planner_user", {"text": "   "})
    log.emit("run_launched", {"run_id": "run-a"})
    assert request_text(tmp_path, "run-a") == "Desc text"


def test_request_text_is_truncated(tmp_path: Path) -> None:
    log = PlannerLog(tmp_path, "planner-1")
    log.emit("planner_user", {"text": "y" * 3000})
    log.emit("run_launched", {"run_id": "run-a"})
    assert request_text(tmp_path, "run-a") == "y" * 2000


def test_request_text_later_session_wins(tmp_path: Path) -> None:
    for session, text in (("planner-1", "first"), ("planner-2", "second")):
        log = PlannerLog(tmp_path, session)
        log.emit("planner_user", {"text": text})
        log.emit("run_launched", {"run_id": "run-a"})
    assert request_text(tmp_path, "run-a") == "second"


def test_request_text_without_log(tmp_path: Path) -> None:
    assert request_text(tmp_path, "run-a") is None


def test_haiku_title_result_wins(tmp_path: Path) -> None:
    fake = _fake(_assistant("Assistant one"), _result("Final pick."))
    assert asyncio.run(haiku_title("req", tmp_path, fake)) == "Final pick"


def test_haiku_title_ignores_subagent(tmp_path: Path) -> None:
    fake = _fake(_assistant("Sub text", "tu1"), _assistant("Main text"), _result(None))
    assert asyncio.run(haiku_title("req", tmp_path, fake)) == "Main text"
    only_sub = _fake(_assistant("Sub text", "tu1"))
    assert asyncio.run(haiku_title("req", tmp_path, only_sub)) == ""


def test_haiku_title_options(tmp_path: Path) -> None:
    seen: dict = {}
    asyncio.run(haiku_title("the text", tmp_path, _fake(_assistant("T"), seen=seen)))
    options = seen["options"]
    assert options.model == MODEL_IDS["haiku"]
    assert options.max_turns == 1
    assert options.tools == []
    assert options.system_prompt == TITLE_SYSTEM_PROMPT
    assert options.cwd == str(tmp_path.resolve())
    assert seen["prompt"] == "the text"


def test_ensure_request_title_long_text_and_empty_result(tmp_path: Path) -> None:
    got = ensure_request_title(tmp_path, "orch-9", "z" * 3000, query_fn=_fake(_result("")))
    assert got == "z" * 59 + "…"
    assert read_title(tmp_path, "orch-9") == got


def test_ensure_request_title_model_reply_cleans_to_empty(tmp_path: Path) -> None:
    got = ensure_request_title(tmp_path, "orch-1", "Hi", query_fn=_fake(_assistant("...")))
    assert got == "Hi"


def test_ensure_never_raises_on_bad_repo(tmp_path: Path) -> None:
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    assert ensure_title(f, "r") is None
    assert ensure_request_title(f, "orch-1", "Fix it", use_model=False) is None
