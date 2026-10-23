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
    assert "'Opus'" in text
    assert "'planner'" not in text
    assert "'awaiting'" in text
    assert 'id="office"' in text
    assert text.count('<details class="fold">') == 2
    assert '<details class="fold" open>' not in text


def test_two_panel_layout():
    text = _text()
    for id_ in ["layout", "left", "right", "convo", "transcript"]:
        assert f'id="{id_}"' in text, id_
    assert '<main id="layout">' in text
    assert text.index('id="left"') < text.index('id="transcript"') < text.index('id="right"')
    assert text.index('id="right"') < text.index('id="office"') < text.index('id="content"')
    assert re.search(r"#layout \{[^}]*grid-template-columns: 1fr 1fr;", text)
    assert re.search(r"#left \{[^}]*overflow: auto;", text)
    assert "@media (max-width: 900px)" in text


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
    "composer", "desc", "answer", "continue", "office-msg", "btn-start", "btn-send",
    "btn-continue", "btn-implement", "btn-resume", "btn-fresh", "btn-next", "btn-new",
    "plan-fold", "plan-body", "progress",
]  # fmt: skip
SECTION_IDS = [
    "sec-prompt", "sec-status", "sec-reply", "sec-plan", "sec-failed", "sec-next", "office-note",
    "office-title", "office-cost", "status-line", "failed-line", "next-line",
]  # fmt: skip
REMOVED_IDS = [
    "office-modal", "office-close", "reply-body", "plan-reply", "draft-body", "btn-view-plan",
    "btn-back",
]  # fmt: skip


def test_modal_ids_and_meta_tags():
    text = _text()
    for id_ in OFFICE_IDS:
        assert f'id="{id_}"' in text, id_
    assert '<meta name="harness-token" content="">' in text
    assert '<meta name="harness-mode" content="read-only">' in text
    assert "<dialog" not in text


def test_composer_and_plan_fold_replace_the_modal():
    text = _text()
    for id_ in SECTION_IDS:
        assert f'id="{id_}"' in text, id_
    for id_ in REMOVED_IDS:
        assert f'id="{id_}"' not in text, id_
        assert f"'{id_}'" not in text, id_
    for word in ["openModal", "renderModal", "showPlan", "showModal", "modal"]:
        assert word not in text, word
    assert '<div id="composer" class="hidden">' in text
    assert '<details id="plan-fold" class="hidden"><summary>Plan</summary>' in text
    assert "What should Opus plan?" in text
    convo = text.index('id="convo"')
    assert convo < text.index('id="transcript"') < text.index('id="composer"')
    assert text.index('id="composer"') < text.index('id="plan-fold"') < text.index('id="right"')
    assert "renderComposer();" in text
    assert "focusComposer" in text


def test_pick_section(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    m = re.search(r"  function pickSection\(.*?\n  \}", _text(), re.S)
    assert m
    cases = [
        {"state": "idle"}, {"state": "idle", "next_stage": 2}, {"state": "failed"},
        {"state": "interrupted"}, {"state": "asking", "plan_exists": True}, {"state": "ready"},
        {"state": "busy"}, {"state": "staging"}, {"state": "supervising"}, {"state": "terminal"},
    ]  # fmt: skip
    script = (
        "var forcePrompt = false;\n" + m.group(0) + "\nvar cases = " + json.dumps(cases) + ";\n"
        "var out = cases.map(pickSection);\n"
        "forcePrompt = true;\n"
        "out.push(pickSection(cases[1]), pickSection(cases[2]));\n"
        "console.log(JSON.stringify(out));\n"
    )
    js = tmp_path / "pick.js"
    js.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, str(js)], capture_output=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "prompt", "next", "failed", "failed", "reply", "plan", "status", "status", "status",
        "status", "prompt", "prompt",
    ]  # fmt: skip


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


def test_transcript_css_and_names():
    text = _text()
    for word in ["msg-user", "msg-opus", "msg-who", "'You'", "renderTranscript(o)"]:
        assert word in text, word
    assert re.search(r"\.msg-text \{[^}]*white-space: pre-wrap;", text)


def _transcript_script() -> str:
    text = _text()
    el = re.search(r"  function el\(.*?\n  \}", text, re.S)
    string = re.search(r"  function str\(x\).*", text)
    msg = re.search(r"  function msgNode\(.*?\n  \}", text, re.S)
    render = re.search(r"  function renderTranscript\(.*?\n  \}", text, re.S)
    start = text.index("  // ---- markdown block tree")
    end = text.index("  function fill(")
    assert el and string and msg and render
    return (
        STUB + "var shownSession = null, shownCount = 0;\n" + el.group(0) + "\n"
        + string.group(0) + "\n" + text[start:end] + "\n" + msg.group(0) + "\n"
        + render.group(0) + "\n"
    )  # fmt: skip


