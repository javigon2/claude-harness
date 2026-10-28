import json

from test_ui_page import _functions, _page_helpers, _run_node, _text

STUBS = """
var gitRemotes = ['origin'], confirms = [], answer = true, posts = [], dones = [], prompts = [];
var promptAnswer = null, gets = [], getAnswer = null, shown = [];
var window = {
  confirm: function (t) { confirms.push(t); return answer; },
  prompt: function (t) { prompts.push(t); return promptAnswer; }
};
function byId(id) { return document.getElementById(id); }
function gitPost(path, body, done) { posts.push([path, body]); dones.push(done); }
function gitGet(path, done) { gets.push(path); done(getAnswer); }
function gitAct(label, fn) {
  var b = el('button', 'git-act', label);
  b.on.click = function (ev) { ev.stopPropagation(); fn(); };
  return b;
}
function loadGitCommit(sha) { shown.push(sha); }
function gitTrack(b) { return ''; }
function relTime() { return 'now'; }
function labels(a) { return a.map(function (b) { return b.text; }); }
function click(b) { b.on.click({stopPropagation: function () {}}); }
"""

NAMES = (
    "fillGitRemotes", "loadGitRemotes", "gitBranchActs", "renderGitBranches",
    "gitNewBranch", "gitFetch", "gitPush", "onKey",
)  # fmt: skip


def _script(body: str) -> str:
    text = _text()
    return _page_helpers(text) + STUBS + _functions(text, *NAMES) + body


def test_markup():
    text = _text()
    bar = text[text.index('<div id="git-bar">') : text.index('<p id="git-msg"')]
    order = [
        bar.index('id="git-show-harness"'),
        bar.index('<select id="git-remote"'),
        bar.index('id="btn-git-fetch"'),
        bar.index('id="btn-git-push"'),
    ]
    assert order == sorted(order)
    box = '<div id="git-branch-box" class="hidden">'
    i = text.index(box)
    assert 'id="btn-git-new-branch"' in text[i : text.index("</div>", i)]
    assert text.index('id="git-branches"') > i
    between = text[i + len(box) : text.index('id="git-branches"')]
    assert 'id="git-' not in between.split("</div>", 1)[1]


def test_fill_remotes(tmp_path):
    body = """
var sel = byId('git-remote');
gitRemotes = ['origin', 'up'];
sel.value = 'up';
fillGitRemotes();
var a = [sel.children.map(function (o) { return o.text + '/' + o.value; }), sel.value];
sel.value = 'gone';
fillGitRemotes();
var b = sel.value;
gitRemotes = [];
fillGitRemotes();
console.log(JSON.stringify([a, b, sel.children.length, sel.value]));
"""
    out = json.loads(_run_node(tmp_path, _script(body))[-1])
    assert out == [[["origin/origin", "up/up"], "up"], "origin", 0, ""]


def test_branch_acts(tmp_path):
    body = """
var r = [];
r.push(labels(gitBranchActs({name: 'main', kind: 'local', current: true}, 'main')));
r.push(labels(gitBranchActs({name: 'harness/x', kind: 'local'}, 'main')));
var loc = {name: 'feat', kind: 'local'};
r.push(labels(gitBranchActs(loc, 'main')));
r.push(labels(gitBranchActs(loc, null)));
var rem = {name: 'origin/main', kind: 'remote'};
r.push(labels(gitBranchActs(rem, 'main')));
r.push(labels(gitBranchActs(rem, null)));
var acts = gitBranchActs(loc, 'main');
acts.forEach(click);
var racts = gitBranchActs(rem, 'main');
click(racts[0]);
dones[0]({}); var n1 = byId('git-note').textContent;
dones[1]({sha: 'abc1234'}); var n2 = byId('git-note').textContent;
dones[2]({}); var n3 = byId('git-note').textContent;
answer = false;
var before = posts.length;
acts.forEach(click);
console.log(JSON.stringify([r, confirms.slice(0, 4), posts, [n1, n2, n3], posts.length - before]));
"""
    out = json.loads(_run_node(tmp_path, _script(body))[-1])
    assert out[0] == [
        [], [], ["Switch", "Merge", "Delete"], ["Switch", "Delete"], ["Merge"], [],
    ]  # fmt: skip
    assert out[1] == [
        "Switch to feat?",
        "Merge feat into main?",
        "Delete branch feat?\n\nOnly a fully merged branch can be deleted.",
        "Merge origin/main into main?",
    ]
    assert out[2] == [
        ["/api/git/switch", {"name": "feat"}],
        ["/api/git/merge", {"name": "feat"}],
        ["/api/git/delete-branch", {"name": "feat"}],
        ["/api/git/merge", {"name": "origin/main"}],
    ]
    assert out[3] == [
        "Switched to feat.",
        "Merged feat into main: abc1234.",
        "Deleted feat.",
    ]
    assert out[4] == 0


