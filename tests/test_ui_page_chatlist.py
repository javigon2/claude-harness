import json
import re

from test_ui_page import _functions, _page_helpers, _run_node, _text

SCENARIO = """
var now = new Date(2026, 9, 27, 12).getTime();
function iso(daysAgo, hour) { return new Date(2026, 9, 27 - daysAgo, hour || 9).toISOString(); }
function R(id, title, first, daysAgo, state, hour) {
  return {id: id, title: title, first_message: first, started_at: iso(daysAgo, hour),
    state: state};
}
var reqs = [
  R('old', 'Fix login', 'the login page is broken', 40, 'done'),
  R('a', 'Plan the dashboard', 'build a dashboard', 0, 'running', 8),
  R('b', 'Rename flags', 'rename --fast to --quick', 0, 'waiting', 11),
  R('c', 'Yesterday chat', 'hello', 1, 'done'),
  R('d', 'Last week', 'x', 5, 'attention'),
  R('e', 'Three weeks ago', 'y', 20, 'done'),
  R('f', 'Ancient', 'z', 400, 'done')
];
function view(secs) {
  return secs.map(function (s) {
    return [s.label, s.items.map(function (i) { return i.value; })];
  });
}
var out = [];
out.push(view(chatSections(reqs, [{value: 'r9', label: 'orphan run'}], '', now)));
out.push(view(chatSections(reqs, [], 'DASHBOARD', now)));
out.push(view(chatSections(reqs, [], '--fast', now)));
out.push(view(chatSections(reqs, [{value: 'r9', label: 'orphan run'}], 'orphan', now)));
out.push(view(chatSections(reqs, [], 'no such thing', now)));
out.push(chatSections(reqs, [], 'rename', now)[0].items[0]);
out.push([chatGroup('garbage', now).label, chatGroup(iso(-3), now).label,
  chatGroup(iso(7), now).label, chatGroup(iso(8), now).label, chatGroup(iso(30), now).label,
  chatGroup(iso(31), now).label]);
console.log(JSON.stringify(out));
"""


def _funcs() -> str:
    return _functions(_text(), "chatGroup", "chatSections")


def test_groups_search_and_order(tmp_path):
    script = "function str(x) { return (x === null || x === undefined) ? '?' : String(x); }\n"
    script += _funcs() + SCENARIO
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out[0] == [
        ["Today", ["b", "a"]], ["Yesterday", ["c"]], ["Previous 7 days", ["d"]],
        ["Previous 30 days", ["e"]], ["September", ["old"]], ["September 2025", ["f"]],
        ["Runs", ["r9"]],
    ]  # fmt: skip
    assert out[1] == [["Today", ["a"]]]  # title match, case-insensitive
    assert out[2] == [["Today", ["b"]]]  # first-message match
    assert out[3] == [["Runs", ["r9"]]]
    assert out[4] == []
    assert out[5] == {"value": "b", "label": "Rename flags", "tip": "rename --fast to --quick",
                      "state": "waiting"}  # fmt: skip
    assert out[6] == ["Older", "Today", "Previous 7 days", "Previous 30 days", "Previous 30 days",
                      "September"]  # fmt: skip


RENDER = """
var runs = ['r1', 'old'], titles = {}, pendingNew = false, chatQuery = '', chatSig = '';
var chatRows = {}, chatSel = null, sidebarClosed = 0, started = 0;
var STATE_LABEL = {running: 'running', waiting: 'needs you', done: 'done',
  attention: 'needs attention'};
var nowMs = Date.now();
var requests = [
  {id: 'a', title: 'Plan it', first_message: 'plan it', started_at: new Date(nowMs).toISOString(),
   state: 'running', runs: ['r1']},
  {id: 'b', title: 'Done one', first_message: 'x', started_at: new Date(nowMs - 1000).toISOString(),
   state: 'done', runs: []}
];
var location = {};
function sidebarOpen(on) { if (!on) sidebarClosed++; }
function startNew() { started++; }
function byId(id) { return document.getElementById(id); }
function rows() { return nodes['chat-list'].children; }
function info() {
  return rows().map(function (n) {
    var kids = n.children.map(function (c) { return c.className + ':' + c.text; });
    return n.className + '|' + (kids.length ? kids.join(',') : n.text);
  });
}
var out = [];
renderChatList();
out.push(info());
var first = rows()[1];
renderChatList();
out.push(rows()[1] === first);
chatSel = 'a'; markChat();
out.push([rows()[1].className, rows()[1].attrs['aria-current'], rows()[2].className]);
chatSel = 'b'; markChat();
out.push([rows()[1].className, 'aria-current' in rows()[1].attrs, rows()[2].className]);
rows()[2].on.click();
out.push([location.hash, sidebarClosed]);
pendingNew = true; renderChatList();
out.push(rows()[0].children[1].text);
rows()[0].on.click();
out.push(started);
pendingNew = false; chatQuery = 'zzz'; renderChatList();
out.push(info());
requests = []; runs = []; chatQuery = ''; renderChatList();
out.push(info());
console.log(JSON.stringify(out));
"""


def test_chat_list_renders_and_selects_in_place(tmp_path):
    text = _text()
    script = (
        _page_helpers(text)
        + "function setClass(n, c) { if (n.className !== c) n.className = c; }\n"
        + "function runsOf(r) { return (r && Array.isArray(r.runs)) ? r.runs : []; }\n"
        + _funcs() + _functions(text, "pickChat", "markChat", "renderChatList")
        + RENDER
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out[0] == [
        "chat-group|Today",
        "chat-row|dot dot-running:,chat-title:Plan it",
        "chat-row|dot:,chat-title:Done one",
        "chat-group|Runs",
        "chat-row|dot:,chat-title:old",
    ]
    assert out[1] is True  # an unchanged list is not rebuilt
    assert out[2] == ["chat-row selected", "true", "chat-row"]
    assert out[3] == ["chat-row", False, "chat-row selected"]
    assert out[4] == ["#b", 1]
    assert out[5] == "New chat"
    assert out[6] == 1
    assert out[7] == ["chat-empty muted|No matching chats"]
    assert out[8] == ["chat-empty muted|No chats yet"]


def test_sidebar_markup_and_wiring():
    text = _text()
    side = text[text.index('<aside id="sidebar"') : text.index("</aside>")]
    order = [
        "btn-new-chat", 'id="chat-search"', 'id="chat-list"', 'id="sidebar-foot"',
        'id="project-name"', 'id="btn-switch-project"',
    ]  # fmt: skip
    places = [side.index(w) for w in order]
    assert places == sorted(places)
    head = text[text.index("<header>") : text.index("</header>")]
    assert "project-name" not in head and "btn-switch-project" not in head
    assert "'/api/orchestrator/new'" in text and "wireSidebar();" in text
    assert re.search(r"\.dot-running \{[^}]*animation: think", text)
    assert "location.hash = '#' + encodeURIComponent(value);" in text
