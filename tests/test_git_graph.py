import pytest

from harness.ui.git_graph import layout

CASES = {
    "linear": (
        [("c", ["b"]), ("b", ["a"]), ("a", [])],
        ([(0, [(0, 0)]), (0, [(0, 0)]), (0, [])], 1),
    ),
    "merge": (
        [("m", ["b", "x"]), ("x", ["a"]), ("b", ["a"]), ("a", [])],
        ([(0, [(0, 0), (0, 1)]), (1, [(0, 0), (1, 1)]), (0, [(0, 0), (1, 0)]), (0, [])], 2),
    ),
    "octopus": (
        [("o", ["a", "b", "c"]), ("c", ["r"]), ("b", ["r"]), ("a", ["r"]), ("r", [])],
        (
            [
                (0, [(0, 0), (0, 1), (0, 2)]),
                (2, [(0, 0), (1, 1), (2, 2)]),
                (1, [(0, 0), (1, 1), (2, 2)]),
                (0, [(0, 0), (1, 0), (2, 0)]),
                (0, []),
            ],
            3,
        ),
    ),
    "two_roots": (
        [("m", ["a", "z"]), ("z", []), ("a", [])],
        ([(0, [(0, 0), (0, 1)]), (1, [(0, 0)]), (0, [])], 2),
    ),
    "two_tips": (
        [("f", ["a"]), ("m", ["a"]), ("a", [])],
        ([(0, [(0, 0)]), (1, [(0, 0), (1, 0)]), (0, [])], 2),
    ),
    "cut_off": (
        [("m", ["b", "x"]), ("x", ["a"]), ("b", ["a"])],
        ([(0, [(0, 0), (0, 1)]), (1, [(0, 0), (1, 1)]), (0, [(0, 0), (1, 1)])], 2),
    ),
    "empty": ([], ([], 0)),
}


@pytest.mark.parametrize("name", list(CASES))
def test_layout_cases(name):
    rows, expected = CASES[name]
    assert layout(rows) == expected


@pytest.mark.parametrize("name", list(CASES))
def test_edges_within_lane_count(name):
    rows, _ = CASES[name]
    placed, count = layout(rows)
    for lane, edges in placed:
        assert lane < count
        for a, b in edges:
            assert a < count
            assert b < count


def test_long_linear_history():
    rows = [(f"c{i}", [f"c{i + 1}"]) for i in range(29)] + [("c29", [])]
    placed, count = layout(rows)
    assert count == 1
    assert all(lane == 0 for lane, _ in placed)


def test_input_not_mutated():
    rows, _ = CASES["merge"]
    before = [(s, list(p)) for s, p in rows]
    layout(rows)
    assert rows == before
