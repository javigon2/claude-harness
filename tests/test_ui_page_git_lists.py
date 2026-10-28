from test_ui_page import _functions, _page_helpers, _run_node, _text

STUBS = """
var calls = [], shown = [], gitRemotes = ['origin'];
function byId(id) { return document.getElementById(id); }
function gitGet(path, done) { calls.push([path, done]); }
function loadGitCommit(sha) { shown.push(sha); }
function setClass(n, c) { if (n.className !== c) n.className = c; }
function dump(n) {
  if (n.tag === '#text') return n.text;
  return n.tag + '.' + n.className + '[' + n.text + n.children.map(dump).join('|') + ']';
}
function kids(id) { return nodes[id].children; }
"""


def _script(body: str) -> str:
    text = _text()
    names = (
        "gitTrack", "renderGitBranches", "gitDiff", "renderGitDiff", "renderGitChanges",
        "loadGitBranches", "loadGitChanges", "patchHtml", "relTime", "onKey",
    )  # fmt: skip
    return _page_helpers(text) + STUBS + _functions(text, *names) + body


def test_load_paths(tmp_path):
    body = """
byId('git-show-harness').checked = false;
loadGitBranches();
byId('git-show-harness').checked = true;
loadGitBranches();
loadGitChanges();
console.log(calls.map(function (c) { return c[0]; }).join(' '));
console.log(calls[0][1] === renderGitBranches, calls[2][1] === renderGitChanges);
"""
    out = _run_node(tmp_path, _script(body))
    assert out[0] == "/api/git/branches /api/git/branches?harness=1 /api/git/status"
    assert out[1] == "true true"


def test_git_track(tmp_path):
    body = """
var b = {upstream: null, base: null, ahead: 0, behind: 0, base_ahead: 0, base_behind: 0};
console.log(JSON.stringify(gitTrack(b)));
b.upstream = 'origin/x'; b.ahead = 2; b.behind = 1;
console.log(gitTrack(b));
b.upstream = null; b.base = 'main'; b.base_ahead = 3; b.base_behind = 4;
console.log(gitTrack(b));
b.upstream = 'origin/x';
console.log(gitTrack(b));
"""
    out = _run_node(tmp_path, _script(body))
    assert out[0] == '""'
    assert out[1] == "↑2 ↓1 vs origin/x"
    assert out[2] == "↑3 ↓4 vs base main"
    assert out[3] == "↑2 ↓1 vs origin/x · ↑3 ↓4 vs base main"


def test_render_branches(tmp_path):
    body = """
function B(name, kind, extra) {
  var b = {name: name, kind: kind, sha: 'sha-' + name, subject: 's ' + name, time: 0,
    current: false, upstream: null, ahead: 0, behind: 0, base: null, base_ahead: 0,
    base_behind: 0, work: false};
  for (var k in extra) b[k] = extra[k];
  return b;
}
renderGitBranches({detached: true, remotes: ['origin', 'up'], branches: [
  B('main', 'local', {current: true, upstream: 'origin/main', ahead: 1, behind: 2}),
  B('work/a', 'local', {base: 'main', base_ahead: 5, base_behind: 0, work: true}),
  B('origin/main', 'remote')]});
var k = kids('git-branches');
console.log(k.map(function (n) { return n.tag + ':' + n.text; }).join(','));
console.log(gitRemotes.join());
var rows = k[2].children;
console.log(rows.map(function (r) { return r.className; }).join(','));
console.log(rows[0].children.map(function (c) { return c.text; }).join('|'));
console.log(rows[1].children[1].text);
console.log(rows[0].attrs.role, rows[0].tabIndex);
rows[1].on.click();
rows[0].on.keydown({key: 'Enter', preventDefault: function () {}});
console.log(shown.join());
console.log(k[4].children[0].children[0].text);
renderGitBranches({detached: false, remotes: [], branches: []});
console.log(kids('git-branches').map(function (n) { return n.text; }).join());
"""
    out = _run_node(tmp_path, _script(body))
    assert out[0] == "p:HEAD is detached.,h3:Local,ul:,h3:Remote,ul:"
    assert out[1] == "origin,up"
    assert out[2] == "git-branch current,git-branch"
    parts = out[3].split("|")
    assert parts[:3] == ["main", "↑1 ↓2 vs origin/main", "s main"]
    assert out[4] == "↑5 ↓0 vs base main"
    assert out[5] == "button 0"
    assert out[6] == "sha-work/a,sha-main"
    assert out[7] == "origin/main"
    assert out[8] == "No branches yet."


def test_render_changes(tmp_path):
    body = """
function E(path, x, y, extra) {
  var e = {path: path, old_path: null, staged: x, unstaged: y, untracked: false,
    conflicted: false};
  for (var k in extra) e[k] = extra[k];
  return e;
}
renderGitChanges({branch: 'main', detached: false, merging: true, entries: [
  E('a b.txt', 'M', 'M'), E('new.txt', 'R', ' ', {old_path: 'old.txt'}),
  E('u.txt', '?', '?', {untracked: true}), E('c.txt', 'U', 'U', {conflicted: true})]});
var k = kids('git-changes');
console.log(k.map(function (n) { return n.text; }).join(','));
function rowsOf(i) { return k[i].children.map(function (r) {
  return r.children[0].text + ' ' + r.children[1].text; }).join(';'); }
console.log(rowsOf(2));
console.log(rowsOf(4));
console.log(rowsOf(6));
console.log(rowsOf(8));
k[4].children[0].on.click();
k[6].children[0].on.click();
k[2].children[0].on.click();
k[8].children[0].on.click();
console.log(calls.map(function (c) { return c[0]; }).join(' '));
renderGitChanges({branch: null, detached: true, merging: false, entries: []});
console.log(kids('git-changes').map(function (n) { return n.text; }).join(','));
renderGitChanges({branch: null, detached: false, merging: false, entries: []});
console.log(kids('git-changes')[0].text);
"""
    out = _run_node(tmp_path, _script(body))
    assert out[0] == (
        "On main · merge in progress,Conflicted (1),,Staged (2),,Unstaged (1),,"
        "Untracked (1),"
    )
    assert out[1] == "UU c.txt"
    assert out[2] == "M a b.txt;R old.txt → new.txt"
    assert out[3] == "M a b.txt"
    assert out[4] == "? u.txt"
    assert out[5] == (
        "/api/git/diff?path=a%20b.txt&staged=1 /api/git/diff?path=a%20b.txt "
        "/api/git/diff?path=c.txt /api/git/diff?path=u.txt"
    )
    assert out[6] == "HEAD is detached,Nothing to commit: the working tree is clean."
    assert out[7] == "No commit yet"


def test_render_diff(tmp_path):
    body = """
renderGitDiff({path: 'p.bin', staged: true, binary: true, patch: '', truncated: false});
console.log(kids('git-detail').map(function (n) { return n.text; }).join('|'));
renderGitDiff({path: 'p.txt', staged: false, binary: false, patch: '', truncated: false});
console.log(kids('git-detail').map(function (n) { return n.text; }).join('|'));
renderGitDiff({path: 'p.txt', staged: false, binary: false, patch: '+x\\n-y', truncated: false});
var k = kids('git-detail');
console.log(k[0].text, k[1].tag, k[1].className, k[1].children.length);
"""
    out = _run_node(tmp_path, _script(body))
    assert out[0] == "p.bin (staged)|Binary file; no text diff."
    assert out[1] == "p.txt|No changes."
    assert out[2].startswith("p.txt pre patch")
