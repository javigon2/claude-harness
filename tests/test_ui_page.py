import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest
from ui_fixtures import write_paused_run

import harness.ui
from harness.ui.markdown import parse_markdown
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


@pytest.mark.parametrize("cls", [f"desk-{s}" for s in STATUSES] + ["desk-boss", "bubble"])
def test_office_css_rules(cls):
    assert re.search(rf"\.{cls}[\s,{{]", _text())


def test_office_labels_and_collapsible():
    text = _text()
    assert "'planner'" in text
    assert "'awaiting'" in text
    assert 'id="office"' in text
    assert text.count('<details class="fold" open>') == 2


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


BOSS_STATES = [
    "idle", "busy", "asking", "ready", "staging", "supervising", "interrupted", "failed",
    "terminal",
]  # fmt: skip
OFFICE_IDS = [
    "office-modal", "desc", "answer", "continue", "office-msg", "btn-start", "btn-send",
    "btn-continue", "btn-implement", "btn-resume", "btn-fresh", "btn-next", "btn-new",
    "reply-body", "plan-body", "progress",
]  # fmt: skip


def test_modal_ids_and_meta_tags():
    text = _text()
    for id_ in OFFICE_IDS:
        assert f'id="{id_}"' in text, id_
    assert '<meta name="harness-token" content="">' in text
    assert '<meta name="harness-mode" content="read-only">' in text
    assert "<dialog" in text


def test_boss_states_and_endpoints_are_in_the_script():
    text = _text()
    for state in BOSS_STATES:
        assert f"'{state}'" in text, state
    for word in [
        "/api/office", "/api/plan", "/api/planner/start", "/api/planner/message",
        "/api/planner/implement", "/api/planner/next-stage", "X-Harness-Token", "has a question",
        "plan ready!", "writing work orders", "supervising ", "busy (terminal)",
    ]:  # fmt: skip
        assert word in text, word


def test_boss_css():
    text = _text()
    assert re.search(r"\.boss-ready \.desk-top \{ animation: flash 1s infinite; \}", text)
    assert "@keyframes flash" in text
    for state in BOSS_STATES[1:]:
        assert re.search(rf"\.boss-{state} \.desk-top", text), state
    assert re.search(r"\.desk-boss\.clickable \{ cursor: pointer; \}", text)


def test_links_are_never_anchors():
    text = _text()
    assert "createElement('a')" not in text
    assert "el('a'" not in text
    assert "<a " not in text


STUB = """
function mk(tag) {
  return {
    tag: tag, className: '', text: '', children: [], frag: false,
    set textContent(v) { this.text = String(v); },
    appendChild: function (c) {
      if (c.frag) { for (var i = 0; i < c.children.length; i++) this.children.push(c.children[i]); }
      else this.children.push(c);
      return c;
    },
    replaceChildren: function () {
      this.children = [];
      for (var i = 0; i < arguments.length; i++) this.appendChild(arguments[i]);
    }
  };
}
var document = {
  createElement: mk,
  createTextNode: function (t) { var n = mk('#text'); n.text = t; return n; },
  createDocumentFragment: function () { var n = mk('#frag'); n.frag = true; return n; }
};
function ser(n) {
  if (n.tag === '#text') return n.text;
  var cls = n.className ? ' class="' + n.className + '"' : '';
  return '<' + n.tag + cls + '>' + n.text + n.children.map(ser).join('') + '</' + n.tag + '>';
}
"""


def test_markdown_blocks_render_to_the_expected_dom(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    text = _text()
    el = re.search(r"  function el\(.*?\n  \}", text, re.S)
    string = re.search(r"  function str\(x\).*", text)
    start = text.index("  // ---- markdown block tree")
    end = text.index("  function fill(")
    assert el and string
    md = (
        "# T\n\nHi **b** `c` [l](u.html) <script>alert(1)</script>\n\n- a\n  - child\n- b\n\n"
        "| h | i |\n|---|---|\n| <x> | `y` |\n\n```\n<b>\n```\n\n> q\n\n---"
    )
    script = (
        STUB + el.group(0) + "\n" + string.group(0) + "\n" + text[start:end]
        + "\nvar root = mk('div'); root.appendChild(renderBlocks(" + json.dumps(parse_markdown(md))
        + ")); console.log(ser(root)); console.log(renderBlocks(null).children.length);"
    )  # fmt: skip
    path = tmp_path / "render.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, str(path)], capture_output=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    html, empty = proc.stdout.decode().strip().split("\n")
    assert empty == "0"
    assert html == (
        "<div><h1>T</h1>"
        '<p>Hi <strong>b</strong> <code>c</code> l<span class="muted"> (u.html)</span>'
        " <script>alert(1)</script></p>"
        "<ul><li><p>a</p><ul><li><p>child</p></li></ul></li><li><p>b</p></li></ul>"
        "<table><thead><tr><th>h</th><th>i</th></tr></thead>"
        "<tbody><tr><td><x></td><td><code>y</code></td></tr></tbody></table>"
        "<pre><code><b></code></pre><blockquote><p>q</p></blockquote><hr></hr></div>"
    )
