import json
import re

from test_ui_page import _run_node, _text
from test_ui_page_attach import _fn, _helpers

FUNCS = ("uploadImage", "idsOf", "clearPending", "renderThumbs", "addFiles")

PRELUDE = """
var els = {};
function mkEl(tag) {
  var e = {
    tag: tag, className: '', textContent: '', children: [], attrs: {}, handlers: {},
    appendChild: function (c) { this.children.push(c); return c; },
    replaceChildren: function () { this.children = []; },
    setAttribute: function (k, v) { this.attrs[k] = v; },
    addEventListener: function (n, f) { this.handlers[n] = f; }
  };
  return e;
}
var document = {createElement: mkEl};
function byId(id) { if (!els[id]) els[id] = mkEl('x'); return els[id]; }
var token = 'tok';
var uploading = {};
var syncs = [];
function syncSend(id) { syncs.push(id); }
var calls = [];
var mode = 'ok';
function fetch(path, opts) {
  calls.push({path: path, opts: opts});
  if (mode === 'reject') return Promise.reject(new Error('down'));
  var n = calls.length;
  var ext = String(opts.headers['X-Filename']).slice(-3) === '.md' ? '.md' : '.png';
  var body = mode === 'error' ? {error: 'image too large (max 8 MB)'} : {id: 'id' + n + ext};
  return Promise.resolve({
    ok: mode !== 'error',
    text: function () { return Promise.resolve(JSON.stringify(body)); }
  });
}
function tick() { return new Promise(function (r) { setTimeout(r, 20); }); }
pending['desc'] = [];
uploading['desc'] = 0;
"""


def _script(body: str) -> str:
    text = _text()
    fns = "\n".join(_fn(text, n) for n in FUNCS)
    return PRELUDE + "\n" + fns + "\n" + body


def _run(tmp_path, body: str) -> dict:
    script = _helpers() + "\n" + _script(body)
    return json.loads(_run_node(tmp_path, script)[-1])


def test_png_uploads_and_svg_refused(tmp_path):
    out = _run(
        tmp_path,
        """
var svg = {name: 'x.svg', type: 'image/svg+xml', size: 1};
var png = {name: 'b.png', type: 'image/png', size: 1};
addFiles('desc', [svg, png]);
var typeMsg = byId('office-msg').textContent;
tick().then(function () {
  var list = byId('desc-thumbs');
  var li = list.children[0];
  console.log(JSON.stringify({
    typeMsg: typeMsg, calls: calls.length, path: calls[0].path,
    token: calls[0].opts.headers['X-Harness-Token'],
    ct: calls[0].opts.headers['Content-Type'],
    ids: idsOf('desc'), lis: list.children.length, cls: list.className,
    src: li.children[0].src, alt: li.children[0].alt,
    aria: li.children[1].attrs['aria-label']
  }));
});
""",
    )
    assert "only images and text or code files" in out["typeMsg"]
    assert out["calls"] == 1
    assert out["path"] == "/api/upload"
    assert out["token"] == "tok"
    assert out["ct"] == "application/octet-stream"
    assert out["ids"] == ["id1.png"]
    assert out["lis"] == 1
    assert out["cls"] == "thumbs"
    assert out["src"] == "/api/upload/id1.png"
    assert out["alt"] == "b.png"
    assert out["aria"] == "Remove b.png"


def test_server_error_and_rejected_fetch(tmp_path):
    out = _run(
        tmp_path,
        """
var png = {name: 'b.png', type: 'image/png', size: 1};
var res = {};
mode = 'error';
addFiles('desc', [png]);
tick().then(function () {
  res.err = byId('office-msg').textContent;
  mode = 'reject';
  addFiles('desc', [png]);
  return tick();
}).then(function () {
  res.rej = byId('office-msg').textContent;
  res.ids = idsOf('desc');
  console.log(JSON.stringify(res));
});
""",
    )
    assert out["err"] == "b.png: image too large (max 8 MB)"
    assert out["rej"] == "b.png: upload failed."
    assert out["ids"] == []


def test_five_files_four_uploads(tmp_path):
    out = _run(
        tmp_path,
        """
var files = [];
for (var i = 0; i < 5; i++) files.push({name: 'f' + i + '.png', type: 'image/png', size: 1});
addFiles('desc', files);
var msg = byId('office-msg').textContent;
tick().then(function () {
  console.log(JSON.stringify({calls: calls.length, msg: msg, ids: idsOf('desc')}));
});
""",
    )
    assert out["calls"] == 4
    assert out["msg"] == "At most 4 attachments per message."
    assert len(out["ids"]) == 4


def test_remove_button_empties_pending(tmp_path):
    out = _run(
        tmp_path,
        """
addFiles('desc', [{name: 'b.png', type: 'image/png', size: 1}]);
tick().then(function () {
  var list = byId('desc-thumbs');
  var before = idsOf('desc').length;
  list.children[0].children[1].handlers.click();
  console.log(JSON.stringify({
    before: before, after: idsOf('desc').length, cls: list.className,
    lis: list.children.length
  }));
});
""",
    )
    assert out == {"before": 1, "after": 0, "cls": "thumbs hidden", "lis": 0}


def test_wiring_in_page():
    text = _text()
    assert re.search(r"wireOrch\(\);\s*wireAttach\(\);", text)
    assert text.count("withImages(") - 1 >= 4
    assert text.count("clearComposer(") >= 5
    assert "Wait for the files to finish uploading." in text
    for bad in ("http://", "https://", "<a ", "innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert bad not in text
    assert "document.write" not in text and "eval(" not in text
    assert "modal" not in text
    assert text.count('<details class="fold">') == 2


def test_a_markdown_file_uploads_with_its_name_and_shows_a_chip(tmp_path):
    out = _run(
        tmp_path,
        """
var md = {name: 'my notes.md', type: '', size: 5};
addFiles('desc', [md]);
tick().then(function () {
  var li = byId('desc-thumbs').children[0];
  console.log(JSON.stringify({
    name: calls[0].opts.headers['X-Filename'], ids: idsOf('desc'),
    cls: li.children[0].className, text: li.children[0].textContent,
    tag: li.children[0].tag
  }));
});
""",
    )
    assert out["name"] == "my%20notes.md"
    assert out["ids"] == ["id1.md"]
    assert out["cls"] == "file-chip" and out["text"] == "my notes.md"
