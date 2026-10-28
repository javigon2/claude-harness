import json
import re

from test_ui_page import _functions, _page_helpers, _run_node, _text

SVG = """
document.createElementNS = function (ns, tag) { var n = mk(tag); n.ns = ns; return n; };
"""


def _script(*names: str) -> str:
    text = _text()
    consts = [
        re.search(rf"  var {name} = .*", text).group(0)
        for name in ("LANE_COLORS", "GIT_ROW", "SVG_NS")
    ]
    return _page_helpers(text) + SVG + "\n".join(consts) + "\n" + _functions(text, *names)


def _eval(tmp_path, names, expr):
    script = _script(*names) + f"console.log(JSON.stringify({expr}));\n"
    return json.loads(_run_node(tmp_path, script)[-1])


DUMP = "function dump(n) { return {tag: n.tag, attrs: n.attrs}; }\n"


def test_graph_svg_example(tmp_path):
    expr = (
        "(function () { var s = graphSvg([{lane: 0, edges: [[0, 0], [0, 1]]},"
        " {lane: 1, edges: [[0, 0], [1, 0]]}, {lane: 0, edges: []}], 2);"
        " return {attrs: s.attrs, kids: s.children.map(function (c) {"
        " return [c.tag, c.attrs]; })}; })()"
    )
    got = _eval(tmp_path, ("svgEl", "graphSvg"), expr)
    assert got["attrs"] == {
        "class": "git-svg", "width": "28", "height": "84",
        "viewBox": "0 0 28 84", "aria-hidden": "true",
    }  # fmt: skip
    kids = got["kids"]
    assert [k[0] for k in kids] == ["line", "path", "line", "path", "circle", "circle", "circle"]
    assert kids[0][1] == {
        "x1": "7", "y1": "14", "x2": "7", "y2": "42",
        "stroke": "#1b6e94", "stroke-width": "2",
    }  # fmt: skip
    assert kids[1][1] == {
        "d": "M7 14 C7 28 21 28 21 42", "stroke": "#1e7a34",
        "stroke-width": "2", "fill": "none",
    }  # fmt: skip
    assert kids[2][1]["y1"] == "42" and kids[2][1]["y2"] == "70"
    assert kids[2][1]["stroke"] == "#1b6e94"
    assert kids[3][1]["d"] == "M21 42 C21 56 7 56 7 70"
    assert kids[3][1]["stroke"] == "#1b6e94"
    assert kids[4][1] == {"cx": "7", "cy": "14", "r": "4", "fill": "#1b6e94"}
    assert kids[5][1]["cx"] == "21" and kids[5][1]["cy"] == "42"
    assert kids[5][1]["fill"] == "#1e7a34"
    assert kids[6][1]["cx"] == "7" and kids[6][1]["cy"] == "70"


def test_graph_svg_lane_nine_and_empty(tmp_path):
    got = _eval(
        tmp_path, ("svgEl", "graphSvg"),
        "(function () { var s = graphSvg([{lane: 9, edges: []}], 10);"
        " return [s.attrs.width, s.children[0].attrs.fill]; })()",
    )  # fmt: skip
    assert got == ["140", "#1e7a34"]
    got = _eval(
        tmp_path, ("svgEl", "graphSvg"),
        "(function () { var s = graphSvg([], 0);"
        " return [s.attrs.width, s.attrs.height, s.children.length]; })()",
    )  # fmt: skip
    assert got == ["14", "0", 0]


PATCH = (
    "diff --git a/f b/f\\nindex 1..2 100644\\n--- a/f\\n+++ b/f\\n@@ -1,3 +1,3 @@\\n"
    " ctx\\n--- deleted\\n+<b> & added\\n\\n\\\\ No newline at end of file\\n"
    "diff --git a/g b/g\\n--- a/g\\n"
)


def test_patch_html(tmp_path):
    expr = (
        "(function () { var p = patchHtml('" + PATCH + "', true);"
        " return {tag: p.tag, cls: p.className, kids: p.children.map(function (c) {"
        " return [c.className, c.text]; })}; })()"
    )
    got = _eval(tmp_path, ("patchHtml",), expr)
    assert got["tag"] == "pre" and got["cls"] == "patch"
    classes = [k[0] for k in got["kids"]]
    assert classes == [
        "meta", "meta", "meta", "meta", "hunk", "", "del", "add", "", "meta",
        "meta", "meta", "cut",
    ]  # fmt: skip
    assert got["kids"][7][1] == "+<b> & added"
    assert got["kids"][-1][1].startswith("The patch is too large")


def test_patch_html_empty(tmp_path):
    got = _eval(tmp_path, ("patchHtml",), "[patchHtml(null, false).children.length]")
    assert got == [0]


def test_ref_badges(tmp_path):
    expr = (
        "[refBadges(['HEAD -> main', 'HEAD', 'tag: v1', 'origin/main', 'work/x', 'dev',"
        " 'up/dev'], undefined), refBadges(['up/dev', 'origin/x'], ['up']), refBadges(5)]"
        ".map(function (b) { return [b.className, b.children.map(function (c) {"
        " return [c.className, c.text]; })]; })"
    )
    got = _eval(tmp_path, ("refBadges",), expr)
    assert got[0][0] == "refs"
    assert got[0][1] == [
        ["ref ref-head", "main"], ["ref ref-head", "HEAD"], ["ref ref-tag", "v1"],
        ["ref ref-remote", "origin/main"], ["ref ref-work", "work/x"],
        ["ref ref-branch", "dev"], ["ref ref-branch", "up/dev"],
    ]  # fmt: skip
    assert got[1][1] == [["ref ref-remote", "up/dev"], ["ref ref-branch", "origin/x"]]
    assert got[2][1] == []


def test_rel_time(tmp_path):
    now = 1800000000
    cases = [0, 59, 60, 3599, 3600, 86399, 86400, 29 * 86400, 30 * 86400, -5]
    expr = (
        f"[0, 59, 60, 3599, 3600, 86399, 86400, {29 * 86400}, {30 * 86400}, -5]"
        f".map(function (d) {{ return relTime({now} - d, {now}); }})"
        f".concat([relTime(null, {now}), relTime({now}, null)])"
    )
    got = _eval(tmp_path, ("relTime",), expr)
    assert len(cases) == 10
    assert got == [
        "just now", "just now", "1 min ago", "59 min ago", "1 h ago", "23 h ago",
        "1 d ago", "29 d ago", "2026-12-16", "just now", "?", "?",
    ]  # fmt: skip
