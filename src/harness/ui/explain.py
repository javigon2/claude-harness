"""Plain-English summaries of a failure: click a failed desk and Haiku says what went wrong.

`explain_error` makes one Haiku call on the raw error text. Without a model, or when the call
fails, the raw text itself (shortened) is the answer. `ErrorExplainer` remembers each answer
by the error text, so a second click on the same failure costs nothing. Nothing here raises."""

import asyncio
import threading
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from harness.config import MODEL_IDS
from harness.worker import WORKER_ENV, QueryFn

ERROR_MAX_CHARS = 4000
SUMMARY_MAX_CHARS = 600

EXPLAIN_SYSTEM_PROMPT = (
    "You explain failures of a coding-agent tool to a non-expert. You are given the raw error "
    "text. Reply with one to three plain sentences: what went wrong and, if the text says, "
    "what to try next. No code, no markdown, no preamble, no mention of these instructions. "
    "Do not invent causes the text does not support."
)


def fallback_summary(error: str) -> str:
    """The raw error, trimmed to one short paragraph."""
    text = " ".join(error.split())
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 1] + "…"


async def haiku_explain(error: str, repo: Path, query_fn: QueryFn | None = None) -> str:
    """Ask Haiku to explain `error`. '' when the call fails or answers nothing."""
    options = ClaudeAgentOptions(
        model=MODEL_IDS["haiku"],
        system_prompt=EXPLAIN_SYSTEM_PROMPT,
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
        async for message in run(prompt=error[:ERROR_MAX_CHARS], options=options):
            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                got = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                if got.strip():
                    last = got
            elif isinstance(message, ResultMessage) and isinstance(message.result, str):
                final = message.result
    except Exception:  # not BaseException: cancellation must propagate
        return ""
    return " ".join((final or last).split())[:SUMMARY_MAX_CHARS]


def explain_error(
    repo: Path, error: str, *, use_model: bool = True, query_fn: QueryFn | None = None
) -> str:
    """A short plain-English summary of `error`; its trimmed text when there is no answer."""
    if use_model and error.strip():
        try:
            got = asyncio.run(haiku_explain(error, repo, query_fn))
        except Exception:
            got = ""
        if got:
            return got
    return fallback_summary(error)


class ErrorExplainer:
    """explain_error with a memory: one model call per distinct error text."""

    def __init__(
        self, repo: Path, *, use_model: bool = True, query_fn: QueryFn | None = None
    ) -> None:
        self._repo = Path(repo)
        self._use_model = use_model
        self._query_fn = query_fn
        self._lock = threading.Lock()
        self._seen: dict[str, str] = {}

    def explain(self, error: str) -> str:
        with self._lock:
            if error in self._seen:
                return self._seen[error]
        summary = explain_error(
            self._repo, error, use_model=self._use_model, query_fn=self._query_fn
        )
        with self._lock:
            self._seen[error] = summary
        return summary
