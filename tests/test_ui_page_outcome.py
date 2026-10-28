import json

from test_ui_page import _functions, _run_node, _text


def test_phase_pill(tmp_path):
    script = (
        _functions(_text(), "phasePill")
        + """
var out = [
  phasePill({phase: 'running', outcome: null, outcome_text: ''}),
  phasePill({phase: 'stopped', outcome: 'needs_attention', outcome_text: 'needs attention'}),
  phasePill({phase: 'ended', outcome: 'completed', outcome_text: 'completed'}),
  phasePill({phase: 'ended', outcome: 'needs_attention',
    outcome_text: 'needs attention: 1 failed, 2 blocked'}),
  phasePill({phase: 'ended'}),
];
console.log(JSON.stringify(out));
"""
    )
    got = json.loads(_run_node(tmp_path, script)[-1])
    assert got == [
        {"cls": "phase phase-running", "text": "running"},
        {"cls": "phase phase-stopped", "text": "stopped"},
        {"cls": "phase outcome-completed", "text": "completed"},
        {"cls": "phase outcome-attention", "text": "needs attention: 1 failed, 2 blocked"},
        {"cls": "phase phase-ended", "text": "ended"},
    ]


def test_progress_suffix(tmp_path):
    script = (
        _functions(_text(), "fmtEta", "progressSuffix")
        + """
var out = [
  progressSuffix({phase: 'running'}, {eta: 5 * 60000}),
  progressSuffix({phase: 'running'}, {eta: null}),
  progressSuffix({phase: 'stopped', outcome: 'completed'}, {eta: null}),
  progressSuffix({phase: 'ended', outcome: 'completed'}, {eta: null}),
  progressSuffix({phase: 'ended', outcome: 'needs_attention'}, {eta: null}),
  progressSuffix({phase: 'ended'}, {eta: null}),
];
console.log(JSON.stringify(out));
"""
    )
    got = json.loads(_run_node(tmp_path, script)[-1])
    assert got == [
        " · about 5 min left",
        "",
        " · stopped",
        " · completed",
        " · needs attention",
        " · ended",
    ]


def test_next_button_text():
    text = _text()
    assert "'Plan and run stage '" in text
    assert "Plan and run next stage" in text
    assert "Plan next stage (" not in text
