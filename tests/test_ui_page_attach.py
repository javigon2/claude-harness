import json
import re

import pytest
from test_ui_page import _run_node, _text

IDS = ["orch-text", "desc", "answer", "continue", "chat-text"]


ACCEPT = (
    "image/png,image/jpeg,image/gif,image/webp,.md,.txt,.rst,.log,.csv,.tsv,.json,.jsonl,.yaml,"
    ".yml,.toml,.ini,.cfg,.conf,.xml,.html,.css,.scss,.js,.jsx,.ts,.tsx,.mjs,.py,.pyi,.ipynb,"
    ".sh,.bash,.zsh,.sql,.java,.kt,.go,.rs,.c,.h,.cpp,.hpp,.cs,.rb,.php,.swift,.lua,.r,.scala,"
    ".dart,.gradle,.dockerfile,.makefile"
)


SENDS = {
    "orch-text": "btn-orch-send", "desc": "btn-start", "answer": "btn-send",
    "continue": "btn-continue", "chat-text": "btn-chat-send",
}  # fmt: skip


def _block(x: str) -> str:
    return (
        f'<div class="attach hidden" data-for="{x}">'
        f'<input id="{x}-file" type="file" '
        f'accept="{ACCEPT}" multiple class="hidden">'
        f'<button id="{x}-attach" type="button" class="attach-btn" '
        f'aria-label="Attach file" title="Attach file">+</button></div>'
    )


def _fn(text: str, name: str) -> str:
    m = re.search(r"  function " + name + r"\(.*?\n  \}", text, re.S)
    assert m, name
    return m.group(0)


def _helpers() -> str:
    text = _text()
    consts = re.search(r"  var IMAGE_TYPES.*?\n  var pending = \{\};", text, re.S)
    assert consts
    parts = [consts.group(0)]
    parts += [_fn(text, n) for n in ("refuseReason", "imageFiles", "withImages", "attachSupported")]
    return "\n".join(parts)


JS = """
var png = {name: 'a.png', type: 'image/png', size: 1000};
var out = {};
out.small = refuseReason(png, 0);
out.svg = refuseReason({name: 'x.svg', type: 'image/svg+xml', size: 10}, 0);
out.nameless = refuseReason({type: 'text/plain', size: 10}, 0);
out.big = refuseReason({name: 'big.png', type: 'image/png', size: 9 * 1024 * 1024}, 0);
out.full = refuseReason(png, 4);
out.md = refuseReason({name: 'README.md', type: '', size: 2000}, 0);
out.docker = refuseReason({name: 'Dockerfile', type: '', size: 2000}, 0);
out.exe = refuseReason({name: 'run.exe', type: 'application/octet-stream', size: 10}, 0);
out.bigmd = refuseReason({name: 'big.md', type: 'text/markdown', size: 300 * 1024}, 0);

var f1 = {name: 'one.png', type: 'image/png', size: 1};
var f2 = {name: 'two.jpg', type: 'image/jpeg', size: 1};
var list = {length: 2, item: function (i) { return [f1, f2][i]; }};
out.list = imageFiles(list).map(function (f) { return f.name; });
var fileItem = {kind: 'file', getAsFile: function () { return f1; }};
var strItem = {kind: 'string', getAsFile: function () { return f2; }};
out.items = imageFiles([strItem, fileItem]).map(function (f) { return f.name; });
out.nil = imageFiles(null).length;
out.mixed = imageFiles([null, 'str', f2]).map(function (f) { return f.name; });

var body = {text: 'hi'};
out.same = withImages(body, []) === body;
var copy = withImages(body, ['a']);
out.copyImages = copy.images;
out.copyText = copy.text;
out.copyIsNew = copy !== body;
out.inputUntouched = Object.prototype.hasOwnProperty.call(body, 'images');

var good = {
  File: function () {},
  fetch: function () {},
  document: {createElement: function () { return {files: []}; }}
};
out.good = attachSupported(good);
out.noFile = attachSupported({fetch: good.fetch, document: good.document});
out.noFetch = attachSupported({File: good.File, document: good.document});
out.noDoc = attachSupported({File: good.File, fetch: good.fetch});
out.nul = attachSupported(null);
console.log(JSON.stringify(out));
"""


@pytest.mark.parametrize("x", IDS)
def test_composer_box_layout(x):
    # thumbnails, then the text, then one row: attach (+) on the left, send on the right
    text = _text()
    assert _block(x) in text
    box = re.search(
        r'<div class="composer-box" data-for="' + x + r'">.*?\n\s*</div>\n(?=\s*</)', text, re.S
    )
    assert box, x
    body = box.group(0)
    order = [
        f'id="{x}-thumbs"', f'<textarea id="{x}"', '<div class="composer-row">',
        f'id="{x}-attach"', f'id="{SENDS[x]}"',
    ]  # fmt: skip
    places = [body.index(w) for w in order]
    assert places == sorted(places)
    assert 'class="composer-grip"' in body and body.count("<textarea") == 1


def test_one_composer_box_per_textarea():
    text = _text()
    assert text.count('class="composer-box"') == 5
    assert text.count("<textarea") == 5
    assert text.count('class="composer-row"') == 5


def test_attach_blocks_hidden_by_default():
    assert _text().count('class="attach hidden"') == 5


def test_page_guards():
    text = _text()
    assert "http://" not in text
    assert "https://" not in text
    assert "<a " not in text
    assert text.count('<details class="fold">') == 2


def test_attach_helpers_in_node(tmp_path):
    lines = _run_node(tmp_path, _helpers() + "\n" + JS)
    out = json.loads(lines[0])

    assert out["small"] == ""
    assert out["svg"] == "x.svg: only images and text or code files can be attached."
    assert out["nameless"] == "file: only images and text or code files can be attached."
    assert out["big"] == "big.png: larger than 8 MB."
    assert out["full"] == "At most 4 attachments per message."
    assert out["md"] == "" and out["docker"] == ""
    assert out["exe"] == "run.exe: only images and text or code files can be attached."
    assert out["bigmd"] == "big.md: larger than 256 KB."

    assert out["list"] == ["one.png", "two.jpg"]
    assert out["items"] == ["one.png"]
    assert out["nil"] == 0
    assert out["mixed"] == ["two.jpg"]

    assert out["same"] is True
    assert out["copyImages"] == ["a"]
    assert out["copyText"] == "hi"
    assert out["copyIsNew"] is True
    assert out["inputUntouched"] is False

    assert out["good"] is True
    assert out["noFile"] is False
    assert out["noFetch"] is False
    assert out["noDoc"] is False
    assert out["nul"] is False
