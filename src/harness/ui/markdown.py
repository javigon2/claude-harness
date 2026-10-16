"""A small Markdown parser for the office page: text in, a JSON block tree out.

It covers the subset the plan-project skill writes (ATX headings, paragraphs, lists, fenced
code, pipe tables, quotes, rules, and flat inline spans). Raw HTML is never interpreted:
`<tag>` stays text. The page builds DOM nodes from the tree with textContent, so nothing
here can inject markup. Pure functions, no I/O.

Block shapes: heading {level, inline}, paragraph {inline}, list {ordered, items: [[block]]},
code {lang, text}, table {header: [inline], rows: [[inline]]}, quote {blocks}, hr.
An inline is a list of flat spans {"t": text|strong|em|code|link, "text"} (+ "href" for link).
"""

import re
from typing import Any

Block = dict[str, Any]
Span = dict[str, str]

_FENCE = re.compile(r"^ {0,3}(`{3,})[ \t]*([^`\s]*)")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])[ \t]+(.*)$")
_QUOTE = re.compile(r"^ {0,3}>[ ]?(.*)$")
_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_LINK = re.compile(r"\[([^\[\]]+)\]\(([^()\s]+)\)")
_ESCAPABLE = "\\`*_[]()#|"


def parse_markdown(text: str) -> list[Block]:
    """The block tree of `text`. Never raises on any string."""
    return _blocks(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        i + 1 < len(lines)
        and "|" in lines[i]
        and "|" in lines[i + 1]
        and _SEP.match(lines[i + 1]) is not None
    )


def _starts_block(lines: list[str], i: int, interrupting_paragraph: bool = False) -> bool:
    """True if lines[i] begins a non-paragraph block. A paragraph is only interrupted by an
    ordered item numbered 1 (so a wrapped "2026. ..." stays in the paragraph)."""
    line = lines[i]
    if _FENCE.match(line) or _HEADING.match(line) or _HR.match(line) or _QUOTE.match(line):
        return True
    if _is_table_start(lines, i):
        return True
    m = _ITEM.match(line)
    if m is None:
        return False
    if interrupting_paragraph and m.group(2)[0].isdigit():
        return m.group(2)[:-1] == "1"
    return True


def _blocks(lines: list[str]) -> list[Block]:
    out: list[Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            block, i = _code(lines, i, fence.group(1), fence.group(2))
            out.append(block)
            continue
        heading = _HEADING.match(line)
        if heading:
            content = re.sub(r"[ \t]+#+[ \t]*$", "", (heading.group(2) or "").strip())
            out.append(
                {"type": "heading", "level": len(heading.group(1)), "inline": _inline(content)}
            )
            i += 1
            continue
        if _HR.match(line):
            out.append({"type": "hr"})
            i += 1
            continue
        if _QUOTE.match(line):
            inner: list[str] = []
            while i < n and (q := _QUOTE.match(lines[i])):
                inner.append(q.group(1))
                i += 1
            out.append({"type": "quote", "blocks": _blocks(inner)})
            continue
        if _is_table_start(lines, i):
            block, i = _table(lines, i)
            out.append(block)
            continue
        if _ITEM.match(line):
            block, i = _list(lines, i)
            out.append(block)
            continue
        parts = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines, i, True):
            parts.append(lines[i].strip())
            i += 1
        out.append({"type": "paragraph", "inline": _inline(" ".join(parts))})
    return out


def _code(lines: list[str], i: int, fence: str, lang: str) -> tuple[Block, int]:
    body: list[str] = []
    i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith(fence) and not stripped.strip("`"):
            i += 1
            break
        body.append(lines[i])
        i += 1
    return {"type": "code", "lang": lang, "text": "\n".join(body)}, i


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [part.strip().replace("\\|", "|") for part in re.split(r"(?<!\\)\|", s)]


