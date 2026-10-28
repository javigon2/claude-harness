import json
import re

from test_ui_page import _functions, _page_helpers, _run_node, _text

IDS = [
    "git-view", "btn-git-back", "git-tab-graph", "git-tab-changes", "git-tab-branches",
    "git-show-harness", "git-msg", "git-graph", "git-changes", "git-branches", "btn-git-more",
    "git-detail",
]  # fmt: skip


def _section(text: str) -> str:
    return text[text.index('<section id="git-view"') : text.index('<dialog id="img-view">')]


def test_git_button_sits_in_the_header_and_shows_with_a_project():
    text = _text()
    head = text[text.index("<header>") : text.index("</header>")]
    assert '<button id="btn-git" type="button" class="hidden">Git</button>' in head
    apply = re.search(r"  function applyProject\(p\) \{.*?\n  \}\n", text, re.S)
    assert apply and "setHidden('btn-git', !project);" in apply.group(0)


def test_git_view_markup_in_order():
    text = _text()
    sec = _section(text)
    assert sec.startswith('<section id="git-view" class="hidden">')
    places = [sec.index(f'id="{i}"') for i in IDS]
    assert places == sorted(places)
    for pane in ("git-changes", "git-branches"):
        assert f'<div id="{pane}" role="tabpanel" class="hidden"></div>' in sec
    assert '<button id="btn-git-more" type="button" class="hidden">Load more</button>' in sec
    assert 'class="selected">Graph</button>' in sec
    assert "'git-view'" in re.search(r"var MAIN_VIEWS = \[[^\]]*\]", text).group(0)
    assert "  wirePlans();\n  wireGit();\n" in text
    assert re.search(r"#git-msg:empty \{ display: none; \}", text)


SHOW = """
var hidden = {};
function setHidden(id, hide) { hidden[id] = hide; }
function byId(id) {
  var n = document.getElementById(id);
  if (!n.classList) {
    n.classList = {on: {}, toggle: function (c, v) { this.on[c] = v; }};
  }
  return n;
}
var GIT_TABS = ['graph', 'changes', 'branches'];
var gitTab = 'graph', loaded = [];
function loadGitGraph() { loaded.push('graph'); }
function loadGitChanges() { loaded.push('changes'); }
function loadGitBranches() { loaded.push('branches'); }
byId('git-msg').textContent = 'old error';
byId('git-detail').appendChild(el('div', null, 'old detail'));
gitShowTab('branches');
var out = [gitTab, hidden['git-graph'], hidden['git-changes'], hidden['git-branches'],
  hidden['btn-git-more'], byId('git-tab-branches').classList.on.selected,
  byId('git-tab-graph').classList.on.selected, byId('git-tab-branches').attrs['aria-selected'],
  byId('git-tab-graph').attrs['aria-selected'], byId('git-msg').textContent,
  byId('git-detail').children.length];
gitShowTab('graph');
gitLoad();
out.push(loaded);
console.log(JSON.stringify(out));
"""


def test_show_tab_switches_panes_and_loads(tmp_path):
    text = _text()
    script = _page_helpers(text) + _functions(text, "gitLoad", "gitShowTab") + SHOW
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        "branches", True, True, False, True, True, False, "true", "false", "", 0,
        ["branches", "graph", "graph"],
    ]  # fmt: skip


GET = """
var msgs = [], got = [];
function byId(id) { return document.getElementById(id); }
function errorOf(res) { return 'E ' + res.status + ' ' + (res.data && res.data.error); }
var replies = {
  '/ok': {ok: true, status: 200, body: '{"a": 1}'},
  '/bad': {ok: false, status: 409, body: '{"error": "not a git repository: /x"}'}
};
function fetch(path) {
  if (!replies[path]) return Promise.reject(new Error('down'));
  var r = replies[path];
  return Promise.resolve({ok: r.ok, status: r.status,
    text: function () { return Promise.resolve(r.body); }});
}
function step(path) {
  return new Promise(function (resolve) {
    gitGet(path, function (d) { got.push(d); });
    setTimeout(function () { msgs.push(byId('git-msg').textContent); resolve(); }, 10);
  });
}
step('/bad').then(function () { return step('/ok'); }).then(function () {
  return step('/down');
}).then(function () { console.log(JSON.stringify([msgs, got])); });
"""


def test_git_get_reports_errors_to_the_message(tmp_path):
    text = _text()
    script = _page_helpers(text) + _functions(text, "gitGet") + GET
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        ["E 409 not a git repository: /x", "", "Connection lost; try again."],
        [{"a": 1}],
    ]
