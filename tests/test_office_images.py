import asyncio

from test_office import state_is, wait_for

from harness.planner import PlannerTurn
from harness.planner_log import PlannerLog, read_planner_events
from harness.ui.office import Office, _OfficeOpusDesk, office_view_from_log
from harness.ui.uploads import attachment_note, store_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> PlannerTurn:
        self.sent.append(text)
        await asyncio.sleep(0)
        return PlannerTurn(text="a question?")

    async def close(self) -> None:
        pass


def _make(tmp_path, rec):
    return Office(tmp_path, fake=True, fake_delay=0, session_factory=lambda repo, progress: rec)


def _users(tmp_path):
    return [e for e in read_planner_events(tmp_path) if e.type == "planner_user"]


def test_start_and_message_carry_images(tmp_path):
    a, b = store_image(tmp_path, PNG), store_image(tmp_path, JPEG)
    rec = Recorder()
    office = _make(tmp_path, rec)
    try:
        office.start("a demo project", images=[a])
        wait_for(state_is(office, "asking"))
        assert rec.sent[0].endswith("a demo project" + attachment_note(tmp_path, [a]))
        office.message("my answer", images=[b])
        wait_for(lambda: len(rec.sent) == 2)
        assert rec.sent[1] == "my answer" + attachment_note(tmp_path, [b])
        assert rec.sent[1].endswith(str(tmp_path.resolve() / ".harness" / "uploads" / b))
        users = _users(tmp_path)
        assert users[0].payload == {"text": "a demo project", "images": [a]}
        assert users[1].payload == {"text": "my answer", "images": [b]}
        wait_for(state_is(office, "asking"))
        assert [t.images for t in office.view().transcript if t.role == "user"] == [[a], [b]]
        view = office_view_from_log(tmp_path)
        assert [t.images for t in view.transcript if t.role == "user"] == [[a], [b]]
    finally:
        office.close()


def test_no_images_no_key(tmp_path):
    rec = Recorder()
    office = _make(tmp_path, rec)
    try:
        office.start("plain")
        wait_for(state_is(office, "asking"))
        office.message("answer")
        wait_for(lambda: len(rec.sent) == 2)
        assert rec.sent[0].endswith("plain")
        assert "Attached images" not in rec.sent[0]
        assert rec.sent[1] == "answer"
        assert all("images" not in e.payload for e in _users(tmp_path))
    finally:
        office.close()


def test_junk_images_rebuild_empty(tmp_path):
    log = PlannerLog(tmp_path, "s1")
    log.emit("planner_started", {"session_id": "s1", "route": "project"})
    log.emit("planner_user", {"text": "hi", "images": ["../x.png", 3]})
    view = office_view_from_log(tmp_path)
    assert [t.images for t in view.transcript] == [[]]


def test_demo_planner_and_desk(tmp_path):
    a = store_image(tmp_path, PNG)
    office = Office(tmp_path, fake=True, fake_delay=0)
    try:
        office.start("demo", images=[a])
        wait_for(state_is(office, "asking"))
    finally:
        office.close()
    tmp2 = tmp_path / "other"
    tmp2.mkdir()
    b = store_image(tmp2, PNG)
    office = Office(tmp2, fake=True, fake_delay=0)
    try:
        _OfficeOpusDesk(office).start("x", "project", images=[b])
        assert _users(tmp2)[0].payload["images"] == [b]
        wait_for(state_is(office, "asking"))
    finally:
        office.close()
