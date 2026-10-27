import json
import re

from test_ui_page import _run_node, _text
from test_ui_page import _transcript_script as _base_script

EXTRA = """
var listeners = [];
var _mk = document.createElement;
document.createElement = function (tag) {
  var n = _mk(tag);
  n.src = ''; n.alt = '';
  n.addEventListener = function (type, fn) { listeners.push({ node: n, type: type, fn: fn }); };
  n.classList = { toggle: function (c) { n.toggled = (n.toggled || 0) + 1; n.lastToggle = c; } };
  return n;
};
function ser2(n) {
  if (n.tag === '#text') return n.text;
  var cls = n.className ? ' class="' + n.className + '"' : '';
  var attrs = n.tag === 'img' ? ' src="' + n.src + '" alt="' + n.alt + '"' : '';
  var inner = n.text + n.children.map(ser2).join('');
  return '<' + n.tag + cls + attrs + '>' + inner + '</' + n.tag + '>';
}
"""

OPEN_IMAGE = re.compile(r"  function openImage\(.*?\n  \}", re.S)


def _script() -> str:
    m = OPEN_IMAGE.search(_text())
    assert m
    return _base_script() + EXTRA + m.group(0) + "\n"


def test_user_entry_with_images_renders_thumbs(tmp_path):
    entry = {"role": "user", "text": "look", "blocks": [], "images": ["abc.png"]}
    plain = {"role": "user", "text": "look", "blocks": []}
    script = (
        _script()
        + f"console.log(JSON.stringify(ser2(msgNode({json.dumps(entry)}))));\n"
        + f"console.log(JSON.stringify(ser(msgNode({json.dumps(plain)}))));\n"
    )
    lines = _run_node(tmp_path, script)
    assert json.loads(lines[0]) == (
        '<li class="msg msg-user"><div class="msg-who">You</div>'
        '<div class="msg-text">look</div>'
        '<div class="msg-images"><img class="msg-thumb" src="/api/upload/abc.png"'
        ' alt="attached image"></img></div></li>'
    )
    assert json.loads(lines[1]) == (
        '<li class="msg msg-user"><div class="msg-who">You</div>'
        '<div class="msg-text">look</div></li>'
    )


def test_click_opens_the_dialog_once(tmp_path):
    script = (
        _script()
        + """
var imgEl = { src: '' };
var dlg = { open: false, calls: 0, showModal: function () { this.calls++; this.open = true; } };
document.getElementById = function (id) { return id === 'img-view' ? dlg : imgEl; };
var li = msgNode({ role: 'user', text: 't', images: ['x.png'] });
var thumb = li.children[2].children[0];
var click = listeners.filter(function (l) { return l.node === thumb && l.type === 'click'; })[0];
click.fn();
click.fn();
console.log(JSON.stringify([imgEl.src, dlg.calls]));
"""
    )
    assert json.loads(_run_node(tmp_path, script)[0]) == ["/api/upload/x.png", 1]


def test_without_dialog_the_thumb_toggles_big(tmp_path):
    script = (
        _script()
        + """
document.getElementById = function () { return { }; };
var thumb = { classList: { toggle: function (c) { this.got = c; } } };
openImage('/s', thumb);
console.log(JSON.stringify(thumb.classList.got));
document.getElementById = function () { return null; };
openImage('/s', thumb);
console.log(JSON.stringify(thumb.classList.got));
"""
    )
    lines = _run_node(tmp_path, script)
    assert json.loads(lines[0]) == "big"
    assert json.loads(lines[1]) == "big"


def test_page_has_one_dialog_and_guards_hold():
    text = _text()
    assert text.count('<dialog id="img-view">') == 1
    assert text.count("<dialog") == 1
    for bad in ["http://", "https://", "<a ", "innerHTML", "outerHTML", "eval(", "modal"]:
        assert bad not in text, bad
    assert text.count('<details class="fold">') == 2
