import json
import re

from test_ui_page import _functions, _page_helpers, _run_node, _text

SVG = """
document.createElementNS = function (ns, tag) { var n = mk(tag); n.ns = ns; return n; };
"""

STUBS = """
var calls = [], hidden = {}, cb = null;
function gitGet(p, done) { calls.push(p); cb = done; }
function setHidden(id, h) { hidden[id] = h; }
function byId(id) { return document.getElementById(id); }
function setClass(n, c) { n.className = c; }
var gitTab = 'graph', gitLimit = 500, gitSelSha = null, gitRowNodes = {}, gitRemotes = ['origin'];
var gitExpanded = {};
function out(x) { console.log(JSON.stringify(x)); }
"""

NAMES = (
    "svgEl", "graphSvg", "refBadges", "relTime", "patchHtml", "onKey", "gitFiles",
    "gitHighlights", "renderGitGraph", "renderGitCommit", "loadGitCommit", "loadGitGraph",
)  # fmt: skip

PAGE = """
var page = {lanes: 1, more: true, commits: [
  {sha: 'a1', short: 'a1', parents: ['b2'], author: 'Ann', email: 'a@x', time: 1,
   subject: 'First', refs: ['HEAD -> main'], lane: 0, edges: [[0, 0]]},
  {sha: 'b2', short: 'b2', parents: [], author: 'Bob', email: 'b@x', time: 1,
   subject: 'Second', refs: [], lane: 0, edges: []}]};
"""


def _script(body: str) -> str:
    text = _text()
    consts = [
        re.search(rf"  var {name} = .*", text).group(0)
        for name in ("LANE_COLORS", "GIT_ROW", "SVG_NS")
    ]
    return (
        _page_helpers(text) + SVG + "\n".join(consts) + "\n" + STUBS
        + _functions(text, *NAMES) + PAGE + body
    )  # fmt: skip


def _run(tmp_path, body: str):
    return [json.loads(x) for x in _run_node(tmp_path, _script(body))]


def test_load_graph_paths(tmp_path):
    body = """
byId('git-show-harness').checked = false; loadGitGraph();
byId('git-show-harness').checked = true; loadGitGraph();
gitLimit = 1000; loadGitGraph();
out(calls);
"""
    assert _run(tmp_path, body)[0] == [
        "/api/git/graph?limit=500",
        "/api/git/graph?limit=500&harness=1",
        "/api/git/graph?limit=1000&harness=1",
    ]


def test_render_graph_rows(tmp_path):
    body = """
gitSelSha = 'b2';
renderGitGraph(page);
var g = byId('git-graph');
var ol = g.children[1];
out([g.children.length, g.children[0].tag, ol.className, ol.children.map(function (li) {
  return [li.className, li.attrs['data-sha'], li.children.slice(1).map(function (c) {
    return c.text;
  })];
}), hidden['btn-git-more']]);
"""
    r = _run(tmp_path, body)[0]
    assert r[0] == 2 and r[1] == "svg" and r[2] == "git-rows"
    assert r[3][0][0] == "git-row" and r[3][1][0] == "git-row git-gap"
    assert r[3][0][1] == "a1"
    assert r[3][0][2][:2] == ["First", "Ann"] and r[3][0][2][3] == "a1"
    assert r[3][0][2][2] == "1970-01-01"
    assert r[4] is False


def test_more_button_visibility(tmp_path):
    body = """
var res = [];
page.more = false; renderGitGraph(page); res.push(hidden['btn-git-more']);
page.more = true; renderGitGraph(page); res.push(hidden['btn-git-more']);
gitLimit = 5000; renderGitGraph(page); res.push(hidden['btn-git-more']);
out(res);
"""
    assert _run(tmp_path, body)[0] == [True, False, True]


def test_empty_page(tmp_path):
    body = """
renderGitGraph({commits: [], lanes: 0, more: false});
var g = byId('git-graph');
out([g.children.length, g.children[0].text]);
"""
    assert _run(tmp_path, body)[0] == [1, "No commits yet."]