def _run_node(tmp_path, script: str) -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    path = tmp_path / "t.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, str(path)], capture_output=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.decode().strip().split("\n")


def test_message_nodes_render_to_the_expected_dom(tmp_path):
    user = {"role": "user", "text": "plan <b>a</b>\nnow", "blocks": []}
    opus = {"role": "opus", "text": "Hi **x**", "blocks": parse_markdown("Hi **x**")}
    script = (
        _transcript_script()
        + f"console.log(JSON.stringify(ser(msgNode({json.dumps(user)}))));\n"
        + f"console.log(JSON.stringify(ser(msgNode({json.dumps(opus)}))));\n"
    )
    lines = _run_node(tmp_path, script)
    assert json.loads(lines[0]) == (
        '<li class="msg msg-user"><div class="msg-who">You</div>'
        '<div class="msg-text">plan <b>a</b>\nnow</div></li>'
    )
    assert json.loads(lines[1]) == (
        '<li class="msg msg-opus"><div class="msg-who">Opus</div>'
        '<div class="md"><p>Hi <strong>x</strong></p></div></li>'
    )


def test_transcript_appends_in_place_and_rebuilds_on_a_new_session(tmp_path):
    entries = [
        {"role": "user", "text": "one", "blocks": []},
        {"role": "opus", "text": "two", "blocks": parse_markdown("two")},
        {"role": "user", "text": "three", "blocks": []},
    ]
    script = (
        _transcript_script()
        + "var E = " + json.dumps(entries) + ";\n"
        + "var nodes = {transcript: mk('ol'), left: mk('section')};\n"
        + "nodes.left.scrollHeight = 500; nodes.left.clientHeight = 100;\n"
        + "nodes.left.scrollTop = 370;\n"
        + "document.getElementById = function (id) { return nodes[id]; };\n"
        + "var out = [];\n"
        + "renderTranscript({session_id: 's1', transcript: E});\n"
        + "out.push([nodes.transcript.children.length, nodes.left.scrollTop]);\n"
        + "nodes.left.scrollTop = 0;\n"
        + "var first = nodes.transcript.children[0];\n"
        + "renderTranscript({session_id: 's1', transcript: E});\n"
        + "out.push([nodes.transcript.children.length, nodes.left.scrollTop]);\n"
        + "renderTranscript({session_id: 's1', transcript: E.slice(0, 2)});\n"
        + "out.push([nodes.transcript.children.length, nodes.transcript.children[0] === first]);\n"
        + "renderTranscript({session_id: 's1', transcript: E});\n"
        + "first = nodes.transcript.children[0];\n"
        + "renderTranscript({session_id: 's2', transcript: [E[2]]});\n"
        + "out.push([nodes.transcript.children.length, nodes.transcript.children[0] === first]);\n"
        + "renderTranscript({session_id: 's2', transcript: []});\n"
        + "out.push([nodes.transcript.children.length]);\n"
        + "console.log(JSON.stringify(out));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [[3, 500], [3, 0], [2, False], [1, False], [0]]


def _classroom_section(text: str) -> str:
    start = text.index("  // ---- the classroom")
    end = text.index("  // ---- markdown block tree")
    return text[start:end]


def test_classroom_markup():
    text = _text()
    ids = ["office", "teacher", "row-opus", "row-sonnet", "row-haiku", "empty"]
    places = [text.index(f'id="{id_}"') for id_ in ids]
    assert places == sorted(places)
    for tier in ["opus", "sonnet", "haiku"]:
        row = (
            f'<div id="row-{tier}" class="row row-{tier} hidden"><div class="row-label">{tier}'
            f'</div><div id="desks-{tier}" class="row-desks"></div></div>'
        )
        assert row in text, tier
    assert re.search(r"\.row\.hidden \{ display: none; \}", text)
    assert re.search(r"\.teacher \{[^}]*justify-content: center;", text)
    assert "'Opus'" in _classroom_section(text)


def test_row_of(tmp_path):
    tasks = [
        {"tier": "sonnet"}, {"tier": "haiku"}, {"tier": "opus"}, {"tier": None}, {"tier": "gpt"},
        {},
    ]  # fmt: skip
    script = (
        _classroom_section(_text())
        + "console.log(JSON.stringify(" + json.dumps(tasks) + ".map(rowOf)));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == ["sonnet", "haiku", "opus", "sonnet", "sonnet", "sonnet"]


DOM = """
function mk(tag) {
  return {
    tag: tag, className: '', text: '', children: [], parentNode: null, attrs: {}, on: {},
    tabIndex: -1,
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    appendChild: function (c) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = this;
      this.children.push(c);
      return c;
    },
    removeChild: function (c) {
      this.children.splice(this.children.indexOf(c), 1);
      c.parentNode = null;
      return c;
    },
    replaceChildren: function () {
      while (this.children.length) this.removeChild(this.children[0]);
      for (var i = 0; i < arguments.length; i++) this.appendChild(arguments[i]);
    },
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    removeAttribute: function (k) { delete this.attrs[k]; },
    addEventListener: function (type, fn) { this.on[type] = fn; }
  };
}
var nodes = {};
var document = {
  createElement: mk,
  createTextNode: function (t) { var n = mk('#text'); n.text = String(t); return n; },
  getElementById: function (id) { return nodes[id] || (nodes[id] = mk('div')); }
};
"""


def _page_helpers(text: str) -> str:
    el = re.search(r"  function el\(.*?\n  \}", text, re.S)
    num = re.search(r"  function num\(x\).*", text)
    string = re.search(r"  function str\(x\).*", text)
    assert el and num and string
    return DOM + el.group(0) + "\n" + num.group(0) + "\n" + string.group(0) + "\n"


CLASSROOM_SCENARIO = """
var out = [];
function ids(tier) {
  return nodes['desks-' + tier].children.map(function (d) { return d.attrs['data-task']; });
}
function rows() { return [ids('opus'), ids('sonnet'), ids('haiku')]; }
runView = {tasks: [
  {task_id: 'a', tier: 'sonnet', status: 'running', activity: 'Edit',
   steps: [{text: 'Reading a.py'}]},
  {task_id: 'b', tier: 'haiku', status: 'pending', activity: null, steps: []},
  {task_id: 'c', tier: null, status: 'done', steps: []},
  {task_id: 'd', tier: 'haiku', status: 'paused', activity: 'x', steps: [{text: 'y'}]}
]};
renderOffice();
var a = deskNodes.a, b = deskNodes.b;
out.push(rows());
out.push([nodes['row-opus'].className, nodes['row-sonnet'].className]);
out.push([a.className, a.children[0].className, a.children[0].text, a.children[3].text,
  a.attrs.role, a.tabIndex]);
out.push([b.className, b.children[0].className]);
out.push([deskNodes.c.className, deskNodes.c.children[3].text, deskNodes.d.children[0].text]);
runView = {tasks: [
  {task_id: 'a', tier: 'sonnet', status: 'done', steps: []},
  {task_id: 'b', tier: 'opus', status: 'running', activity: 'Bash', steps: []}
]};
selectedTask = 'b';
renderOffice();
out.push([deskNodes.a === a, a.className, deskNodes.b === b]);
out.push(rows());
out.push([nodes['row-haiku'].className, 'c' in deskNodes, 'd' in deskNodes]);
out.push([b.className, b.children[0].text, b.children[3].text]);
runView = null;
selectedTask = null;
renderOffice();
out.push([Object.keys(deskNodes).length, nodes['row-opus'].className,
  nodes['row-sonnet'].className, nodes['row-haiku'].className]);
var boss = bossNode;
interactive = true;
office = {state: 'asking', can_click: true};
renderOffice();
out.push([nodes.teacher.children.length, bossNode === boss, boss.className,
  boss.children[0].className, boss.children[0].text, boss.attrs.role]);
boss.on.click();
out.push(focused);
office = {state: 'idle', can_click: false};
renderOffice();
out.push([boss.className, boss.children[0].className, 'role' in boss.attrs]);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_classroom_keeps_desk_nodes(tmp_path):
    text = _text()
    section = _classroom_section(text)
    assert "replaceChildren" not in section
    for word in ["deskNodes", "settled", "selectTask", "data-task"]:
        assert word in section, word
    script = (
        _page_helpers(text)
        + "var office = null, interactive = false, runView = null, focused = 0;\n"
        + "var deskNodes = {}, bossNode = null, selectedTask = null;\n"
        + "function focusComposer() { focused++; }\n"
        + section
        + CLASSROOM_SCENARIO
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        [[], ["a", "c"], ["b", "d"]],
        ["row row-opus hidden", "row row-sonnet"],
        ["desk desk-running", "bubble bubble-running", "Reading a.py", "sonnet · a",
         "button", 0],
        ["desk desk-pending", "bubble bubble-pending hidden"],
        ["desk desk-done settled", "- · c", "paused"],
        [True, "desk desk-done", True],
        [["b"], ["a"], []],
        ["row row-haiku hidden", False, False],
        ["desk desk-running selected", "Bash", "opus · b"],
        [0, "row row-opus hidden", "row row-sonnet hidden", "row row-haiku hidden"],
        [1, True, "desk desk-boss boss-asking clickable", "bubble bubble-boss",
         "has a question", "button"],
        1,
        ["desk desk-boss boss-idle", "bubble bubble-boss hidden", False],
    ]  # fmt: skip


def test_timeline_markup():
    text = _text()
    for word in ['id="timeline-view"', "btn-back-opus", "tl-title", "tl-meta", 'id="timeline"']:
        assert word in text, word
    assert '<div id="timeline-view" class="hidden">' in text
    assert text.index('id="convo"') < text.index('id="timeline-view"') < text.index('id="right"')
    for word in [
        "Back to Opus",
        "'No steps yet'",
        "step-attempt",
        "renderTimeline();",
        "selectTask(null)",
    ]:
        assert word in text, word


TIMELINE_SCENARIO = """
var S0 = {ts: '2026-10-23T09:00:01+00:00', attempt: 1, tier: 'haiku', text: 'Reading a.py'};
var S1 = {ts: '2026-10-23T09:00:02+00:00', attempt: 1, tier: 'haiku', text: 'Running ls'};
var S2 = {ts: '2026-10-23T09:01:00+00:00', attempt: 2, tier: 'sonnet', text: 'Editing a.py'};
var S3 = {ts: '2026-10-23T09:01:05+00:00', attempt: 2, tier: 'sonnet', text: 'Writing b.py'};
var left = byId('left');
left.scrollHeight = 500; left.clientHeight = 100; left.scrollTop = 0;
function desc(li) {
  var t = li.children.length ? li.children.map(function (c) { return c.text; }).join(' ') : li.text;
  return li.className + '|' + t;
}
function items() { return byId('timeline').children.map(desc); }
function run(tier, status, attempt, steps) {
  runView = {tasks: [{task_id: 'a', title: 'Do A', tier: tier, status: status,
    attempt: attempt, max_attempts: 3, steps: steps}]};
  renderTimeline();
}
var out = [];
out.push(stepKey(S0, 0));
selectedTask = 'a';
run('haiku', 'running', 1, []);
out.push([items(), byId('tl-title').text, byId('tl-meta').text]);
run('haiku', 'running', 1, [S0]);
var first = byId('timeline').children[1];
out.push(items());
run('haiku', 'running', 1, [S0, S1]);
out.push([items(), byId('timeline').children[1] === first, left.scrollTop]);
run('sonnet', 'done', 2, [S0, S1, S2]);
out.push([items(), byId('tl-meta').text]);
run('sonnet', 'running', 2, [S1, S2]);
out.push([items(), byId('timeline').children[1] === first]);
left.scrollTop = 370;
run('sonnet', 'running', 2, [S1, S2, S3]);
out.push([byId('timeline').children.length, left.scrollTop]);
selectedTask = 'zz';
renderTimeline();
out.push([items(), byId('tl-title').text, byId('tl-meta').text]);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_timeline_appends_steps_in_place(tmp_path):
    text = _text()
    start = text.index("  // ---- a worker's timeline")
    end = text.index("  // ---- the composer and the plan")
    script = (
        _page_helpers(text)
        + "var runView = null, selectedTask = null;\n"
        + "var tlTask = null, tlFirst = null, tlCount = 0, tlAttempt = null, tlLast = null;\n"
        + "function byId(id) { return document.getElementById(id); }\n"
        + text[start:end]
        + TIMELINE_SCENARIO
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    head = ["step-attempt|attempt 1 on haiku", "step|09:00:01 Reading a.py"]
    two = [*head, "step current|09:00:02 Running ls"]
    assert out == [
        "2026-10-23T09:00:01+00:00|0",
        [["step-empty muted|No steps yet"], "a · Do A", "haiku · running · attempt 1/3"],
        ["step-attempt|attempt 1 on haiku", "step current|09:00:01 Reading a.py"],
        [two, True, 0],
        [
            [*head, "step|09:00:02 Running ls", "step-attempt|attempt 2 on sonnet",
             "step|09:01:00 Editing a.py"],
            "sonnet · done · attempt 2/3",
        ],
        [
            ["step-attempt|attempt 1 on haiku", "step|09:00:02 Running ls",
             "step-attempt|attempt 2 on sonnet", "step current|09:01:00 Editing a.py"],
            False,
        ],
        [5, 500],
        [["step-empty muted|No steps yet"], "zz · ", "- · ? · attempt ?/?"],
    ]  # fmt: skip
