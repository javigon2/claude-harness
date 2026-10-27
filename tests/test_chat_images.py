from pathlib import Path

from git_helpers import init_repo

from harness.models import Usage
from harness.planner import PlannerTurn
from harness.ui.chat import ChatLog, ChatManager, chats_from_log, read_chat_events
from harness.ui.uploads import attachment_note, store_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def wait_for(check, timeout: float = 5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


class Scripted:
    def __init__(self, repo, progress):
        self.repo = repo
        self.progress = progress
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)
        n = len(self.sent)
        (Path(self.repo) / f"{n}.txt").write_text("x\n", encoding="utf-8")
        return PlannerTurn(text=f"did {n}", usage=Usage(cost_usd=0.01), error=None)

    async def close(self):
        pass


def make(repo, **kw):
    sessions: list[Scripted] = []

    def factory(r, tier, progress):
        s = Scripted(r, progress)
        sessions.append(s)
        return s

    return ChatManager(repo, session_factory=factory, **kw), sessions


def view(mgr, chat_id):
    return next(v for v in mgr.views() if v.chat_id == chat_id)


def user_payloads(repo, cid):
    return [e.payload for e in read_chat_events(repo) if e.type == "chat_user"]


def test_images_flow(tmp_path):
    repo = init_repo(tmp_path / "r")
    a = store_image(repo, PNG)
    b = store_image(repo, JPEG)
    mgr, sessions = make(repo)
    try:
        cid = mgr.start("haiku", " hi ", images=[a])
        wait_for(lambda: view(mgr, cid).state == "waiting")
        sent = sessions[0].sent[0]
        assert sent.startswith("hi") and sent.endswith(attachment_note(repo, [a]))
        assert "Attached images: " in sent
        mgr.message(cid, "two", images=[a, b])
        wait_for(lambda: len(sessions[0].sent) == 2 and view(mgr, cid).state == "waiting")
        assert sessions[0].sent[1].endswith(attachment_note(repo, [a, b]))
        mgr.message(cid, "plain")
        wait_for(lambda: len(sessions[0].sent) == 3 and view(mgr, cid).state == "waiting")
        assert sessions[0].sent[2] == "plain"
        payloads = user_payloads(repo, cid)
        assert payloads[0] == {"text": "hi", "images": [a]}
        assert payloads[1]["images"] == [a, b]
        assert payloads[2] == {"text": "plain"}
        users = [e for e in view(mgr, cid).transcript if e.role == "user"]
        assert [u.images for u in users] == [[a], [a, b], []]
        assert view(mgr, cid).commits[0].endswith("chat haiku: hi")
    finally:
        mgr.close()
    restarted = [v for v in chats_from_log(repo) if v.chat_id == cid][0]
    entries = restarted.transcript
    assert [e.images for e in entries if e.role == "user"] == [[a], [a, b], []]
    assert all(e.images == [] for e in entries if e.role == "agent")


def test_bad_ids_in_log_rebuild_empty(tmp_path):
    repo = init_repo(tmp_path / "r")
    log = ChatLog(repo, "chat-x")
    log.emit("chat_started", {"tier": "haiku", "run_id": None, "task_id": None, "pid": 0})
    log.emit("chat_user", {"text": "hello", "images": ["../x", 3]})
    views = [v for v in chats_from_log(repo) if v.chat_id == "chat-x"]
    assert views and views[0].transcript[0].images == []


def test_fake_chat_with_image(tmp_path):
    repo = init_repo(tmp_path / "r")
    a = store_image(repo, PNG)
    mgr = ChatManager(repo, fake=True, fake_delay=0)
    try:
        cid = mgr.start("haiku", "first")
        wait_for(lambda: view(mgr, cid).state == "waiting")
        mgr.message(cid, "look", images=[a])
        wait_for(lambda: view(mgr, cid).state == "waiting" and len(view(mgr, cid).transcript) == 4)
        assert view(mgr, cid).transcript[-1].role == "agent"
    finally:
        mgr.close()