def _table(lines: list[str], i: int) -> tuple[Block, int]:
    header = _cells(lines[i])
    width = len(header)
    i += 2
    rows: list[list[list[Span]]] = []
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        cells = _cells(lines[i])[:width]
        cells += [""] * (width - len(cells))
        rows.append([_inline(c) for c in cells])
        i += 1
    return {"type": "table", "header": [_inline(c) for c in header], "rows": rows}, i


def _list(lines: list[str], i: int) -> tuple[Block, int]:
    n = len(lines)
    first = _ITEM.match(lines[i])
    assert first is not None
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    items: list[list[Block]] = []
    while i < n:
        m = _ITEM.match(lines[i])
        if m is None or len(m.group(1)) >= base + 2 or m.group(2)[0].isdigit() != ordered:
            break
        body = [m.group(3)]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _indent(lines[j]) >= base + 2:
                    body.extend([""] * (j - i))
                    i = j
                    continue
                break
            if _indent(line) >= base + 2:
                body.append(line[base + 2 :])
            elif _starts_block(lines, i):
                break
            else:
                body.append(line.strip())  # lazy continuation of the item's paragraph
            i += 1
        items.append(_blocks(body))
        j = i
        while j < n and not lines[j].strip():
            j += 1
        if j > i and j < n:
            nxt = _ITEM.match(lines[j])
            if nxt and len(nxt.group(1)) < base + 2 and nxt.group(2)[0].isdigit() == ordered:
                i = j
    return {"type": "list", "ordered": ordered, "items": items}, i


def _restyle(spans: list[Span], kind: str) -> list[Span]:
    """Spans inside ** or *: text and nested emphasis become `kind`; code and links stay.
    Neighbours of the same kind are merged, so the result is flat."""
    out: list[Span] = []
    for span in spans:
        if span["t"] in ("text", "strong", "em"):
            span = {"t": kind, "text": span["text"]}
        if out and span["t"] == kind and out[-1]["t"] == kind:
            out[-1] = {"t": kind, "text": out[-1]["text"] + span["text"]}
        else:
            out.append(span)
    return out


def _find_em_close(text: str, i: int, marker: str) -> int:
    """Index of the closing `marker` for an emphasis opened at i, or -1."""
    j = i + 1
    while True:
        j = text.find(marker, j + 1)
        if j == -1:
            return -1
        if text[j - 1].isspace() or text[j + 1 : j + 2] == marker:
            continue
        if marker == "_" and text[j + 1 : j + 2].isalnum():
            continue
        return j


def _inline(text: str) -> list[Span]:
    """Flat inline spans of one line of text. An unclosed marker stays literal text."""
    spans: list[Span] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            spans.append({"t": "text", "text": "".join(buf)})
            buf.clear()

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE:
            buf.append(text[i + 1])
            i += 2
            continue
        if c == "`":
            m = _CODE_SPAN.match(text, i)
            if m is None:
                run = len(text) - i - len(text[i:].lstrip("`"))
                buf.append(text[i : i + run])
                i += run
                continue
            code = m.group(2)
            if len(code) > 2 and code[0] == code[-1] == " " and code.strip():
                code = code[1:-1]
            flush()
            spans.append({"t": "code", "text": code})
            i = m.end()
            continue
        if c == "[":
            m = _LINK.match(text, i)
            if m is not None:
                flush()
                spans.append({"t": "link", "text": m.group(1), "href": m.group(2)})
                i = m.end()
                continue
        if text.startswith("**", i):
            j = text.find("**", i + 3)
            if j != -1 and not text[i + 2].isspace() and not text[j - 1].isspace():
                flush()
                spans.extend(_restyle(_inline(text[i + 2 : j]), "strong"))
                i = j + 2
                continue
        elif c in "*_" and i + 1 < n and not text[i + 1].isspace() and text[i + 1] != c:
            intraword = c == "_" and i > 0 and text[i - 1].isalnum()
            j = -1 if intraword else _find_em_close(text, i, c)
            if j != -1:
                flush()
                spans.extend(_restyle(_inline(text[i + 1 : j]), "em"))
                i = j + 1
                continue
        buf.append(c)
        i += 1
    flush()
    return spans
