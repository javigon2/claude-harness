import json

from harness.ui.markdown import parse_markdown


def _t(text: str) -> dict:
    return {"t": "text", "text": text}


def test_heading_levels_and_closing_hashes():
    blocks = parse_markdown("# One\n\n### Three ###\n\n####### seven\n\n#nospace")
    assert blocks[0] == {"type": "heading", "level": 1, "inline": [_t("One")]}
    assert blocks[1] == {"type": "heading", "level": 3, "inline": [_t("Three")]}
    assert [b["type"] for b in blocks[2:]] == ["paragraph", "paragraph"]


def test_paragraph_joins_lines_with_a_space():
    assert parse_markdown("one\ntwo\n\nthree") == [
        {"type": "paragraph", "inline": [_t("one two")]},
        {"type": "paragraph", "inline": [_t("three")]},
    ]


def test_bullet_and_ordered_lists():
    blocks = parse_markdown("- a\n- b\n\n1. x\n2. y")
    assert blocks == [
        {
            "type": "list",
            "ordered": False,
            "items": [
                [{"type": "paragraph", "inline": [_t("a")]}],
                [{"type": "paragraph", "inline": [_t("b")]}],
            ],
        },
        {
            "type": "list",
            "ordered": True,
            "items": [
                [{"type": "paragraph", "inline": [_t("x")]}],
                [{"type": "paragraph", "inline": [_t("y")]}],
            ],
        },
    ]


def test_list_continuation_and_nesting():
    blocks = parse_markdown("* **Do:** build\n  more text\n  - child 1\n  - child 2\n* second")
    assert len(blocks) == 1
    first, second = blocks[0]["items"]
    assert first[0]["inline"] == [
        {"t": "strong", "text": "Do:"},
        _t(" build more text"),
    ]
    assert first[1]["type"] == "list"
    assert len(first[1]["items"]) == 2
    assert second == [{"type": "paragraph", "inline": [_t("second")]}]


def test_list_with_blank_lines_between_items_stays_one_list():
    blocks = parse_markdown("- a\n\n- b\n\n- c")
    assert len(blocks) == 1
    assert len(blocks[0]["items"]) == 3


def test_fenced_code_is_verbatim():
    text = "```python\n# not a heading\n| not | a table |\n|---|---|\n- not a list\n```\nafter"
    blocks = parse_markdown(text)
    assert blocks[0] == {
        "type": "code",
        "lang": "python",
        "text": "# not a heading\n| not | a table |\n|---|---|\n- not a list",
    }
    assert blocks[1]["type"] == "paragraph"


def test_unclosed_fence_runs_to_the_end():
    blocks = parse_markdown("```\nabc\ndef")
    assert blocks == [{"type": "code", "lang": "", "text": "abc\ndef"}]


def test_longer_fence_can_contain_a_shorter_one():
    blocks = parse_markdown("````md\n```\ninner\n```\n````")
    assert blocks == [{"type": "code", "lang": "md", "text": "```\ninner\n```"}]


def test_table_drops_the_separator_and_pads_ragged_rows():
    text = "| a | b |\n|---|:-:|\n| 1 | 2 |\n| only |\n| x | y | z |\n\nafter"
    table = parse_markdown(text)[0]
    assert table["type"] == "table"
    assert table["header"] == [[_t("a")], [_t("b")]]
    assert table["rows"] == [
        [[_t("1")], [_t("2")]],
        [[_t("only")], []],
        [[_t("x")], [_t("y")]],
    ]


def test_table_cells_can_hold_inline_markup_and_escaped_pipes():
    table = parse_markdown("| h |\n|---|\n| `a` and **b** \\| c |")[0]
    assert table["rows"][0][0] == [
        {"t": "code", "text": "a"},
        _t(" and "),
        {"t": "strong", "text": "b"},
        _t(" | c"),
    ]


def test_quote_holds_blocks():
    assert parse_markdown("> quoted\n> - item") == [
        {
            "type": "quote",
            "blocks": [
                {"type": "paragraph", "inline": [_t("quoted")]},
                {
                    "type": "list",
                    "ordered": False,
                    "items": [[{"type": "paragraph", "inline": [_t("item")]}]],
                },
            ],
        }
    ]


