import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest
from ui_fixtures import write_paused_run

import harness.ui
from harness.ui.server import make_server

PAGE = Path(harness.ui.__file__).parent / "static" / "index.html"
STATUSES = ["pending", "running", "verifying", "done", "failed", "blocked", "paused"]
RUN_KEYS = [
    "stage", "phase", "stopped_reason", "idle_minutes", "wave", "waves_total", "cost_usd",
    "tiers", "tasks", "events",
]  # fmt: skip
TASK_KEYS = [
    "task_id", "title", "status", "tier", "attempt", "max_attempts", "escalated", "cost_usd",
    "input_tokens", "output_tokens", "detail", "activity",
]  # fmt: skip
TIER_KEYS = ["tier", "attempts", "cost_usd"]
EVENT_KEYS = ["ts", "task_id", "text"]


@pytest.fixture
def server(tmp_path):
    write_paused_run(tmp_path)
    srv = make_server(tmp_path, 0, poll_interval=0.05)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join(5)


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_basics():
    text = _text()
    assert text.lower().startswith("<!doctype html>")
    assert "<title>" in text


def test_no_external_urls():
    text = _text()
    assert "http://" not in text
    assert "https://" not in text


def test_no_html_parsing_apis():
    text = _text()
    for word in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("]:
        assert word not in text
    assert "textContent" in text


def test_mentions():
    text = _text()
    for word in [
        "/api/runs", "/api/stream/", "EventSource", "No runs yet", "location.hash", "problem",
        "No activity for",
    ]:  # fmt: skip
        assert word in text


@pytest.mark.parametrize("cls", [f"status-{s}" for s in STATUSES] + [
    "phase-running", "phase-stopped", "phase-finished",
])  # fmt: skip
def test_css_rules(cls):
    assert re.search(rf"\.{cls}\s*\{{", _text())


def test_keys_contract(server):
    text = _text()
    for key in RUN_KEYS + TASK_KEYS + TIER_KEYS + EVENT_KEYS:
        assert re.search(rf"\b{key}\b", text), key
    with urllib.request.urlopen(server + "/api/run/paused1", timeout=5) as resp:
        import json

        payload = json.loads(resp.read())
    for key in RUN_KEYS:
        assert key in payload, key
    for key in TASK_KEYS:
        assert key in payload["tasks"][0], key
    for key in TIER_KEYS:
        assert key in payload["tiers"][0], key
    for key in EVENT_KEYS:
        assert key in payload["events"][0], key


def test_served_at_root(server):
    with urllib.request.urlopen(server + "/", timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.read().decode("utf-8") == _text()


def test_script_parses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    m = re.search(r"<script>(.*?)</script>", _text(), re.S)
    assert m
    js = tmp_path / "page.js"
    js.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run([node, "--check", str(js)], capture_output=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_feed_does_not_repeat_task_id(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    m = re.search(r"function needsTid\(.*?\n  \}", _text(), re.S)
    assert m
    js = tmp_path / "tid.js"
    js.write_text(
        m.group(0)
        + "\nconsole.log(JSON.stringify([needsTid('a', 'a started on haiku'),"
        + " needsTid('a', 'worker_progress'), needsTid('ab', 'a started'),"
        + " needsTid(null, 'run started: 2 orders in 2 waves')]));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(js)], capture_output=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().strip() == "[false,true,true,false]"
