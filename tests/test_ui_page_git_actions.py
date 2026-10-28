import json
import re

from test_ui_page import _functions, _page_helpers, _run_node, _text

STUBS = """
var posts = [], loads = 0;
function byId(id) { return document.getElementById(id); }
function errorOf(res) { return 'err ' + res.status; }
function gitLoad() { loads++; }
function gitDiff() {}
function onKey(fn) { return fn; }
function gitPost(path, body, done) { posts.push([path, body, done]); }
"""


def _script(body: str, *names: str) -> str:
    text = _text()
    return _page_helpers(text) + STUBS + _functions(text, *names) + body


def _view(text: str) -> str:
    m = re.search(r'<section id="git-view".*?</section>', text, re.S)
    assert m
    return m.group(0)


def test_markup_and_css():
    text = _text()
    view = _view(text)
    assert '<p id="git-msg" role="alert"></p>\n  <p id="git-note" role="status"></p>' in view
    box = re.search(r'<div id="git-commit-box" class="hidden">(.*?)</div>', view, re.S)
    assert box
    assert '<textarea id="git-commit-msg"' in box.group(1)
    assert 'id="btn-git-commit"' in box.group(1)
    assert view.index('id="git-commit-box"') < view.index('id="git-changes"')
    assert "#git-note:empty { display: none; }" in text
    assert "#git-commit-box:not(.hidden)" in text


GITPOST = """
var mode = 'ok', gitCalls = [];
function api(path, body) {
  if (mode === 'reject') return Promise.reject(new Error('x'));
  return Promise.resolve(mode === 'ok' ? {ok: true, data: {sha: 's'}} : {ok: false, status: 409});
}
function run(m) {
  mode = m;
  byId('git-note').textContent = 'old note';
  byId('git-msg').textContent = 'old';
  loads = 0;
  var done = [];
  gitPostReal('/p', {}, function (d) { done.push(d.sha); });
  var cleared = byId('git-note').textContent === '';
  return new Promise(function (r) {
    setTimeout(function () {
      r([m, cleared, byId('git-msg').textContent, done, loads]);
    }, 5);
  });
}
run('ok').then(function (a) { out.push(a); return run('bad'); }).then(function (a) {
  out.push(a); return run('reject');
}).then(function (a) { out.push(a); console.log(JSON.stringify(out)); });
"""


def test_git_post(tmp_path):
    text = _text()
    body = "var out = [];\n" + GITPOST.replace("gitPostReal", "gitPost")
    script = (
        _page_helpers(text)
        + STUBS.replace(
            "function gitPost(path, body, done) { posts.push([path, body, done]); }", ""
        )
        + _functions(text, "gitPost")
        + body
    )
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        ["ok", True, "", ["s"], 1],
        ["bad", True, "err 409", [], 0],
        ["reject", True, "Connection lost; try again.", [], 0],
    ]


def test_git_act(tmp_path):
    body = """
var n = 0, s = 0;
var b = gitAct('Go', function () { n++; });
b.on.click({stopPropagation: function () { s++; }});
var k = 0;
b.on.keydown({stopPropagation: function () { k++; }});
console.log(JSON.stringify([b.className, b.text, b.type, n, s, k]));
"""
    out = json.loads(_run_node(tmp_path, _script(body, "gitAct"))[-1])
    assert out == ["git-act", "Go", "button", 1, 1, 1]


def test_render_changes_buttons(tmp_path):
    body = """
renderGitChanges({branch: 'main', entries: [
  {conflicted: true, staged: 'U', unstaged: 'U', path: 'c.txt'},
  {staged: 'R', unstaged: ' ', old_path: 'old.txt', path: 'new.txt'},
  {staged: ' ', unstaged: 'M', path: 'm.txt'},
  {untracked: true, staged: '?', unstaged: '?', path: 'u.txt'}
]});
var kids = nodes['git-changes'].children;
var h3s = kids.filter(function (k) { return k.tag === 'h3'; });
var uls = kids.filter(function (k) { return k.tag === 'ul'; });
function btn(li) { return li.children[li.children.length - 1]; }
var labels = uls.map(function (u) { return btn(u.children[0]).text; });
var heads = h3s.map(function (h) { return [h.text, h.children[0].text]; });
btn(uls[1].children[0]).on.click({stopPropagation: function () {}});
btn(uls[3].children[0]).on.click({stopPropagation: function () {}});
h3s[1].children[0].on.click({stopPropagation: function () {}});
h3s[3].children[0].on.click({stopPropagation: function () {}});
console.log(JSON.stringify([labels, heads, posts.map(function (p) { return [p[0], p[1]]; })]));
"""
    out = json.loads(
        _run_node(tmp_path, _script(body, "gitAct", "renderGitChanges", "gitPathsOf", "gitStage"))[
            -1
        ]
    )
    assert out[0] == ["Stage", "Unstage", "Stage", "Stage"]
    assert out[1] == [
        ["Conflicted (1)", "Stage all"],
        ["Staged (1)", "Unstage all"],
        ["Unstaged (1)", "Stage all"],
        ["Untracked (1)", "Stage all"],
    ]
    unstage = ["/api/git/unstage", {"paths": ["old.txt", "new.txt"]}]
    stage_u = ["/api/git/stage", {"paths": ["u.txt"]}]
    assert out[2] == [unstage, stage_u, unstage, stage_u]


def test_git_commit(tmp_path):
    body = """
byId('git-commit-msg').value = '  ';
gitCommit();
var a = [byId('git-msg').textContent, posts.length];
byId('git-commit-msg').value = 'fix it';
gitCommit();
var p = posts[0];
p[2]({sha: 'abc1234'});
console.log(JSON.stringify([a, [p[0], p[1]], byId('git-commit-msg').value,
  byId('git-note').textContent]));
"""
    out = json.loads(_run_node(tmp_path, _script(body, "gitCommit"))[-1])
    assert out == [
        ["Write a commit message first.", 0],
        ["/api/git/commit", {"message": "fix it"}],
        "",
        "Committed abc1234.",
    ]


def test_show_tab_commit_box(tmp_path):
    body = """
var hidden = {};
function setHidden(id, hide) { hidden[id] = hide; }
var realById = byId;
function byId(id) {
  var n = document.getElementById(id);
  if (!n.classList) n.classList = {toggle: function () {}};
  return n;
}
var GIT_TABS = ['graph', 'changes', 'branches'];
var gitTab = 'graph';
byId('git-note').textContent = 'old';
gitShowTab('changes');
var a = [hidden['git-commit-box'], byId('git-note').textContent];
gitShowTab('graph');
console.log(JSON.stringify([a, hidden['git-commit-box']]));
"""
    out = json.loads(_run_node(tmp_path, _script(body, "gitShowTab"))[-1])
    assert out == [[False, ""], True]


def test_wire_git_commit():
    text = _text()
    wire = _functions(text, "wireGit")
    assert "byId('btn-git-commit').addEventListener('click', gitCommit);" in wire
