# Planning timings

`harness planning-report` turns the planner event log (`.harness/planner/events.jsonl`)
into a per-session timing report.

## Logged events

Per planner session (`run_id` = session id): `planner_started` (stage), `planner_triage` /
`planner_done` (route), `planner_message` and `planner_reply` (phase, `duration_ms`, cumulative
`cost_usd`), `planner_tool_finished` (tool, `duration_ms`, status).

## Capture a baseline

Make three real requests in the UI: one quick plan and two project plans. Then read them:

```bash
harness planning-report --last 3
```

Options: `--repo PATH`, `--session ID`, `--json`. Exit code 1 means no sessions found.

## Reading the report

- **Model time** is the sum of turn durations; **User wait** is the gap between a reply and
  the next message, so it is not model latency.
- Nested agent tools are not timed.
- A tool with `missing_result` never returned before the turn ended; its time is not added to
  the totals.
- Sessions logged before timings existed show no durations and no tool table.

## Before and after

To compare Sonnet order sessions against all-Opus planning, rerun the three baseline requests
twice:

1. **Before:** with `HARNESS_PLANNER_ALL_OPUS=1` set.
2. **After:** without it.

Then compare `harness planning-report --last 3` for each set: model time and cost, plus the
`Repo digest: reused` lines in the output (the repo digest is built once, then reused while git
HEAD and the working tree are unchanged). Sonnet orders are fine when fallbacks (`Falling back
to opus ...`) are rare and run escalations do not rise.
