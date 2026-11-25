"""The page shows the agent's thinking effort: on the chat desk label and in the Timing
panel's Agent column."""

import json

from test_ui_page import _functions, _run_node, _text


def test_chat_desk_label_and_timing_agent(tmp_path):
    chats = [
        None,
        {"chat_id": "c1"},
        {"chat_id": "c2", "effort": "low"},
        {"chat_id": "c3", "effort": "high", "role": "researcher"},
    ]
    exchanges = [
        {"to": "haiku", "effort": "low", "action": "start", "turns": 1},
        {"to": "sonnet", "effort": "high", "action": "retune", "turns": 2},
        {"to": "sonnet", "action": "switch", "turns": 1},
        {"to": None, "action": "failed", "turns": 0},
    ]
    script = (
        _functions(_text(), "chatDeskLabel", "timingAgent", "roleTitle")
        + "console.log(JSON.stringify([" + json.dumps(chats)
        + ".map(function (c) { return chatDeskLabel('haiku', c); }), "
        + json.dumps(exchanges) + ".map(timingAgent)]));\n"
    )  # fmt: skip
    out = json.loads(_run_node(tmp_path, script)[-1])
    assert out == [
        [
            "Builder · haiku · new chat",
            "Builder · haiku · chat",
            "Builder · haiku · low",
            "Researcher · haiku · high",
        ],
        ["haiku/low", "sonnet/high (retune) · 2 turns", "sonnet (switch)", "— (failed)"],
    ]


def test_chat_desk_label_is_refreshed_on_update():
    text = _text()
    assert "setText(d.children[3], chatDeskLabel(tier, chat));" in text
    assert "timingAgent(x), fmtMs(x.response_ms)" in text
