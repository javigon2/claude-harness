import json
import re

from test_ui_page import _functions, _run_node, _text
from test_ui_page_attach import _helpers

FUNCS = (
    "syncSend", "composerCap", "fitComposer", "composerChanged", "clearComposer", "loadManual",
    "saveManual", "wireGrip", "wireComposers",
)  # fmt: skip

PRELUDE = """
var els = {}, boxes = {}, grips = {}, store = {}, storeBroken = false, windowHandlers = {};
function mkEl(tag) {
  var e = {
    tag: tag, value: '', disabled: false, style: {}, scrollHeight: 0, offsetHeight: 30,
    handlers: {}, clicks: 0, classes: [],
    addEventListener: function (n, f) { this.handlers[n] = f; },
    click: function () { this.clicks++; },
    classList: {
      add: function (c) { e.classes.push(c); },
      remove: function (c) { e.classes = e.classes.filter(function (x) { return x !== c; }); }
    }
  };
  return e;
}
function byId(id) { if (!els[id]) els[id] = mkEl('x'); return els[id]; }
var document = {
  querySelector: function (sel) {
    var id = /data-for="([^"]+)"/.exec(sel)[1];
    if (!boxes[id]) {
      var box = mkEl('div'), grip = mkEl('div');
      box.querySelector = function () { return grip; };
      boxes[id] = box;
      grips[id] = grip;
    }
    return boxes[id];
  }
};
var window = {
  innerHeight: 1000,
  localStorage: {
    getItem: function (k) { if (storeBroken) throw new Error('blocked'); return store[k] || null; },
    setItem: function (k, v) { if (storeBroken) throw new Error('blocked'); store[k] = v; },
    removeItem: function (k) { if (storeBroken) throw new Error('blocked'); delete store[k]; }
  },
  addEventListener: function (n, f) { windowHandlers[n] = f; }
};
"""


def _script(body: str) -> str:
    text = _text()
    fns = "\n".join(_functions(text, n) for n in FUNCS)
    pre = _helpers().replace("var pending = {};", "var pending = {}; var uploading = {};")
    state = "var manualH = {}; var COMPOSER_KEY = 'harness.composer.';\n"
    stub = "function clearPending(id) { pending[id] = []; }\n"
    return pre + "\n" + PRELUDE + state + stub + fns + body


def _run(tmp_path, body: str):
    return json.loads(_run_node(tmp_path, _script(body))[-1])


def test_send_waits_for_text_uploads_and_requests(tmp_path):
    out = _run(
        tmp_path,
        """
var out = [];
function state() { syncSend('orch-text'); return byId('btn-orch-send').disabled; }
out.push(state());
byId('orch-text').value = '  \\n ';
out.push(state());
byId('orch-text').value = 'hi';
out.push(state());
uploading['orch-text'] = 1;
out.push(state());
uploading['orch-text'] = 0;
composerBusy = true;
out.push(state());
composerBusy = false;
clearComposer('orch-text');
out.push([byId('orch-text').value, byId('btn-orch-send').disabled]);
console.log(JSON.stringify(out));
""",
    )
    assert out == [True, True, False, True, True, ["", True]]


def test_box_grows_to_forty_percent_then_stops(tmp_path):
    out = _run(
        tmp_path,
        """
var ta = byId('desc'), out = [];
ta.scrollHeight = 24; fitComposer('desc'); out.push(ta.style.height);
ta.scrollHeight = 200; fitComposer('desc'); out.push(ta.style.height);
ta.scrollHeight = 900; fitComposer('desc'); out.push(ta.style.height);
window.innerHeight = 500;
fitComposer('desc'); out.push(ta.style.height);
ta.scrollHeight = 0; fitComposer('desc'); out.push(ta.style.height);
manualH['desc'] = 321; ta.scrollHeight = 24; fitComposer('desc'); out.push(ta.style.height);
console.log(JSON.stringify(out));
""",
    )
    assert out == ["24px", "200px", "400px", "200px", "", "321px"]


def test_manual_height_is_kept_in_local_storage_and_survives_blocked_storage(tmp_path):
    out = _run(
        tmp_path,
        """
var out = [];
manualH['answer'] = 180; saveManual('answer');
out.push(store['harness.composer.answer']);
manualH = {}; loadManual('answer'); out.push(manualH['answer']);
manualH['answer'] = 0; saveManual('answer'); out.push('harness.composer.answer' in store);
store['harness.composer.answer'] = 'junk'; manualH = {}; loadManual('answer');
out.push(manualH['answer'] === undefined);
storeBroken = true;
manualH['answer'] = 90; saveManual('answer'); loadManual('desc');
out.push('survived');
console.log(JSON.stringify(out));
""",
    )
    assert out == ["180", 180, False, True, "survived"]


def test_enter_sends_and_shift_enter_adds_a_line(tmp_path):
    out = _run(
        tmp_path,
        """
wireComposers();
var ta = byId('chat-text'), btn = byId('btn-chat-send'), out = [];
function key(e) {
  var prevented = false;
  e.preventDefault = function () { prevented = true; };
  ta.handlers.keydown(e);
  return prevented;
}
out.push(btn.disabled);
out.push([key({key: 'Enter'}), btn.clicks]);
ta.value = 'go'; ta.handlers.input();
out.push(btn.disabled);
out.push([key({key: 'Enter', shiftKey: true}), btn.clicks]);
out.push([key({key: 'Enter', isComposing: true}), btn.clicks]);
out.push([key({key: 'a'}), btn.clicks]);
out.push([key({key: 'Enter'}), btn.clicks]);
console.log(JSON.stringify(out));
""",
    )
    assert out == [True, [True, 0], False, [False, 0], [False, 0], [False, 0], [True, 1]]


def test_grip_drag_resizes_and_double_click_resets(tmp_path):
    out = _run(
        tmp_path,
        """
wireComposers();
var g = grips['continue'];
var ta = byId('continue'), out = [];
ta.offsetHeight = 60;
g.handlers.pointerdown({clientY: 500, pointerId: 1, preventDefault: function () {}});
g.handlers.pointermove({clientY: 440});
out.push([ta.style.height, boxes['continue'].classes]);
g.handlers.pointermove({clientY: 5000});
out.push(ta.style.height);
g.handlers.pointermove({clientY: -5000});
out.push(ta.style.height);
g.handlers.pointerup({});
out.push([store['harness.composer.continue'], boxes['continue'].classes]);
g.handlers.dblclick();
out.push(['harness.composer.continue' in store, manualH['continue']]);
console.log(JSON.stringify(out));
""",
    )
    assert out == [
        ["120px", ["resizing"]], "32px", "800px", ["800", []], [False, 0],
    ]  # fmt: skip


def test_composer_css_and_markup_guards():
    text = _text()
    assert "wireComposers();" in text
    assert re.search(r"localStorage", text)
    # every localStorage touch sits in a try block
    for m in re.finditer(r"window\.localStorage", text):
        assert "try {" in text[max(0, m.start() - 120) : m.start()]
    assert re.search(r"\.composer-grip \{[^}]*cursor: ns-resize;", text)
    assert "#left .composer-end { margin-left: auto;" in text
    assert text.count("orch-route") == 0
    assert 'id="route"' in text  # the Opus desk keeps its Quick/Full select
