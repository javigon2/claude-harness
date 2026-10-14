from harness.models import AttemptRecord, TaskState, Usage
from harness.report import (
    escalated_tasks,
    format_table,
    money,
    report_text,
    status_text,
    tier_totals,
)
from harness.state import RunState


def _attempt(n, tier, passed, **usage):
    return AttemptRecord(
        attempt=n,
        tier=tier,
        worker_status="done" if passed else "failed",
        passed=passed,
        usage=Usage(**usage),
    )


def _three_task_run() -> RunState:
    a = TaskState(
        task_id="a",
        status="done",
        attempts=1,
        tier_used="haiku",
        usage=Usage(input_tokens=100, output_tokens=20, cost_usd=0.01),
        history=[_attempt(1, "haiku", True, input_tokens=100, output_tokens=20, cost_usd=0.01)],
    )
    b = TaskState(
        task_id="b",
        status="done",
        attempts=2,
        tier_used="sonnet",
        usage=Usage(
            input_tokens=300,
            output_tokens=50,
            cache_read_tokens=10,
            cache_creation_tokens=5,
            cost_usd=0.05,
        ),
        history=[
            _attempt(1, "haiku", False, input_tokens=100, output_tokens=10, cost_usd=0.01),
            _attempt(
                2,
                "sonnet",
                True,
                input_tokens=200,
                output_tokens=40,
                cache_read_tokens=10,
                cache_creation_tokens=5,
                cost_usd=0.04,
            ),
        ],
    )
    c = TaskState(
        task_id="c",
        status="blocked",
        attempts=1,
        tier_used="opus",
        usage=Usage(input_tokens=1000, output_tokens=200, cost_usd=0.5),
        history=[_attempt(1, "opus", False, input_tokens=1000, output_tokens=200, cost_usd=0.5)],
        replan_note="split c into two orders\nsecond line of detail",
    )
    return RunState(run_id="r2", plan_path="p.json", stage="S2", tasks={"a": a, "b": b, "c": c})


def test_format_table_widths_and_rstrip():
    out = format_table(["x", "long"], [["abc", "1"], ["d", ""]])
    assert out == "x    long\n---  ----\nabc  1\nd"
    assert not out.endswith("\n")


def test_format_table_no_rows():
    assert format_table(["ab", "c"], []) == "ab  c\n--  -"


def test_money():
    assert money(0.0793) == "$0.0793"
    assert money(0) == "$0.0000"


def test_tier_totals_ladder_order_and_skips_unused():
    totals = tier_totals(_three_task_run())
    assert list(totals) == ["haiku", "sonnet", "opus"]
    count, reviews, usage = totals["haiku"]
    assert count == 2
    assert reviews == 0
    assert usage.input_tokens == 200
    assert usage.output_tokens == 30
    assert abs(usage.cost_usd - 0.02) < 1e-9
    assert totals["sonnet"][0] == 1
    assert totals["sonnet"][2].cache_read_tokens == 10
    assert totals["sonnet"][2].cache_creation_tokens == 5
    assert totals["opus"][0] == 1
    assert totals["opus"][2].input_tokens == 1000


def test_tier_totals_skips_unused_tier_and_orders_by_ladder():
    t = TaskState(
        task_id="x",
        attempts=2,
        history=[
            _attempt(1, "opus", False, input_tokens=5),
            _attempt(2, "haiku", True, input_tokens=7),
        ],
    )
    state = RunState(run_id="r", plan_path="p", stage="S", tasks={"x": t})
    totals = tier_totals(state)
    assert list(totals) == ["haiku", "opus"]
    assert "sonnet" not in totals
    assert totals["haiku"][:2] == (1, 0)
    assert totals["opus"][:2] == (1, 0)


def _reviewed_run() -> RunState:
    reviewed = AttemptRecord(
        attempt=1,
        tier="haiku",
        worker_status="done",
        passed=False,
        usage=Usage(input_tokens=10, output_tokens=2, cost_usd=0.005),
        review_tier="sonnet",
        review_usage=Usage(input_tokens=50, output_tokens=5, cost_usd=0.04),
    )
    t = TaskState(
        task_id="x",
        status="done",
        attempts=2,
        tier_used="haiku",
        history=[
            reviewed,
            _attempt(2, "haiku", True, input_tokens=10, output_tokens=2, cost_usd=0.005),
        ],
    )
    return RunState(run_id="r5", plan_path="p", stage="S5", tasks={"x": t})


