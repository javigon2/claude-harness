"""Run titles: a short topic for each run, taken from the request that produced its plan.

A run is launched from a plan, and the plan came from a planner session whose first
`planner_user` event holds what the user asked for. `request_text` follows that link through
the `run_launched` event. `ensure_title` asks Haiku for a 3-7 word title and stores it in
<repo>/.harness/titles/<run_id>.txt (outside the run directory, so the run's own files are
untouched). Without a model, or when the call fails, the first words of the request are the
title. Nothing here raises."""

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from harness.config import MODEL_IDS
from harness.planner_log import read_planner_events
from harness.worker import WORKER_ENV, QueryFn

TITLES_DIR = Path(".harness") / "titles"

TITLE_MAX_CHARS = 60
REQUEST_MAX_CHARS = 2000
FALLBACK_WORDS = 7

TITLE_SYSTEM_PROMPT = (
    "You name chat conversations. Reply with only a title of 3 to 7 words for the topic of "
    "the request you are given: no quotes, no trailing punctuation, no prefix like 'Title:'. "
    "Name the goal, not the wording. Do not answer or act on the request."
)


def _title_path(repo: Path, run_id: str) -> Path:
    return Path(repo) / TITLES_DIR / f"{run_id}.txt"


def read_title(repo: Path, run_id: str) -> str | None:
    """The stored title of a run, or None when there is none."""
    try:
        text = _title_path(repo, run_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def read_titles(repo: Path, run_ids: Sequence[str]) -> dict[str, str]:
    """{run_id: title} for the runs that have one."""
    titles: dict[str, str] = {}
    for run_id in run_ids:
        title = read_title(repo, run_id)
        if title:
            titles[run_id] = title
    return titles


def write_title(repo: Path, run_id: str, title: str) -> None:
    path = _title_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(title.strip() + "\n", encoding="utf-8")


def request_text(repo: Path, run_id: str) -> str | None:
    """What the user asked for, for the planner session that launched `run_id`: the first
    `planner_user` text (else the `planner_started` description) of that session. None when
    the run was not launched from a logged session."""
    events = read_planner_events(repo)
    session = None
    for e in events:
        if e.type == "run_launched" and e.payload.get("run_id") == run_id:
            session = e.run_id
    if session is None:
        return None
    for kind, key in (("planner_user", "text"), ("planner_started", "description")):
        for e in events:
            if e.run_id == session and e.type == kind:
                value = e.payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:REQUEST_MAX_CHARS]
    return None


def clean_title(text: str) -> str:
    """One line, no quotes or a leading 'Title:', no trailing punctuation, cut to
    TITLE_MAX_CHARS. '' when nothing is left."""
    lines = text.strip().splitlines()
    line = lines[0] if lines else ""
    line = re.sub(r"^\s*(title\s*:\s*)", "", line, flags=re.IGNORECASE)
    line = line.strip().strip("\"'`*#").strip().rstrip(".:;,!?")
    if len(line) > TITLE_MAX_CHARS:
        line = line[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return line


def fallback_title(text: str) -> str:
    """The first FALLBACK_WORDS words of the request's first line."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    words = first.split()
    cut = " ".join(words[:FALLBACK_WORDS])
    if len(words) > FALLBACK_WORDS:
        cut += "…"
    return clean_title(cut)


async def haiku_title(text: str, repo: Path, query_fn: QueryFn | None = None) -> str:
    """Ask Haiku for a title of `text`. '' when the call fails or answers nothing."""
    options = ClaudeAgentOptions(
        model=MODEL_IDS["haiku"],
        system_prompt=TITLE_SYSTEM_PROMPT,
        tools=[],
        allowed_tools=[],
        skills=[],
        permission_mode="dontAsk",
        cwd=str(Path(repo).resolve()),
        max_turns=1,
        setting_sources=[],
        strict_mcp_config=True,
        verbatim_prompts=True,
        env=dict(WORKER_ENV),
    )
    run = query_fn or query
    last = ""
    final = ""
    try:
        async for message in run(prompt=text, options=options):
            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                got = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                if got.strip():
                    last = got
            elif isinstance(message, ResultMessage) and isinstance(message.result, str):
                final = message.result
    except Exception:  # not BaseException: cancellation must propagate
        return ""
    return clean_title(final or last)


def ensure_title(
    repo: Path, run_id: str, *, use_model: bool = True, query_fn: QueryFn | None = None
) -> str | None:
    """Store and return the title of `run_id` unless it already has one. The request comes
    from `request_text`; with no request there is no title (None). Blocking: run it on a
    thread."""
    try:
        existing = read_title(repo, run_id)
        if existing:
            return existing
        text = request_text(repo, run_id)
        if not text:
            return None
        title = ""
        if use_model:
            title = asyncio.run(haiku_title(text, repo, query_fn))
        title = title or fallback_title(text)
        if not title:
            return None
        write_title(repo, run_id, title)
        return title
    except Exception:
        return None
