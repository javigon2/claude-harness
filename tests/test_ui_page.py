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
    section = _classroom_section(text)
    assert "'Opus'" not in section
    for word in ["'opus · agent'", "'Orchestrator'", "'desk-orch'", "'desk-opus'"]:
        assert word in section, word


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
    insertBefore: function (c, ref) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = this;
      var i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(c);
      else this.children.splice(i, 0, c);
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
out.push([nodes.teacher.children.length, nodes.teacher.children[0] === orchNode,
  nodes['desks-opus'].children[0] === boss, bossNode === boss, boss.className,
  boss.children[0].className, boss.children[0].text, boss.attrs.role]);
boss.on.click();
out.push([focused, hidden.convo, hidden['orch-view']]);
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
        + "var deskNodes = {}, bossNode = null, orchNode = null, selectedTask = null;\n"
        + "var chatTier = null, chatDesks = {}, tlTask = null, hidden = {};\n"
        + "function focusComposer() { focused++; }\n"
        + "function setHidden(id, hide) { hidden[id] = hide; }\n"
        + _chat_section(text)
        + section
        + CLASSROOM_SCENARIO
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        [[None], ["a", "c"], ["b", "d"]],
        ["row row-opus", "row row-sonnet"],
        ["desk desk-running", "bubble bubble-running", "Reading a.py", "sonnet · a",
         "button", 0],
        ["desk desk-pending", "bubble bubble-pending hidden"],
        ["desk desk-done settled", "- · c", "paused"],
        [True, "desk desk-done", True],
        [[None, "b"], ["a"], []],
        ["row row-haiku hidden", False, False],
        ["desk desk-running selected", "Bash", "opus · b"],
        [0, "row row-opus", "row row-sonnet hidden", "row row-haiku hidden"],
        [1, True, True, True, "desk desk-boss boss-asking clickable", "bubble bubble-boss",
         "has a question", "button"],
        [1, False, True],
        ["desk desk-boss boss-idle clickable", "bubble bubble-boss hidden", True],
    ]  # fmt: skip


