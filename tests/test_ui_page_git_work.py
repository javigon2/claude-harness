import json

from test_ui_page import _functions, _page_helpers, _run_node, _text

STUBS = """
var confirms = [], answer = true, posts = [], dones = [], gets = [], getDone = [];
var window = {confirm: function (t) { confirms.push(t); return answer; }};
function byId(id) { return document.getElementById(id); }
function setHidden(id, h) { byId(id).hidden = h; }
function gitPost(path, body, done) { posts.push([path, body]); dones.push(done); }
function gitGet(path, done) { gets.push(path); getDone.push(done); }
function gitAct(label, fn) {
  var b = el('button', 'git-act', label);
  b.on.click = function (ev) { ev.stopPropagation(); fn(); };
  return b;
}
function gitFiles(files) { return el('ul', 'git-files'); }
function patchHtml(p, t) { return el('pre', 'patch', p); }
function loadGitCommit(sha) {}
function labels(a) { return a.map(function (b) { return b.text; }); }
function click(b) { b.on.click({stopPropagation: function () {}}); }
"""

NAMES = (
    "gitBranchActs", "gitReview", "gitApprove", "renderGitCompare", "showGitBranch",
)  # fmt: skip


def _script(body: str) -> str:
    text = _text()
    return _page_helpers(text) + STUBS + _functions(text, *NAMES) + body


def _run(tmp_path, body: str):
    return json.loads(_run_node(tmp_path, _script(body))[-1])


def test_chip_markup():
    text = _text()
    a = '<button id="btn-git" type="button" class="hidden">Git</button>\n'
    b = '<span id="git-branch-chip" class="chip hidden" title="The checked-out branch"></span>'
    assert a + "  " + b in text


def test_work_acts(tmp_path):
    body = """
var w = {name: 'work/r1', kind: 'local', work: true, base: 'main'};
var cur = {name: 'work/r1', kind: 'local', work: true, base: 'main', current: true};
var nb = {name: 'work/r1', kind: 'local', work: true, base: null};
console.log(JSON.stringify([labels(gitBranchActs(cur, 'work/r1')),
  labels(gitBranchActs(w, 'main')), labels(gitBranchActs(nb, 'main'))]));
"""
    assert _run(tmp_path, body) == [
        ["Review", "Approve"],
        ["Review", "Approve", "Switch", "Merge", "Delete"],
        ["Switch", "Merge", "Delete"],
    ]


def test_review_and_approve(tmp_path):
    body = """
var w = {name: 'work/r1', kind: 'local', work: true, base: 'main'};
var acts = gitBranchActs(w, 'main');
click(acts[0]);
click(acts[1]);
dones[0]({sha: 'abc1234'});
var note = byId('git-note').textContent;
answer = false;
click(acts[1]);
console.log(JSON.stringify([gets, confirms, posts, note, posts.length]));
"""
    gets, confirms, posts, note, n = _run(tmp_path, body)
    assert gets == ["/api/git/compare?base=main&head=work%2Fr1"]
    assert confirms[0] == "Approve work/r1?\n\nThis merges it into main and checks out main."
    assert posts == [["/api/git/approve", {"name": "work/r1"}]]
    assert note == "Approved work/r1: merged into main as abc1234. Push main when ready."
    assert n == 1


def test_render_compare(tmp_path):
    body = """
function run(c) {
  renderGitCompare(c);
  var d = byId('git-detail');
  return d.children.map(function (n) {
    return [n.tag, n.className, n.text, n.children.map(function (li) {
      return [li.className, li.children.map(function (s) {
        return [s.className, s.text];
      })];
    })];
  });
}
var cm = [{short: 'a1', subject: 's1'}];
console.log(JSON.stringify([
  run({base: 'main', head: 'w', ahead: 0, behind: 0, commits: [], files: [], patch: 'p'}),
  run({base: 'main', head: 'w', ahead: 1, behind: 0, commits: cm, files: [], patch: 'p'}),
  run({base: 'main', head: 'w', ahead: 3, behind: 2, commits: [], files: [], patch: 'p'})
]));
"""
    r0, r1, r3 = _run(tmp_path, body)
    assert [n[0] for n in r0] == ["h3", "p", "ul", "ul", "pre"]
    assert r0[0][2] == "w → main"
    assert r0[1][1:3] == ["muted", "Nothing to approve: w has no commits that main lacks."]
    assert r0[2][1] == "git-compare-commits"
    assert r1[1][2] == "1 commit to approve"
    assert r1[2][3] == [["git-file", [["git-sha mono", "a1"], ["git-subject", "s1"]]]]
    assert r3[1][2] == "3 commits to approve · main has 2 newer"


def test_show_branch(tmp_path):
    body = """
var out = [];
[{branch: 'work/r1'}, {branch: null, detached: true}, null].forEach(function (st) {
  showGitBranch(st);
  var c = byId('git-branch-chip');
  out.push([c.text, c.hidden]);
});
console.log(JSON.stringify(out));
"""
    assert _run(tmp_path, body) == [["on work/r1", False], ["detached HEAD", False], ["", True]]


def test_wiring():
    text = _text()
    assert "loadGitBranchChip();" in _functions(text, "poll")
    assert "if (!project) showGitBranch(null);" in _functions(text, "applyProject")