def test_click_selects_row(tmp_path):
    body = """
renderGitGraph(page);
gitRowNodes['a1'].on.click();
out([calls, gitRowNodes['a1'].className, gitRowNodes['b2'].className,
  byId('git-graph').children[1].children[1].attrs['aria-expanded']]);
"""
    r = _run(tmp_path, body)[0]
    assert r == [["/api/git/commit/a1"], "git-row selected", "git-row git-minor", "true"]


def test_highlights_keep_refs_merges_and_regular_checkpoints(tmp_path):
    body = """
var commits = Array.from({length: 23}, function (_, i) {
  return {sha: String(i), parents: i === 5 ? ['a', 'b'] : ['a'],
    refs: i === 3 ? ['main'] : []};
});
out(gitHighlights(commits).map(function (g) {
  return [g.main.sha, g.minor.map(function (c) { return c.sha; })];
}));
"""
    assert _run(tmp_path, body)[0] == [
        ["0", ["1", "2"]], ["3", ["4"]], ["5", ["6", "7", "8", "9"]],
        ["10", ["11", "12", "13", "14", "15", "16", "17", "18", "19"]],
        ["20", ["21", "22"]],
    ]


def test_expand_minor_commits_and_select_them(tmp_path):
    body = """
renderGitGraph(page);
var rows = byId('git-graph').children[1];
rows.children[1].on.click();
out([rows.children.length, byId('git-graph').children[1].children.length,
  gitRowNodes['b2'].className]);
gitRowNodes['b2'].on.click();
out([calls, gitRowNodes['b2'].className]);
"""
    assert _run(tmp_path, body) == [
        [2, 3, "git-row git-minor"],
        [["/api/git/commit/b2"], "git-row git-minor selected"],
    ]


def test_collapsing_does_not_replace_real_graph_edges(tmp_path):
    body = """
renderGitGraph(page);
var svg = byId('git-graph').children[0];
out([svg.attrs.height, svg.children.filter(function (n) { return n.tag === 'line'; }).length,
  svg.children.filter(function (n) { return n.tag === 'circle'; }).length]);
"""
    assert _run(tmp_path, body)[0] == ["112", 1, 2]


def test_git_panes_have_separate_scroll_and_resize():
    text = _text()
    assert 'id="git-divider" role="separator"' in text
    assert "grid-template-columns: minmax(0, var(--git-list-width))" in text
    assert "#git-list, #git-detail { min-width: 0; overflow: auto;" in text
    assert "divider.addEventListener('pointermove'" in text
    assert "divider.addEventListener('keydown'" in text


def test_render_commit(tmp_path):
    body = """
renderGitCommit({subject: 'S', short: 'abc', author: 'Ann', email: 'a@x', time: 1,
  refs: [], body: 'More text', patch: '+x', truncated: false,
  files: [{path: 'n', old_path: 'o', status: 'R', added: 1, deleted: 2},
          {path: 'b.png', old_path: null, status: 'A', added: null, deleted: null}]});
var d = byId('git-detail').children;
var f = d[4].children;
out([d.map(function (c) { return c.tag; }), d[0].text, d[1].text, d[3].text,
  f[0].children.map(function (c) { return c.text; }),
  f[1].children[2].text]);
renderGitCommit({subject: 'S', short: 'abc', author: 'A', email: 'e', time: 1,
  refs: [], body: '  \\n', patch: '', truncated: false, files: []});
out(byId('git-detail').children.map(function (c) { return c.tag; }));
"""
    r = _run(tmp_path, body)
    assert r[0][0] == ["h3", "p", "span", "pre", "ul", "pre"]
    assert r[0][1] == "S"
    assert r[0][2].startswith("abc · Ann <a@x> · ")
    assert r[0][3] == "More text"
    assert r[0][4] == ["R", "o → n", "+1 -2"]
    assert r[0][5] == "binary"
    assert r[1] == ["h3", "p", "span", "ul", "pre"]


def test_wire_git_more_listener():
    text = _text()
    m = re.search(r"  function wireGit\(.*?\n  \}", text, re.S)
    assert m
    assert "btn-git-more" in m.group(0)
    assert "Math.min(gitLimit + 500, 5000)" in m.group(0)
