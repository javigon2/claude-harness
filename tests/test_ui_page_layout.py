import re

from test_ui_page import _text


def _header(text: str) -> str:
    return text[text.index("<header>") : text.index("</header>")]


def test_three_columns_in_order():
    text = _text()
    main = text[text.index('<main id="layout">') : text.index("</main>")]
    places = [main.index(f'id="{i}"') for i in ("sidebar", "left", "right")]
    assert places == sorted(places)
    assert '<aside id="sidebar"' in main
    for col in ("sidebar", "left", "right"):
        assert re.search(rf"#{col} \{{[^}}]*overflow: auto;", text), col


def test_header_keeps_only_the_actions():
    head = _header(_text())
    for gone in ("brand", "run-picker", "run-sub-picker", 'id="phase"', "run-progress"):
        assert gone not in head, gone
    for kept in ("btn-plans", "btn-report", "btn-dashboard", "btn-stop-run", "btn-run-tests"):
        assert f'id="{kept}"' in head, kept


def test_brand_and_run_picker_live_in_the_sidebar():
    text = _text()
    side = text[text.index('<aside id="sidebar"') : text.index("</aside>")]
    assert "agent harness" in side and 'class="brand-mark"' in side
    assert 'id="chat-search"' in side and 'id="chat-list"' in side
    assert '<select id="run-picker" class="hidden" aria-label="Run"></select>' in side
    assert 'id="btn-new-chat"' in side and 'id="btn-sidebar-toggle"' in side


def test_run_sub_picker_phase_and_progress_head_the_agent_room():
    text = _text()
    room = text[text.index('<div id="room-bar">') : text.index('<div id="run-options"')]
    for i in ("phase", "run-sub-picker", "run-progress-box"):
        assert f'id="{i}"' in room, i
    assert text.index('id="run-options"') < text.index('id="office"')


def test_collapse_is_kept_in_local_storage_and_goes_to_half_and_half():
    text = _text()
    assert "'harness.sidebar'" in text
    for m in re.finditer(r"window\.localStorage", text):
        assert "try {" in text[max(0, m.start() - 120) : m.start()]
    assert re.search(r"#layout\.collapsed \{[^}]*grid-template-columns: 56px 1fr 1fr;", text)
    # the strip shows expand, New chat and search; the labels go
    assert re.search(r"#layout\.collapsed #sidebar \.lbl", text)
    assert 'id="btn-sidebar-search"' in text and "strip-only" in text
    assert "wireSidebar();" in text


def test_narrow_windows_use_an_overlay_sidebar_and_stack_the_columns():
    text = _text()
    m = re.search(r"@media \(max-width: 900px\) \{(.*?)\n\}", text, re.S)
    assert m
    css = m.group(1)
    assert "#layout, #layout.collapsed { grid-template-columns: 1fr;" in css
    assert re.search(r"#sidebar \{[^}]*position: fixed;[^}]*translateX\(-100%\)", css)
    assert "#layout.sidebar-open #sidebar { transform: none; }" in css
    assert 'id="btn-sidebar-open"' in _header(text)
    assert "sidebarOpen(false);" in text


def test_page_still_self_contained():
    text = _text()
    assert "http://" not in text and "https://" not in text


def test_hidden_wins_over_id_rules_that_set_display():
    # an id rule that sets display beats the plain .hidden: the empty progress bar and the
    # run options showed in read-only mode, and the sidebar buttons would have too
    text = _text()
    after = text[text.index("#btn-sidebar-open, #sidebar-scrim") :]
    m = re.search(r"\n(#sidebar button\.hidden[^{]*)\{ display: none; \}", after)
    assert m
    rule = m.group(1)
    for sel in ("#sidebar button.hidden", "#run-progress-box.hidden", "#run-options.hidden"):
        assert sel in rule, sel
    for sel in ("#run-progress-box", "#run-options"):
        assert re.search(rf"\n{sel} \{{[^}}]*display: flex", text), sel


def test_transcripts_stay_at_the_bottom_when_their_box_resizes():
    text = _text()
    m = re.search(r"  function keepAtBottom\(id\) \{.*?\n  \}\n", text, re.S)
    assert m
    body = m.group(0)
    assert "ResizeObserver" in body and "scrollTop = list.scrollHeight" in body
    assert "addEventListener('scroll'" in body
    call = re.search(r"\[([^\]]*)\]\.forEach\(keepAtBottom\)", text)
    assert call
    for i in ("transcript", "orch-transcript", "chat-transcript", "timeline"):
        assert f"'{i}'" in call.group(1), i


def test_picking_the_live_planner_or_thread_shows_its_panel_after_the_run_opens():
    text = _text()
    pick = re.search(r"  function pickChat\(value\) \{.*?\n  \}\n", text, re.S)
    assert pick
    body = pick.group(0)
    assert "'opus'" in body and "'orch'" in body and "wantPanel = want" in body
    show = re.search(r"  function showLive\(want\) \{.*?\n  \}\n", text, re.S)
    assert show and "showOpus()" in show.group(0) and "selectTask(null)" in show.group(0)
    # open() resets the left panel, so choose() applies the pick after it
    choose = re.search(r"  function choose\(\) \{.*?\n  \}\n", text, re.S)
    assert choose
    c = choose.group(0)
    assert c.index("open(sel.run)") < c.index("showLive(wantPanel)")