def test_tier_totals_books_review_on_review_tier():
    totals = tier_totals(_reviewed_run())
    assert list(totals) == ["haiku", "sonnet"]
    attempts, reviews, usage = totals["haiku"]
    assert (attempts, reviews) == (2, 0)
    assert usage.input_tokens == 20
    assert usage.output_tokens == 4
    assert abs(usage.cost_usd - 0.01) < 1e-9
    attempts, reviews, usage = totals["sonnet"]
    assert (attempts, reviews) == (0, 1)
    assert usage.input_tokens == 50
    assert usage.output_tokens == 5
    assert abs(usage.cost_usd - 0.04) < 1e-9


def test_report_text_review_row():
    expected = (
        "Per tier\n"
        "tier    attempts  reviews  input  output  cache read  cache write  cost\n"
        "------  --------  -------  -----  ------  ----------  -----------  -------\n"
        "haiku   2         0        20     4       0           0            $0.0100\n"
        "sonnet  0         1        50     5       0           0            $0.0400\n"
        "\n"
    )
    assert expected in report_text(_reviewed_run())


def test_escalated_tasks():
    assert escalated_tasks(_three_task_run()) == ["b"]
    empty = RunState(run_id="r", plan_path="p", stage="S", tasks={"z": TaskState(task_id="z")})
    assert escalated_tasks(empty) == []


def test_status_text_example():
    a = TaskState(
        task_id="a", status="done", attempts=1, tier_used="haiku", usage=Usage(cost_usd=0.5)
    )
    state = RunState(run_id="r1", plan_path="p", stage="S", tasks={"a": a})
    assert status_text(state) == (
        "Run r1: S\n\ntask  status  attempts  tier   cost\n----  ------  --------  -----  -------\n"
        "a     done    1         haiku  $0.5000"
    )


def test_status_text_untried_task_shows_dash_tier():
    state = RunState(run_id="r", plan_path="p", stage="S", tasks={"q": TaskState(task_id="q")})
    assert status_text(state).splitlines()[-1] == "q     pending  0         -     $0.0000"


def test_report_text_three_task_run():
    expected = (
        "Run r2: S2\n"
        "\n"
        "Per task\n"
        "task  status   attempts  tier    input  output  cache read  cache write  cost\n"
        "----  -------  --------  ------  -----  ------  ----------  -----------  -------\n"
        "a     done     1         haiku   100    20      0           0            $0.0100\n"
        "b     done     2         sonnet  300    50      10          5            $0.0500\n"
        "c     blocked  1         opus    1000   200     0           0            $0.5000\n"
        "\n"
        "Per tier\n"
        "tier    attempts  reviews  input  output  cache read  cache write  cost\n"
        "------  --------  -------  -----  ------  ----------  -----------  -------\n"
        "haiku   2         0        200    30      0           0            $0.0200\n"
        "sonnet  1         0        200    40      10          5            $0.0400\n"
        "opus    1         0        1000   200     0           0            $0.5000\n"
        "\n"
        "Totals: 3 tasks: 2 done, 0 failed, 1 blocked, 0 not run; 4 attempts; cost $0.5600\n"
        "Escalated: 1 of 3 tasks (b)\n"
        "\n"
        "Needs attention\n"
        "- c (blocked): split c into two orders"
    )
    assert report_text(_three_task_run()) == expected


def test_report_text_nothing_ran():
    state = RunState(
        run_id="r3",
        plan_path="p",
        stage="S3",
        tasks={"a": TaskState(task_id="a"), "b": TaskState(task_id="b")},
    )
    out = report_text(state)
    assert "Per tier\n(no attempts)" in out
    assert "Needs attention" not in out
    assert out.endswith(
        "Totals: 2 tasks: 0 done, 0 failed, 0 blocked, 2 not run; 0 attempts; cost $0.0000\n"
        "Escalated: 0 of 2 tasks"
    )


def test_report_text_failed_task_shows_error():
    f = TaskState(
        task_id="f",
        status="failed",
        attempts=1,
        tier_used="haiku",
        history=[_attempt(1, "haiku", False)],
        error="acceptance failed: exit 1",
        replan_note="ignored because error wins",
    )
    n = TaskState(task_id="n", status="blocked")
    state = RunState(run_id="r4", plan_path="p", stage="S4", tasks={"f": f, "n": n})
    out = report_text(state)
    assert out.endswith(
        "Needs attention\n- f (failed): acceptance failed: exit 1\n- n (blocked): -"
    )
    assert "1 failed, 1 blocked" in out
