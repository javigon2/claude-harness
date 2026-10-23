import os

import pytest

from harness.config import MODEL_IDS
from harness.models import AttemptRecord, TaskState
from harness.planner import SdkPlannerSession
from harness.state import RunState, run_dir, save_state
from harness.ui.chat import (
    CHAT_APPEND,
    CHAT_EVENTS,
    CHAT_MAX_TURNS,
    ChatConflict,
    ChatLog,
    SdkChatSession,
    chats_from_log,
    read_chat_events,
    task_brief,
)
from harness.ui.markdown import parse_markdown
from harness.worker import WORKER_TOOLS


def test_log_roundtrip(tmp_path):
    assert read_chat_events(tmp_path) == []
    ChatLog(tmp_path, "chat-1").emit("chat_started", {"tier": "haiku"})
    path = tmp_path / CHAT_EVENTS
    with path.open("a", encoding="utf-8") as f:
        f.write("\n{garbage\n")
    events = read_chat_events(tmp_path)
    assert len(events) == 1
    assert events[0].run_id == "chat-1"
    assert events[0].type == "chat_started"


def _start(tmp_path, cid, tier="haiku", pid=None):
    log = ChatLog(tmp_path, cid)
    log.emit("chat_started", {"tier": tier, "pid": os.getpid() if pid is None else pid})
    return log


def test_chats_from_log(tmp_path):
    a = _start(tmp_path, "a")
    a.emit("chat_user", {"text": "hi"})
    a.emit("chat_progress", {"tool": "Edit", "detail": "a.py"})
    a.emit("chat_progress", {"tool": "Bash"})
    a.emit("chat_commit", {"sha": "abc123", "subject": "chat haiku: x"})
    a.emit("chat_reply", {"text": "# done\n\nok", "cost_usd": 0.25})
    b = _start(tmp_path, "b")
    b.emit("chat_user", {"text": "go"})
    b.emit("chat_progress", {"tool": "Edit", "detail": "a.py"})
    c = _start(tmp_path, "c")
    c.emit("chat_closed")
    d = _start(tmp_path, "d")
    d.emit("chat_failed", {"reason": "boom", "cost_usd": 1.5})
    _start(tmp_path, "e", pid=999999999)
    _start(tmp_path, "f", tier="opus")
    views = {v.chat_id: v for v in chats_from_log(tmp_path)}
    assert [v.chat_id for v in chats_from_log(tmp_path)] == ["e", "d", "c", "b", "a"]
    va = views["a"]
    assert va.state == "waiting"
    assert [t.role for t in va.transcript] == ["user", "agent"]
    assert va.transcript[1].blocks == parse_markdown("# done\n\nok")
    assert va.commits == ["abc123 chat haiku: x"]
    assert va.cost_usd == 0.25
    assert va.progress == []
    assert views["b"].state == "working"
    assert views["b"].progress == ["Edit: a.py"]
    assert views["c"].state == "closed"
    assert views["d"].state == "failed"
    assert views["d"].error == "boom"
    assert views["d"].cost_usd == 1.5
    assert views["e"].state == "closed"


def _state(tmp_path, ts):
    save_state(
        run_dir(tmp_path, "r1"),
        RunState(run_id="r1", plan_path="plan.json", stage="s", tasks={"t1": ts}),
    )


def test_task_brief_no_plan(tmp_path):
    (tmp_path / "plan.json").write_text("{}", encoding="utf-8")
    _state(tmp_path, TaskState(task_id="t1", status="failed", error="tests failed"))
    assert task_brief(tmp_path, "r1", "t1") == (
        "You are picking up task t1 (unknown) from run r1; it ended failed.\n"
        "Goal: unknown\nFiles: unknown\nLast summary: none\nError: tests failed"
    )


def test_task_brief_with_plan(tmp_path):
    (tmp_path / "plan.json").write_text(
        '{"stage": "s", "orders": [{"id": "t1", "title": "T one", "goal": "Do one",'
        ' "tier": "haiku", "depends_on": [], "files_owned": ["a.py", "b.py"],'
        ' "files_readonly": [], "context": [], "steps": ["x"], "constraints": [],'
        ' "acceptance": ["true"], "budget": {"max_turns": 5, "max_attempts": 1},'
        ' "out_of_scope": []}]}',
        encoding="utf-8",
    )
    rec = AttemptRecord(attempt=1, tier="haiku", worker_status="done", passed=True, summary="did a")
    _state(tmp_path, TaskState(task_id="t1", status="done", history=[rec]))
    assert task_brief(tmp_path, "r1", "t1") == (
        "You are picking up task t1 (T one) from run r1; it ended done.\n"
        "Goal: Do one\nFiles: a.py, b.py\nLast summary: did a\nError: none"
    )


def test_task_brief_unknown(tmp_path):
    with pytest.raises(ChatConflict, match="unknown task t9 in run r1"):
        task_brief(tmp_path, "r1", "t9")
    _state(tmp_path, TaskState(task_id="t1"))
    with pytest.raises(ChatConflict, match="unknown task t9 in run r1"):
        task_brief(tmp_path, "r1", "t9")


def test_sdk_chat_session_options(tmp_path):
    s = SdkChatSession(tmp_path, "sonnet")
    assert isinstance(s, SdkPlannerSession)
    assert s.tier == "sonnet"
    o = s.options()
    assert o.model == MODEL_IDS["sonnet"]
    assert o.system_prompt["append"] == CHAT_APPEND
    assert o.tools == list(WORKER_TOOLS)
    assert o.allowed_tools == list(WORKER_TOOLS)
    assert o.setting_sources == ["project"]
    assert o.max_turns == CHAT_MAX_TURNS
    assert o.cwd == str(tmp_path.resolve())
