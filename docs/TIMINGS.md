# Timing a request

Timings are written to the existing JSONL event logs. They use a monotonic clock
within each process and are rounded to milliseconds; `ts` remains the wall-clock
timestamp for correlating events across processes. Old events have no duration
field. No model call is needed to inspect them.

- `.harness/planner/events.jsonl`: `orchestrator_turn.routing_ms` (Sonnet
  classification, including SDK connect/close, **not** the subsequent handoff);
  `orchestrator_failed.routing_ms` when classification or handoff failed;
  `planner_reply.duration_ms` and `planner_turn_failed.duration_ms` (one SDK
  turn, including connect, tools, and model generation). The `phase` is
  `triage`, `project`, `staging`, or `quick`.
- `planner_tool_started` and `planner_tool_finished`: top-level SDK tool
  `tool_id`, name and phase; the finish includes `duration_ms` and status
  `ok`, `error`, or `missing_result`. A missing result is time until the turn
  ended, **not** a measured successful tool execution. Nested agent tools
  are not measured separately. The detail is shortened, not the full tool input.
- `.harness/runs/<run-id>/events.jsonl`: `worker_finished.duration_ms` is the
  SDK-reported worker time; `verify_finished.duration_ms` includes acceptance
  commands, diff checks and optional reviewer. Its `commands` list contains
  individual command durations. A rate-limited reviewer emits
  `verify_paused.duration_ms` instead.
- `task_started.queue_wait_ms` is time a ready task waited for the parallelism semaphore;
  `task_started.worktree_ms` is time creating its worktree (including the
  serialized Git lock). Tasks start as soon as their dependencies are done, so there are no
  wave events; `run_finished.duration_ms` is the whole run's wall time.

To find the largest measured segments, from the project root:

```bash
python3 - .harness/planner/events.jsonl .harness/runs/<run-id>/events.jsonl <<'PY'
import json
import sys

for path in sys.argv[1:]:
    rows = []
    for line in open(path, encoding="utf-8"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload", {})
        for key in ("routing_ms", "duration_ms", "queue_wait_ms"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append((value, event["type"], event.get("task_id"), key, event["ts"]))
    print(path)
    for ms, kind, task, key, timestamp in sorted(rows, reverse=True)[:15]:
        print(f"  {ms / 1000:8.1f}s  {kind:25} {str(task or '-'):20} {key:14} {timestamp}")
PY
```

These durations are overlapping measurements, not numbers to sum. For
example, a planner turn includes its tools, and a run includes its workers
and verification. The interval between a `planner_reply` and the next
`planner_message` is usually the time waiting for a person's answer, not
model latency. Comparing the same request with direct Claude still requires
matched requests and multiple runs.