def test_render_branches_buttons_and_remotes(tmp_path):
    body = """
var list = {current: 'main', remotes: ['a', 'b'], branches: [
  {name: 'feat', kind: 'local', sha: 's', subject: 'x', time: 1}]};
renderGitBranches(list);
var ul = kids('git-branches')[1];
var li = ul.children[0];
console.log(JSON.stringify([li.children.map(function (c) { return c.className + ':' + c.text; }),
  byId('git-remote').children.length]));
function kids(id) { return nodes[id].children; }
"""
    out = json.loads(_run_node(tmp_path, _script(body))[-1])
    cls = [c.split(":")[0] for c in out[0]]
    assert cls == [
        "git-name mono", "git-track muted", "git-subject", "git-when muted",
        "git-act", "git-act", "git-act",
    ]  # fmt: skip
    assert out[0][4:] == ["git-act:Switch", "git-act:Merge", "git-act:Delete"]
    assert out[1] == 2


def test_new_branch(tmp_path):
    body = """
promptAnswer = null; gitNewBranch();
promptAnswer = '  '; gitNewBranch();
var none = posts.length;
promptAnswer = ' feat '; gitNewBranch();
dones[0]({});
console.log(JSON.stringify([none, posts, byId('git-note').textContent]));
"""
    out = json.loads(_run_node(tmp_path, _script(body))[-1])
    assert out == [
        0, [["/api/git/branch", {"name": "feat", "start": "HEAD"}]],
        "Created and switched to feat.",
    ]  # fmt: skip


def test_fetch_and_push(tmp_path):
    body = """
var sel = byId('git-remote');
sel.value = '';
gitFetch();
var m1 = byId('git-msg').textContent;
byId('git-msg').textContent = '';
gitPush();
var m2 = byId('git-msg').textContent;
sel.value = 'origin';
gitFetch();
dones[0]({output: 'f'});
var n1 = byId('git-note').textContent;
getAnswer = {branch: null};
gitPush();
var m3 = byId('git-msg').textContent;
var np = posts.length;
getAnswer = {branch: 'main'};
gitPush();
dones[1]({output: 'x'});
console.log(JSON.stringify([m1, m2, posts, n1, m3, np, confirms, byId('git-note').textContent]));
"""
    out = json.loads(_run_node(tmp_path, _script(body))[-1])
    assert out[0] == out[1] == "This project has no remote."
    assert out[2] == [
        ["/api/git/fetch", {"remote": "origin"}],
        ["/api/git/push", {"remote": "origin", "branch": "main"}],
    ]
    assert out[3] == "Fetched origin: f"
    assert out[4] == "Check out a branch first."
    assert out[5] == 1
    assert out[6] == ["Push main to origin?"]
    assert out[7] == "Pushed main to origin: x"


def test_wiring_text():
    text = _text()
    wire = _functions(text, "wireGit")
    assert "loadGitRemotes()" in wire
    for name in ("gitNewBranch", "gitFetch", "gitPush"):
        assert f"addEventListener('click', {name})" in wire
    assert "setHidden('git-branch-box', name !== 'branches');" in _functions(text, "gitShowTab")