def test_timeline_markup():
    text = _text()
    for word in ['id="timeline-view"', "btn-back-opus", "tl-title", "tl-meta", 'id="timeline"']:
        assert word in text, word
    assert '<div id="timeline-view" class="hidden">' in text
    assert text.index('id="convo"') < text.index('id="timeline-view"') < text.index('id="right"')
    for word in [
        "Back to Orchestrator",
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
        + "var runView = null, selectedTask = null, interactive = false;\n"
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


KEYFRAMES = ["walk-in", "walk-out", "type", "think", "ask", "watch", "flash"]


def test_animations_css():
    text = _text()
    for name in KEYFRAMES:
        assert f"@keyframes {name} {{" in text, name
    done = re.search(
        r"\.desk-done \.worker, \.desk-failed \.worker, \.desk-blocked \.worker \{ opacity: 0;"
        r" animation: walk-out [^}]*forwards; \}",
        text,
    )
    assert done
    running = r"\.desk-running \.worker, \.desk-verifying \.worker \{ animation: walk-in "
    assert re.search(running, text)
    settled = text.index(".settled .worker { animation: none; }")
    assert settled > done.start()
    assert re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{\s*\*, \*::before, \*::after \{ "
        r"animation: none !important; transition: none !important; \}\s*\}",
        text,
    )
    for state, name in [("busy", "think"), ("staging", "think"), ("asking", "ask")]:
        assert re.search(rf"\.boss-{state} \.mark::before[^{{]*\{{[^}}]*animation: {name} ", text)
    assert re.search(r"\.boss-supervising \.worker::before \{ animation: watch ", text)
    for word in ["setTimeout", "requestAnimationFrame"]:
        assert word not in text, word


ROUTE_SELECT = """          <select id="route" aria-label="Route">
            <option value="quick">Quick plan</option>
            <option value="project">Full plan</option>
          </select>"""


def test_route_selector_triage_line_and_resume_button_markup():
    text = _text()
    assert ROUTE_SELECT in text
    order = ['id="desc"', 'id="route"', 'id="btn-start"']
    assert [text.index(w) for w in order] == sorted(text.index(w) for w in order)
    assert '<p id="triage-line" class="muted"></p>' in text
    assert text.index('id="transcript"') < text.index('id="triage-line"')
    assert text.index('id="triage-line"') < text.index('id="composer"')
    assert '<button id="btn-resume-run" type="button" class="hidden">Resume run</button>' in text
    assert text.index('id="composer"') < text.index('id="btn-resume-run"')
    assert text.index('id="btn-resume-run"') < text.index('id="sec-prompt"')
    assert re.search(r"#triage-line:empty \{ display: none; \}", text)
    for word in [
        "route: byId('route').value", "'/api/run/resume'", "'Start this quick plan?'",
        "'Start implementing'", "'Start'",
    ]:  # fmt: skip
        assert word in text, word


def _functions(text: str, *names: str) -> str:
    parts = []
    for name in names:
        m = re.search(rf"  function {name}\(.*?\n  \}}", text, re.S)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts) + "\n"


def test_triage_text_can_implement_and_resume_label(tmp_path):
    triage = [
        {"route": "quick", "triage_reason": "two files"}, {"route": "direct", "triage_reason": ""},
        {"route": "project", "triage_reason": None}, {"route": None, "triage_reason": "x"}, {},
    ]  # fmt: skip
    implement = [
        {"plan_kind": "quick", "quick_plan": ".harness/plans/quick-1/plan.json"},
        {"plan_kind": "quick", "quick_plan": None, "plan_status": "draft"},
        {"plan_kind": "project", "plan_status": "draft"},
        {"plan_kind": None, "plan_status": "draft"},
        {"plan_kind": "project", "plan_status": "approved"},
    ]
    resume = [{"resumable_run": "20261023-090000"}, {"resumable_run": None}, {}]
    script = (
        _functions(_text(), "triageText", "canImplement", "resumeLabel")
        + "console.log(JSON.stringify([" + json.dumps(triage) + ".map(triageText), "
        + json.dumps(implement) + ".map(canImplement), " + json.dumps(resume)
        + ".map(resumeLabel), triageText(null)]));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        ["Triage routed this: quick (two files)", "Triage routed this: direct ()", "", "", ""],
        [True, False, True, True, False],
        ["Resume run 20261023-090000", "", ""],
        "",
    ]


def test_chat_view_markup():
    text = _text()
    assert '<div id="chat-view" class="hidden">' in text
    ids = ["timeline-view", "chat-view", "btn-chat-back", "chat-title", "chat-cost",
           "chat-transcript", "chat-progress", "chat-commits", "chat-note", "chat-msg",
           "chat-text", "btn-chat-send", "btn-chat-close", "right"]  # fmt: skip
    places = [text.index(f'id="{id_}"') for id_ in ids]
    assert places == sorted(places)
    assert '<button id="btn-chat-close" type="button" class="hidden">Close chat</button>' in text
    assert re.search(r"#chat-note:empty \{ display: none; \}", text)
    assert re.search(r"\.msg-agent \{", text)
    for word in ["'/api/chat/start'", "'/api/chat/message'", "'/api/chat/close'", "renderChat();"]:
        assert word in text, word


def _chat_section(text: str) -> str:
    start = text.index("  // ---- an agent chat in the left panel")
    end = text.index("  function followLaunchedRun(")
    return text[start:end]


def test_open_chat_and_chat_send(tmp_path):
    chats = [
        {"chat_id": "c3", "tier": "haiku", "state": "closed"},
        {"chat_id": "c2", "tier": "sonnet", "state": "working"},
        {"chat_id": "c1", "tier": "haiku", "state": "waiting"},
    ]
    script = (
        _chat_section(_text())
        + "var C = " + json.dumps(chats) + ";\n"
        + "function id(c) { return c ? c.chat_id : null; }\n"
        + "console.log(JSON.stringify([id(openChat(C, 'haiku')), id(openChat(C, 'sonnet')),"
        + " id(openChat(C.slice(0, 1), 'haiku')), id(openChat(null, 'haiku')),"
        + " chatSend(C[2], 'haiku', 'hi'), chatSend(C[1], 'sonnet', 'yo'),"
        + " chatSend(C[0], 'haiku', 'again'), chatSend(null, 'sonnet', 'new')]));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        "c1", "c2", None, None,
        {"path": "/api/chat/message", "body": {"chat_id": "c1", "text": "hi"}},
        {"path": "/api/chat/message", "body": {"chat_id": "c2", "text": "yo"}},
        {"path": "/api/chat/start", "body": {"tier": "haiku", "text": "again"}},
        {"path": "/api/chat/start", "body": {"tier": "sonnet", "text": "new"}},
    ]  # fmt: skip


CHAT_SCENARIO = """
var hidden = {}, rendered = 0;
function setHidden(id, hide) { hidden[id] = hide; }
function renderOffice() { rendered++; }
function byId(id) { return document.getElementById(id); }
var left = byId('left');
left.scrollHeight = 500; left.clientHeight = 100; left.scrollTop = 0;
function items() {
  return byId('chat-transcript').children.map(function (li) {
    return li.className + '|' + li.children[0].text;
  });
}
function texts(id) { return byId(id).children.map(function (li) { return li.text; }); }
var U = {role: 'user', ts: 't1', text: 'add a flag', blocks: []};
var A = {role: 'agent', ts: 't2', text: 'Done',
  blocks: [{type: 'paragraph', inline: [{t: 'text', text: 'Done'}]}]};
var out = [];
office = {chats: []};
selectedTask = 'a';
openChatPanel('haiku');
out.push([JSON.parse(JSON.stringify(hidden)), selectedTask, chatTier, chatId, rendered,
  byId('chat-title').text, items(), hidden['btn-chat-close'], byId('chat-note').text]);
office = {chats: [{chat_id: 'c1', tier: 'haiku', state: 'working', transcript: [U],
  progress: ['Read: README.md'], commits: [], cost_usd: 0, error: null, task_id: null}]};
renderChat();
var first = byId('chat-transcript').children[0];
out.push([chatId, byId('chat-title').text, items(), texts('chat-progress'),
  hidden['btn-chat-close'], byId('chat-cost').text]);
office = {chats: [{chat_id: 'c1', tier: 'haiku', state: 'waiting', transcript: [U, A],
  progress: [], commits: ['abc1234 chat haiku: add a flag'], cost_usd: 0.002, error: null,
  task_id: 'demo-a'}]};
left.scrollTop = 370;
renderChat();
out.push([byId('chat-title').text, items(), byId('chat-transcript').children[0] === first,
  texts('chat-progress'), texts('chat-commits'), byId('chat-cost').text, left.scrollTop]);
office = {chats: [{chat_id: 'c1', tier: 'haiku', state: 'failed', transcript: [U, A],
  progress: [], commits: [], cost_usd: 0.002, error: 'the agent failed', task_id: null}]};
renderChat();
out.push([chatId, byId('chat-title').text, byId('chat-note').text, hidden['btn-chat-close']]);
office.chats[0].error = null;
office.chats[0].state = 'closed';
renderChat();
out.push(byId('chat-note').text);
office = {chats: [{chat_id: 'c4', tier: 'haiku', state: 'working', transcript: [U],
  progress: [], commits: [], cost_usd: 0, error: null, task_id: null},
  {chat_id: 'c1', tier: 'haiku', state: 'closed', transcript: [U, A]}]};
renderChat();
out.push([chatId, items().length, byId('chat-transcript').children[0] === first]);
chatTier = null;
byId('chat-title').text = 'untouched';
renderChat();
out.push(byId('chat-title').text);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_chat_panel_renders_in_place(tmp_path):
    text = _text()
    money = re.search(r"  function money\(x\).*", text)
    msg = re.search(r"  function msgNode\(.*?\n  \}", text, re.S)
    assert money and msg
    start = text.index("  // ---- markdown block tree")
    end = text.index("  function fill(")
    script = (
        _page_helpers(text) + money.group(0) + "\n" + text[start:end] + msg.group(0) + "\n"
        + "var office = null, selectedTask = null, tlTask = null;\n"
        + "var chatTier = null, chatId = null, chatShown = null, chatCount = 0;\n"
        + "document.createDocumentFragment = function () { return mk('#frag'); };\n"
        + _chat_section(text) + CHAT_SCENARIO
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    agent = "msg msg-agent|haiku"
    assert out == [
        [{"orch-view": True, "convo": True, "timeline-view": True, "chat-view": False,
          "btn-chat-close": True},
         None, "haiku", None, 1, "haiku · new chat", [], True, ""],
        ["c1", "haiku · working", ["msg msg-user|You"], ["Read: README.md"], False, ""],
        ["haiku · waiting · task demo-a", ["msg msg-user|You", agent], True, [],
         ["committed abc1234 chat haiku: add a flag"], "$0.0020", 500],
        ["c1", "haiku · failed", "the agent failed", True],
        "This chat has ended; sending starts a new one.",
        ["c4", 1, False],
        "untouched",
    ]  # fmt: skip


def test_chat_desk_css_and_names():
    text = _text()
    for rule in [
        r"\.desk-chat \{ cursor: pointer; \}",
        r"\.chat-working \.worker, \.chat-waiting \.worker \{ display: block; \}",
        r"\.chat-working \.worker \{ animation: type ",
        r"\.chat-free \.desk-top \{", r"\.chat-working \.desk-top \{",
        r"\.chat-waiting \.desk-top \{", r"\.bubble-chat \{",
    ]:  # fmt: skip
        assert re.search(rule, text), rule
    assert "'desk-chat-' + tier" in text
    apply = re.search(r"  function applyOffice\(.*?\n  \}", text, re.S)
    assert apply and "followHandoff();" in apply.group(0)


CHAT_DESK_SCENARIO = """
var opened = [];
function openChatPanel(tier) { opened.push(tier); chatTier = tier; }
function desk(tier) { return nodes['desks-' + tier].children; }
function info(d) {
  return [d.id, d.className, d.children[0].className, d.children[0].text, d.children[3].text];
}
var out = [];
runView = {tasks: [{task_id: 'a', tier: 'haiku', status: 'running', steps: []}]};
renderOffice();
out.push([desk('haiku').length, desk('sonnet').length, nodes['row-sonnet'].className]);
interactive = true;
office = {state: 'idle', chats: []};
renderOffice();
var h = chatDesks.haiku;
out.push([info(h), h.attrs.role, h.attrs.title, h.tabIndex, desk('haiku').length,
  nodes['row-sonnet'].className, nodes['row-opus'].className]);
office = {state: 'idle', chats: [
  {chat_id: 'c2', tier: 'sonnet', state: 'waiting', progress: []},
  {chat_id: 'c1', tier: 'haiku', state: 'working', progress: ['Read: a.md', 'Edit: notes.md']},
  {chat_id: 'c0', tier: 'haiku', state: 'closed', progress: []}
]};
renderOffice();
out.push([chatDesks.haiku === h, info(h), info(chatDesks.sonnet)]);
h.on.click();
renderOffice();
out.push([opened.slice(), h.className]);
office.chats[1].progress = [];
runView = null;
renderOffice();
out.push([h.children[0].text, desk('haiku').length, h.parentNode === nodes['desks-haiku']]);
office = {state: 'idle', handoff_chat: 'c9', chats: []};
followHandoff();
office = {state: 'idle', handoff_chat: 'c9', chats: [{chat_id: 'c9', tier: 'sonnet',
  state: 'working'}]};
followHandoff();
followHandoff();
out.push([opened.slice(), lastHandoff]);
interactive = false;
office = {state: 'idle', handoff_chat: 'c10', chats: [{chat_id: 'c10', tier: 'haiku',
  state: 'working'}]};
followHandoff();
out.push([opened.slice(), lastHandoff]);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_chat_desks_and_handoff(tmp_path):
    text = _text()
    script = (
        _page_helpers(text)
        + "var office = null, interactive = false, runView = null;\n"
        + "var deskNodes = {}, bossNode = null, orchNode = null, selectedTask = null;\n"
        + "var chatTier = null, chatDesks = {}, lastHandoff = null;\n"
        + "function focusComposer() {}\n"
        + _chat_section(text)
        + _classroom_section(text)
        + CHAT_DESK_SCENARIO
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    free = "bubble bubble-chat hidden"
    assert out == [
        [1, 0, "row row-sonnet hidden"],
        [["desk-chat-haiku", "desk desk-chat chat-free", free, "", "haiku · chat"], "button",
         "Chat with haiku", 0, 2, "row row-sonnet", "row row-opus"],
        [True,
         ["desk-chat-haiku", "desk desk-chat chat-working", "bubble bubble-chat",
          "Edit: notes.md", "haiku · chat"],
         ["desk-chat-sonnet", "desk desk-chat chat-waiting", "bubble bubble-chat", "your turn",
          "sonnet · chat"]],
        [["haiku"], "desk desk-chat chat-working selected"],
        ["working…", 1, True],
        [["haiku", "sonnet"], "c9"],
        [["haiku", "sonnet"], "c9"],
    ]  # fmt: skip


def test_worker_chat_button_markup_and_wiring():
    text = _text()
    button = '<button id="btn-tl-chat" type="button" class="hidden">Chat</button>'
    assert button in text
    assert text.index('id="tl-title"') < text.index('id="btn-tl-chat"') < text.index('id="tl-meta"')
    for word in [
        "openChatPanel(chatTierOf(t ? t.tier : null), {run_id: current, task_id: selectedTask})",
        "chatSend(currentChat(), chatTier, text, chatSeed)",
    ]:
        assert word in text, word


WORKER_CHAT_SCENARIO = """
var out = [];
out.push(['haiku', 'sonnet', 'opus', null, 'gpt'].map(chatTierOf));
var seed = {run_id: 'r1', task_id: 'demo-a'};
out.push([chatSend(null, 'sonnet', 'fix it', seed),
  chatSend({chat_id: 'c1', tier: 'sonnet', state: 'closed'}, 'sonnet', 'again', seed),
  chatSend({chat_id: 'c2', tier: 'sonnet', state: 'waiting'}, 'sonnet', 'more', seed),
  chatSend(null, 'haiku', 'plain', null)]);
selectedTask = 'b';
runView = {tasks: [{task_id: 'b', title: 'B', tier: 'opus', status: 'failed', steps: []}]};
renderTimeline();
var btn = byId('btn-tl-chat');
out.push([btn.className, btn.text]);
runView.tasks[0].tier = 'haiku';
interactive = false;
renderTimeline();
out.push([btn.className, btn.text]);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_worker_chat_tier_seed_and_button(tmp_path):
    text = _text()
    start = text.index("  // ---- a worker's timeline")
    end = text.index("  // ---- the composer and the plan")
    script = (
        _page_helpers(text)
        + "var runView = null, selectedTask = null, interactive = true;\n"
        + "var tlTask = null, tlFirst = null, tlCount = 0, tlAttempt = null, tlLast = null;\n"
        + "function byId(id) { return document.getElementById(id); }\n"
        + text[start:end]
        + _chat_section(text)
        + WORKER_CHAT_SCENARIO
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    started = {"tier": "sonnet", "text": "fix it", "run_id": "r1", "task_id": "demo-a"}
    assert out == [
        ["haiku", "sonnet", "sonnet", "sonnet", "sonnet"],
        [
            {"path": "/api/chat/start", "body": started},
            {"path": "/api/chat/start",
             "body": {"tier": "sonnet", "text": "again", "run_id": "r1", "task_id": "demo-a"}},
            {"path": "/api/chat/message", "body": {"chat_id": "c2", "text": "more"}},
            {"path": "/api/chat/start", "body": {"tier": "haiku", "text": "plain"}},
        ],
        ["", "Chat with sonnet"],
        ["hidden", "Chat with haiku"],
    ]  # fmt: skip


# ---------------------------------------------------------------- the Orchestrator panel


def test_orchestrator_panel_markup():
    text = _text()
    ids = ["left", "orch-view", "orch-cost", "orch-transcript", "orch-line", "convo",
           "btn-convo-back", "office-title", "timeline-view", "chat-view", "right"]  # fmt: skip
    places = [text.index(f'id="{id_}"') for id_ in ids]
    assert places == sorted(places)
    assert '<div id="orch-view">' in text
    assert '<div id="convo" class="hidden">' in text
    assert '<button id="btn-convo-back" type="button">Back to Orchestrator</button>' in text
    assert text.count(">Back to Orchestrator</button>") == 3
    assert "Back to Opus" not in text
    assert text.index('id="tiers"') < text.index('id="office-total"') < text.index("</header>")
    assert re.search(r"#orch-line:empty \{ display: none; \}", text)
    assert re.search(r"\.msg-orch \{", text)
    for word in [
        "['orch-view', 'convo', 'timeline-view', 'chat-view']",
        "showPanel(selectedTask === null ? 'orch-view' : 'timeline-view')",
        "renderOrch(o.orchestrator);",
        "byId('office-total').textContent = totalText(o);",
        "(empty && !interactive) ? 'hidden' : ''",
    ]:
        assert word in text, word


def test_orchestrator_texts(tmp_path):
    text = _text()
    money = re.search(r"  function money\(x\).*", text)
    string = re.search(r"  function str\(x\).*", text)
    assert money and string
    views = [
        {"cost_usd": 0.0123, "holder": "haiku", "holder_id": "chat-1"},
        {"cost_usd": 0, "holder": None, "holder_id": None},
        {"holder": "opus"},
    ]
    offices = [{"total_cost_usd": 0.5}, {"total_cost_usd": 0}, {}]
    script = (
        money.group(0) + "\n" + string.group(0) + "\n"
        + _functions(text, "orchCostText", "totalText", "orchLine")
        + "var V = " + json.dumps(views) + ", O = " + json.dumps(offices) + ";\n"
        + "console.log(JSON.stringify([V.map(orchCostText), O.map(totalText),"
        + " V.map(orchLine), orchCostText(null), totalText(null), orchLine(null)]));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        ["orchestrator $0.0123", "", ""],
        ["office $0.5000", "", ""],
        ["With haiku (chat-1)", "", "With opus (?)"],
        "", "", "",
    ]  # fmt: skip


def test_orchestrator_message_nodes(tmp_path):
    routed = {"role": "orchestrator", "who": "Orchestrator", "text": "Sent to haiku: x",
              "blocks": parse_markdown("Sent to haiku: x")}  # fmt: skip
    reply = {"role": "agent", "who": "haiku", "text": "Done", "blocks": parse_markdown("Done")}
    script = (
        _transcript_script()
        + f"console.log(JSON.stringify(ser(msgNode({json.dumps(routed)}))));\n"
        + f"console.log(JSON.stringify(ser(msgNode({json.dumps(reply)}, 'haiku'))));\n"
    )
    lines = _run_node(tmp_path, script)
    assert json.loads(lines[0]) == (
        '<li class="msg msg-orch"><div class="msg-who">Orchestrator</div>'
        '<div class="md"><p>Sent to haiku: x</p></div></li>'
    )
    assert json.loads(lines[1]) == (
        '<li class="msg msg-agent"><div class="msg-who">haiku</div>'
        '<div class="md"><p>Done</p></div></li>'
    )


ORCH_SCENARIO = """
function byId(id) { return document.getElementById(id); }
var left = byId('left');
left.scrollHeight = 500; left.clientHeight = 100; left.scrollTop = 370;
function items() {
  return byId('orch-transcript').children.map(function (li) {
    return li.className + '|' + li.children[0].text;
  });
}
var U = {role: 'user', who: 'You', ts: 't1', text: 'haiku fix it', blocks: []};
var R = {role: 'orchestrator', who: 'Orchestrator', ts: 't2', text: 'Sent to haiku: small',
  blocks: [{type: 'paragraph', inline: [{t: 'text', text: 'Sent to haiku: small'}]}]};
var A = {role: 'agent', who: 'haiku', ts: 't3', text: 'Done',
  blocks: [{type: 'paragraph', inline: [{t: 'text', text: 'Done'}]}]};
var out = [];
renderOrch(null);
out.push([items(), byId('orch-cost').text, byId('orch-line').text]);
renderOrch({thread_id: 'orch-1', transcript: [U, R], holder: null, cost_usd: 0.01});
var first = byId('orch-transcript').children[0];
out.push([items(), byId('orch-cost').text, byId('orch-line').text, left.scrollTop]);
left.scrollTop = 0;
renderOrch({thread_id: 'orch-1', transcript: [U, R, A], holder: 'haiku', holder_id: 'c1',
  cost_usd: 0.01});
out.push([items(), byId('orch-transcript').children[0] === first, byId('orch-line').text,
  left.scrollTop, hidden['orch-route']]);
renderOrch({thread_id: 'orch-2', transcript: [U], holder: null, cost_usd: 0.02});
out.push([items(), byId('orch-transcript').children[0] === first, byId('orch-cost').text]);
out.push([hidden['orch-composer'], hidden['orch-route']]);
renderOrch({thread_id: null, transcript: [], cost_usd: 0.02});
out.push([items(), byId('orch-line').text, hidden['orch-route']]);
interactive = false;
renderOrch(null);
out.push(hidden['orch-composer']);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_orchestrator_thread_renders_in_place(tmp_path):
    text = _text()
    money = re.search(r"  function money\(x\).*", text)
    msg = re.search(r"  function msgNode\(.*?\n  \}", text, re.S)
    assert money and msg
    start = text.index("  // ---- markdown block tree")
    end = text.index("  function fill(")
    orch_start = text.index("  // ---- the Orchestrator's thread")
    orch_end = text.index("  // ---- a worker's timeline")
    script = (
        _page_helpers(text) + money.group(0) + "\n" + text[start:end] + msg.group(0) + "\n"
        + "var orchShown = null, orchCount = 0, interactive = true, hidden = {};\n"
        + "function setHidden(id, hide) { hidden[id] = hide; }\n"
        + "document.createDocumentFragment = function () { return mk('#frag'); };\n"
        + text[orch_start:orch_end] + ORCH_SCENARIO
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    you, routed = "msg msg-user|You", "msg msg-orch|Orchestrator"
    assert out == [
        [[], "", ""],
        [[you, routed], "orchestrator $0.0100", "", 500],
        [[you, routed, "msg msg-agent|haiku"], True, "With haiku (c1)", 0, True],
        [[you], False, "orchestrator $0.0200"],
        [False, False],
        [[], "", False],
        True,
    ]  # fmt: skip


def test_back_buttons_and_opus_desk_lead_to_the_right_panel(tmp_path):
    text = _text()
    script = (
        _page_helpers(text)
        + "var office = {can_click: true}, selectedTask = 'a', chatTier = 'haiku';\n"
        + "var tlTask = 'a', hidden = {}, focused = 0, rendered = 0, timeline = 0;\n"
        + "function setHidden(id, hide) { hidden[id] = hide; }\n"
        + "function renderOffice() { rendered++; }\n"
        + "function renderTimeline() { timeline++; }\n"
        + "function focusComposer() { focused++; }\n"
        + _functions(text, "selectTask")
        + _chat_section(text)
        + "var out = [];\n"
        + "showOpus();\n"
        + "out.push([JSON.parse(JSON.stringify(hidden)), selectedTask, chatTier, tlTask,"
        + " focused]);\n"
        + "selectTask('b');\n"
        + "out.push([hidden['timeline-view'], hidden['orch-view']]);\n"
        + "selectTask(null);\n"
        + "out.push([JSON.parse(JSON.stringify(hidden)), selectedTask]);\n"
        + "office = {can_click: false};\n"
        + "showOpus();\n"
        + "out.push([hidden.convo, focused]);\n"
        + "console.log(JSON.stringify(out));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    shown = {"orch-view": False, "convo": True, "timeline-view": True, "chat-view": True}
    assert out == [
        [{"orch-view": True, "convo": False, "timeline-view": True, "chat-view": True},
         None, None, None, 1],
        [False, True],
        [shown, None],
        [False, 1],
    ]  # fmt: skip
    back = "byId('btn-convo-back').addEventListener('click', function () { selectTask(null); });"
    assert back in text


# ---------------------------------------------------------------- the Orchestrator and Opus desks


def test_orchestrator_desk_css():
    text = _text()
    for rule in [
        r"\.desk-orch \{ cursor: pointer; \}", r"\.desk-orch \.worker \{ display: block; \}",
        r"\.orch-idle \.desk-top \{", r"\.orch-routing \.desk-top \{",
        r"\.orch-failed \.desk-top \{",
        r"\.orch-routing \.mark::before \{[^}]*animation: think ", r"\.bubble-orch \{",
    ]:  # fmt: skip
        assert re.search(rule, text), rule


DESKS_SCENARIO = """
var selected = [], focused = 0;
function selectTask(id) { selected.push(id); }
nodes['orch-text'] = mk('textarea');
nodes['orch-text'].focus = function () { focused++; };
function info(d) { return [d.className, d.children[0].className, d.children[0].text]; }
function opusRow() {
  return nodes['desks-opus'].children.map(function (d) { return d.id || d.attrs['data-task']; });
}
var out = [];
renderOffice();
var orch = nodes.teacher.children[0];
out.push([nodes.teacher.children.length, orch.id, orch.children[4].text, orch.attrs.role,
  orch.attrs.title, orch.tabIndex, info(orch), opusRow(), bossNode.children[4].text]);
office = {state: 'idle', orchestrator: {state: 'routing', progress: [], holder: null}};
renderOffice();
out.push(info(orch));
office.orchestrator.progress = ['Read: README.md', 'Grep: TODO'];
renderOffice();
out.push(info(orch));
office.orchestrator = {state: 'idle', progress: [], holder: 'sonnet'};
renderOffice();
out.push(info(orch));
office.orchestrator = {state: 'failed', progress: [], holder: 'sonnet'};
renderOffice();
out.push([info(orch), nodes.teacher.children[0] === orch, nodes.teacher.children.length]);
orch.on.click();
orch.on.keydown({key: 'Enter', preventDefault: function () {}});
out.push([selected, focused]);
runView = {tasks: [{task_id: 'x', tier: 'opus', status: 'running', steps: []}]};
renderOffice();
out.push(opusRow());
nodes['desks-opus'].appendChild(bossNode);
out.push(opusRow());
renderOffice();
out.push([opusRow(), nodes['row-opus'].className]);
console.log(JSON.stringify(out));
"""  # fmt: skip


def test_orchestrator_desk_and_opus_desk(tmp_path):
    text = _text()
    script = (
        _page_helpers(text)
        + "var office = null, interactive = true, runView = null;\n"
        + "var deskNodes = {}, bossNode = null, orchNode = null, selectedTask = null;\n"
        + "var chatTier = null, chatDesks = {}, lastHandoff = null;\n"
        + "function focusComposer() {}\n"
        + _chat_section(text)
        + _classroom_section(text)
        + DESKS_SCENARIO
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        [1, "desk-orch", "Orchestrator", "button", "Talk to the Orchestrator", 0,
         ["desk desk-orch orch-idle", "bubble bubble-orch hidden", ""], ["desk-opus"],
         "opus · agent"],
        ["desk desk-orch orch-routing", "bubble bubble-orch", "routing…"],
        ["desk desk-orch orch-routing", "bubble bubble-orch", "Grep: TODO"],
        ["desk desk-orch orch-idle", "bubble bubble-orch", "→ sonnet"],
        [["desk desk-orch orch-failed", "bubble bubble-orch", "failed"], True, 1],
        [[None, None], 2],
        ["desk-opus", "x"],
        ["x", "desk-opus"],
        [["desk-opus", "x"], "row row-opus"],
    ]  # fmt: skip


# ---------------------------------------------------------------- the Orchestrator composer

ORCH_ROUTE_SELECT = """        <select id="orch-route" aria-label="Who takes it">
          <option value="orchestrator">Orchestrator decides</option>
          <option value="quick">Opus: quick plan</option>
          <option value="project">Opus: project plan</option>
        </select>"""


def test_orchestrator_composer_markup():
    text = _text()
    assert '<div id="orch-composer" class="hidden">' in text
    ids = ["orch-view", "orch-line", "orch-composer", "orch-msg", "orch-text", "orch-route",
           "btn-orch-send", "btn-orch-new", "convo"]  # fmt: skip
    places = [text.index(f'id="{id_}"') for id_ in ids]
    assert places == sorted(places)
    assert ORCH_ROUTE_SELECT in text
    assert '<button id="btn-orch-send" type="button">Send</button>' in text
    assert '<button id="btn-orch-new" type="button">New request</button>' in text
    assert re.search(r"#orch-msg:empty \{ display: none; \}", text)
    for word in [
        "'/api/orchestrator/message'", "'/api/orchestrator/new'", "wireOrch();",
        "'#composer button, #chat-view button, #orch-view button'",
        "setHidden('orch-composer', !interactive);", "setHidden('orch-route', !!o.holder);",
    ]:  # fmt: skip
        assert word in text, word
    for gone in ["Opus routed this", "Auto (Opus decides)", '<option value="auto">']:
        assert gone not in text, gone


def test_orchestrator_send_picks_the_endpoint(tmp_path):
    script = (
        _functions(_text(), "orchSend")
        + "console.log(JSON.stringify([orchSend(null, 'orchestrator', 'a'),"
        + " orchSend({holder: null}, 'quick', 'b'), orchSend({holder: null}, 'project', 'c'),"
        + " orchSend({holder: 'haiku'}, 'quick', 'd'), orchSend(null, 'auto', 'e')]));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        {"path": "/api/orchestrator/message", "body": {"text": "a"}},
        {"path": "/api/planner/start", "body": {"description": "b", "route": "quick"}},
        {"path": "/api/planner/start", "body": {"description": "c", "route": "project"}},
        {"path": "/api/orchestrator/message", "body": {"text": "d"}},
        {"path": "/api/orchestrator/message", "body": {"text": "e"}},
    ]


# ---------------------------------------------------------------- the launcher: project browser

PROJECT_IDS = [
    "project-name", "btn-switch-project", "project-view", "fs-path", "btn-fs-go", "btn-fs-up",
    "fs-hidden", "fs-list", "btn-fs-open", "recent-list", "project-info", "project-msg",
]  # fmt: skip


def test_project_view_ids_labels_and_endpoints():
    text = _text()
    for id_ in PROJECT_IDS:
        assert f'id="{id_}"' in text, id_
    assert '<button id="btn-fs-open" type="button">Open this folder</button>' in text
    assert (
        '<button id="btn-switch-project" type="button" class="hidden">Switch project</button>'
        in text
    )
    for word in [
        "'/api/project'",
        "'/api/project/browse'",
        "'/api/project/open'",
        "location.reload()",
    ]:
        assert word in text, word
    # a plain `harness ui` page never shows the launcher's controls
    assert '<span id="project-name" class="chip hidden"></span>' in text
    assert '<section id="project-view" class="hidden">' in text
    assert text.index('id="project-view"') > text.index("</main>")


def test_project_view_is_not_an_anchor_and_has_no_urls():
    text = _text()
    assert "<a " not in text and "http://" not in text and "https://" not in text


def test_project_summary(tmp_path):
    infos = [
        None,
        {"name": "p", "path": "/x/p", "is_git": False, "has_commit": False, "branch": None,
         "on_main": False, "dirty": False, "origin_path": None},
        {"name": "p", "path": "/x/p", "is_git": True, "has_commit": False, "branch": "main",
         "on_main": True, "dirty": False, "origin_path": None},
        {"name": "p", "path": "/x/p", "is_git": True, "has_commit": True, "branch": "main",
         "on_main": True, "dirty": False, "origin_path": None},
        {"name": "p", "path": "/x/p", "is_git": True, "has_commit": True, "branch": "dev",
         "on_main": False, "dirty": True, "origin_path": "/x/o"},
        {"name": "p", "path": "/x/p", "is_git": True, "has_commit": True, "branch": None,
         "on_main": False, "dirty": False, "origin_path": None},
    ]  # fmt: skip
    script = (
        "function str(x) { return (x === null || x === undefined) ? '?' : String(x); }\n"
        + _functions(_text(), "projectSummary")
        + "console.log(JSON.stringify(" + json.dumps(infos) + ".map(projectSummary)));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        "No project is open.",
        "p · /x/p · not a git repository",
        "p · /x/p · git, no commit yet",
        "p · /x/p · branch main",
        "p · /x/p · branch dev, not on main, uncommitted changes, origin /x/o",
        "p · /x/p · detached HEAD, not on main",
    ]


BROWSER_SCENARIO = """
var out = [], browsed = [], fsDir = null;
function onKey(fn) {
  return function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fn(); } };
}
function browse(path) { browsed.push(path); }
function byId(id) { return document.getElementById(id); }
nodes['btn-fs-up'] = mk('button');
renderListing({path: '/a/b', parent: '/a', truncated: false, entries: [
  {name: 'x', path: '/a/b/x', is_git: false}, {name: '<i>', path: '/a/b/<i>', is_git: true}]});
var rows = nodes['fs-list'].children;
out.push([nodes['fs-path'].value, fsDir, nodes['btn-fs-up'].disabled,
          nodes['btn-fs-up'].attrs['data-parent'], rows.length]);
out.push(rows.map(function (r) {
  return [r.text, r.children.map(function (c) { return c.text; }), r.attrs.role, r.tabIndex];
}));
rows[1].on.click();
rows[0].on.keydown({key: 'Enter', preventDefault: function () {}});
rows[0].on.keydown({key: 'x', preventDefault: function () {}});
out.push(browsed);
renderListing({path: '/', parent: null, truncated: true, entries: []});
out.push([nodes['btn-fs-up'].disabled,
          nodes['fs-list'].children.map(function (r) { return r.text; })]);
console.log(JSON.stringify(out));
"""


def test_folder_listing_renders_rows_that_browse_into_folders(tmp_path):
    text = _text()
    script = _page_helpers(text) + _functions(text, "renderListing") + BROWSER_SCENARIO
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out[0] == ["/a/b", "/a/b", False, "/a", 2]
    assert out[1] == [["x", [], "button", 0], ["<i>", ["git"], "button", 0]]
    assert out[2] == ["/a/b/<i>", "/a/b/x"]
    assert out[3] == [
        True,
        ["No subfolders", "Only the first folders are shown; type a path to go further."],
    ]


# ---------------------------------------------------------------- run options and Stop run

OPTION_IDS = [
    "run-options", "opt-review", "opt-parallel", "opt-max-cost", "opt-fake", "opt-summary",
    "opt-msg", "btn-stop-run",
]  # fmt: skip


def test_run_options_ids_labels_and_endpoints():
    text = _text()
    for id_ in OPTION_IDS:
        assert f'id="{id_}"' in text, id_
    assert '<button id="btn-stop-run" type="button" class="hidden">Stop run</button>' in text
    assert '<div id="run-options" class="hidden">' in text
    for word in [
        "'/api/run/options'", "'/api/run/stop'", "Cost so far: ", "Run options: ",
        "Running tasks are paused; Resume run continues them.", "wireOptions();",
    ]:  # fmt: skip
        assert word in text, word
    assert text.count('<details class="fold">') == 2


def test_options_body(tmp_path):
    cases = [
        [False, "4", "", False], [True, " 2 ", "1.5", True], [False, "4", "$3", False],
        [False, "0", "", False], [False, "17", "", False], [False, "2.5", "", False],
        [False, "", "", False], [False, "4", "0", False], [False, "4", "abc", False],
        [False, "4", "-1", False], [False, 16, 0.25, False],
    ]  # fmt: skip
    script = (
        _functions(_text(), "optionsBody")
        + "var cases = " + json.dumps(cases) + ";\n"
        + "console.log(JSON.stringify(cases.map(function (c) {"
        + " return optionsBody(c[0], c[1], c[2], c[3]); })));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    parallel = "Parallel must be a whole number from 1 to 16."
    cap = "The cost cap must be a number above 0, or empty for no cap."
    assert out == [
        {"body": {"review": False, "max_parallel": 4, "max_cost": None, "fake": False}},
        {"body": {"review": True, "max_parallel": 2, "max_cost": 1.5, "fake": True}},
        {"body": {"review": False, "max_parallel": 4, "max_cost": 3, "fake": False}},
        {"error": parallel}, {"error": parallel}, {"error": parallel}, {"error": parallel},
        {"error": cap}, {"error": cap}, {"error": cap},
        {"body": {"review": False, "max_parallel": 16, "max_cost": 0.25, "fake": False}},
    ]  # fmt: skip


def test_launch_and_stop_confirm_texts(tmp_path):
    script = (
        "function str(x) { return (x === null || x === undefined) ? '?' : String(x); }\n"
        "function money(x) { return (x === null || x === undefined || isNaN(Number(x))) ? '$?'"
        " : '$' + Number(x).toFixed(4); }\n"
        + _functions(_text(), "launchConfirm", "stopConfirm")
        + "var o = {options_summary: 'review off · 4 parallel · no cost cap · real workers',"
        + " total_cost_usd: 0.0793};\n"
        + "console.log(JSON.stringify([launchConfirm('Run it?', o), launchConfirm('Run it?', null),"
        + " stopConfirm('r1')]));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        "Run it?\n\nRun options: review off · 4 parallel · no cost cap · real workers\n"
        "Cost so far: $0.0793",
        "Run it?\n\nRun options: ?\nCost so far: $?",
        "Stop run r1? Running tasks are paused; Resume run continues them.",
    ]


OPTIONS_SCENARIO = """
var out = [], hidden = {};
var interactive = true, fakeLocked = false;
function byId(id) { return document.getElementById(id); }
function setHidden(id, h) { hidden[id] = h; }
document.activeElement = null;
['opt-review', 'opt-fake'].forEach(function (id) { nodes[id] = mk('input'); });
['opt-parallel', 'opt-max-cost', 'opt-summary', 'btn-stop-run'].forEach(function (id) {
  nodes[id] = mk('input');
});
var o = {run_options: {review: true, max_parallel: 3, max_cost: 1.5, fake: false},
         options_summary: 'S', stoppable_run: 'r9'};
renderOptions(o, false);
out.push([nodes['opt-review'].checked, nodes['opt-parallel'].value, nodes['opt-max-cost'].value,
          nodes['opt-fake'].checked, nodes['opt-summary'].text, hidden['btn-stop-run'],
          nodes['btn-stop-run'].attrs.title, hidden['run-options']]);
nodes['opt-parallel'].value = '9';
document.activeElement = nodes['opt-parallel'];
renderOptions({run_options: {review: false, max_parallel: 1, max_cost: null, fake: true},
               options_summary: 'T', stoppable_run: null}, false);
out.push([nodes['opt-parallel'].value, nodes['opt-review'].checked, nodes['opt-max-cost'].value,
          nodes['opt-fake'].checked, hidden['btn-stop-run'], nodes['btn-stop-run'].attrs.title]);
renderOptions({run_options: {review: false, max_parallel: 1, max_cost: null, fake: true},
               options_summary: 'T'}, true);
out.push([nodes['opt-parallel'].value, hidden['btn-stop-run']]);
fakeLocked = true;
renderOptions({run_options: {review: false, max_parallel: 1, max_cost: null, fake: false},
               options_summary: 'T'}, true);
out.push([nodes['opt-fake'].checked, nodes['opt-fake'].disabled]);
interactive = false;
renderOptions(o, true);
out.push([hidden['run-options'], hidden['btn-stop-run']]);
console.log(JSON.stringify(out));
"""


def test_render_options_follows_the_server_but_not_the_field_being_edited(tmp_path):
    text = _text()
    script = _page_helpers(text) + _functions(text, "renderOptions") + OPTIONS_SCENARIO
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        [True, "3", "1.5", False, "S", False, "Stop run r9", False],
        ["9", False, "", True, True, ""],
        ["1", True],
        [True, True],
        [True, True],
    ]