def test_horizontal_rules():
    assert parse_markdown("---\n\n***\n\n* * *") == [{"type": "hr"}] * 3


def test_inline_spans():
    spans = parse_markdown("a **b** *c* _d_ `e` [f](http://x.y/z) g")[0]["inline"]
    assert spans == [
        _t("a "),
        {"t": "strong", "text": "b"},
        _t(" "),
        {"t": "em", "text": "c"},
        _t(" "),
        {"t": "em", "text": "d"},
        _t(" "),
        {"t": "code", "text": "e"},
        _t(" "),
        {"t": "link", "text": "f", "href": "http://x.y/z"},
        _t(" g"),
    ]


def test_code_span_has_no_markup_inside():
    assert parse_markdown("`**x** [a](b)`")[0]["inline"] == [{"t": "code", "text": "**x** [a](b)"}]


def test_code_inside_strong_stays_code():
    assert parse_markdown("**run `x` now**")[0]["inline"] == [
        {"t": "strong", "text": "run "},
        {"t": "code", "text": "x"},
        {"t": "strong", "text": " now"},
    ]


def test_unclosed_markers_stay_literal():
    for text in ["**bold", "*em", "_em", "`code", "[a](b", "[a]", "a ** b", "2 * 3 * 4"]:
        assert parse_markdown(text)[0]["inline"] == [_t(text)], text


def test_snake_case_is_not_emphasis():
    assert parse_markdown("a snake_case_name here")[0]["inline"] == [_t("a snake_case_name here")]


def test_backslash_escapes():
    assert parse_markdown(r"\*not em\* and \`x\`")[0]["inline"] == [_t("*not em* and `x`")]


def test_html_is_plain_text():
    blocks = parse_markdown("<script>alert(1)</script>")
    assert blocks == [{"type": "paragraph", "inline": [_t("<script>alert(1)</script>")]}]
    blocks = parse_markdown("- <img src=x onerror=y>\n\n| <b> |\n|---|\n| <i> |")
    assert "<img src=x onerror=y>" in json.dumps(blocks)
    assert {b["type"] for b in blocks} == {"list", "table"}


def test_empty_and_odd_input_never_raise():
    assert parse_markdown("") == []
    assert parse_markdown("\n\n  \n") == []
    for text in ["#", "- ", "> ", "|", "| a |\n|---", "```", "\r\nx\r\n", "1.", "* * *"]:
        parse_markdown(text)


PLAN = """# Demo: implementation plan

Status: draft
Repo: /tmp/demo

## Goal

A tiny **demo** project, run with `uv run demo`.

### MVP

- first behaviour
- second behaviour

**MVP done when:** `uv run demo` prints hello.

## Shared context

- **Repo:** empty; work merges into `main`
- **Commands:** install `uv sync`, test `uv run pytest -q`

## Estimate

| Stage | Delivers | Tasks | Orders | Worker time |
|---|---|---|---|---|
| 1 | core | 3 | ~3 | ~1 h |
| **Total** | | 3 | ~3 | ~1 h |

## Stage 1: Demo core (~1 h, ~3 orders)

**Delivers:** the core.

**Interfaces:**

```python
def main() -> int: ...
```

### T1.1 Build the thing
- **Tier:** haiku
- **Depends:** none
- **Do:** write it
  - sub-point
- **Done when:** tests pass

> Note: a quoted remark.

---

## Prerequisites and risks

1. `git init` first.
2. Mind the risk.
"""


def test_plan_shaped_document_parses_into_every_block_type():
    blocks = parse_markdown(PLAN)
    kinds = {b["type"] for b in blocks}
    assert {"heading", "paragraph", "list", "table", "code", "quote", "hr"} <= kinds
    headings = [b for b in blocks if b["type"] == "heading"]
    assert headings[0]["level"] == 1
    assert any(h["level"] == 3 for h in headings)
    table = next(b for b in blocks if b["type"] == "table")
    assert len(table["header"]) == 5
    assert len(table["rows"]) == 2
    assert any(b["type"] == "list" and b["ordered"] for b in blocks)
    json.dumps(blocks)
