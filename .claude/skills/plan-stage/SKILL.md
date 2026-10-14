---
name: plan-stage
description: Break a project stage into self-contained work orders for the agent harness. Use when asked to plan a stage, phase or set of tasks for Haiku/Sonnet workers. Produces plan.json (machine-readable) and plan.md (human summary).
---

# Plan a stage as work orders

You are the planner. Workers are cheaper models that get **one work order and nothing
else**. You do the exploring and deciding; they only execute. Every minute you spend
pasting context saves a worker from guessing.

The schema is defined in `~/dev/projects/agent-harness/docs/work_order.md`. Read it
before starting, including the complete example. Every order must follow it exactly.

## Inputs

- The stage to plan: a description, a section of a plan file, or a list of tasks.
- The target repo. Read whatever you need from it. Workers won't.

If the stage leaves a real design choice open (one that changes what gets built, not a
detail you can decide), ask the user before writing orders. Decide everything else
yourself and record it as a `decision` item in the `context` of each order it affects.

## Process

1. **Understand the stage.** Read the stage description and the code it touches.
   Write down the interfaces between tasks: function signatures, data shapes, file
   formats, CLI names, exact output strings.

2. **Cut into orders.** Each order is about 30 minutes of work or less: at most ~8
   steps and ~4 owned files. Split anything bigger. Prefer cuts along file boundaries,
   so ownership is clean.

3. **Order the work.** Set `depends_on` so an order starts only after everything it
   reads or builds on has merged. Compute the waves (an order's wave is one more than
   the latest wave among its dependencies; orders with no dependencies are wave 1).

4. **Check ownership per wave.** No path may appear in `files_owned` of two orders in
   the same wave. If two parallel orders need the same file, either put one in a later
   wave (add a dependency) or move the shared edit into one order.

5. **Write each order.** For every order:
   - Write `acceptance` first: commands that fail before the work and pass after.
     Usually the order's own new test file, then the full test suite, then the linter.
     Every test file used in acceptance and created by the order is in `files_owned`.
   - Fill `context` with literal text, never "see X":
     - excerpts of existing code the worker calls or edits, with `path, lines a-b` labels
     - the config of every checker in `acceptance` (e.g. the `[tool.ruff]` section)
     - an example test from the repo when one exists, so tests match the house style
     - your design decisions, labelled `decision`
   - **Files that don't exist yet.** If an order uses a file that an earlier order
     creates, the earlier order must specify that file's interface exactly (paste the
     signatures or even the full file into its `context`), and the later order pastes
     the same text. Never let a later order depend on how a worker happened to write
     something.
   - Make `steps` concrete: each names its file and says exactly what to add, including
     names, signatures, exact output strings, exit codes and error messages.
   - Use `out_of_scope` for the tempting extras: the neighbouring task, a refactor,
     an extra flag. Name the order that owns them when there is one.
   - `tier`: `haiku` when the order is fully specified and mechanical; `sonnet` when it
     needs judgement (algorithms, tricky tests, subprocess or concurrency); `opus` only
     when the design can't be pinned down in the order.
   - `budget`: start with `max_turns` 30 and `max_attempts` 3; raise turns for orders
     that run slow commands repeatedly.

6. **Self-check every order** before writing output. For each order, answer honestly:
   - Could Sonnet do this without asking a single question?
   - Is every piece of needed context pasted in, not just referenced?
   - Would a worker who never opens a file outside `files_owned` and `files_readonly`
     still succeed?
   - Do the acceptance commands fail today and pass only when the goal is met?
   - Is every new test file in `files_owned`?

   Fix any order that fails a check, then run the check again.

7. **Validate mechanically** with the snippet below, and fix every error it reports.

## Output

Write both files to `plans/<stage-slug>/` in the target repo, unless the user names
another location.

### `plan.json`

```json
{
  "stage": "<stage name>",
  "orders": [ <work order>, ... ]
}
```

Each work order has exactly the fields in `docs/work_order.md`. Write the JSON from a
script (a Python dict and `json.dump`) rather than by hand when `context` holds code,
so escaping is never wrong.

### `plan.md`

A human summary someone can read in under 5 minutes. No pasted context here.

1. One paragraph: what the stage delivers.
2. A waves table: wave, order id, title, tier, depends on.
3. Per order, 3–5 lines: goal, files owned, acceptance commands.
4. Decisions made: one line each, the ones the user may want to overrule.
5. Prerequisites and risks: anything the runner or the user must do first.

### Validation snippet

```bash
python3 - plans/<stage-slug>/plan.json <<'EOF'
import json, re, sys
from pathlib import PurePosixPath
REQ = ["id", "title", "goal", "tier", "depends_on", "files_owned", "files_readonly",
       "context", "steps", "constraints", "acceptance", "budget", "out_of_scope"]
plan = json.load(open(sys.argv[1]))
orders, errs = plan["orders"], []
ids = [o.get("id", "") for o in orders]
if len(ids) != len(set(ids)): errs.append("duplicate id")
for o in orders:
    i = o.get("id", "?")
    errs += [f"{i}: missing {k}" for k in REQ if k not in o]
    errs += [f"{i}: unknown field {k}" for k in o if k not in REQ]
    if not re.fullmatch(r"[a-z0-9-]+", i.lower()): errs.append(f"{i}: bad id")
    if o.get("tier") not in ("haiku", "sonnet", "opus"): errs.append(f"{i}: bad tier")
    if not o.get("files_owned"): errs.append(f"{i}: empty files_owned")
    if not o.get("acceptance") or any(not c.strip() for c in o["acceptance"]):
        errs.append(f"{i}: empty acceptance")
    if set(o.get("budget", {})) != {"max_turns", "max_attempts"}: errs.append(f"{i}: bad budget")
    for c in o.get("context", []):
        if set(c) != {"label", "content"}: errs.append(f"{i}: bad context item")
    errs += [f"{i}: unknown dependency {d}" for d in o.get("depends_on", []) if d not in ids]
wave, by = {}, {o["id"]: o for o in orders}
def w(i, seen=()):
    if i in seen: raise SystemExit(f"cycle: {' -> '.join(seen + (i,))}")
    if i not in wave:
        wave[i] = 1 + max((w(d, seen + (i,)) for d in by[i]["depends_on"] if d in by), default=0)
    return wave[i]
for i in by: w(i)
owner = {}
for o in orders:
    for f in o["files_owned"]:
        k = (wave[o["id"]], str(PurePosixPath(f)))
        if k in owner: errs.append(f"wave {k[0]}: {k[1]} owned by {owner[k]} and {o['id']}")
        owner[k] = o["id"]
for n in sorted(set(wave.values())):
    print(f"wave {n}: {sorted(i for i in wave if wave[i] == n)}")
print("\n".join(errs) or "OK")
EOF
```

Report to the user: the waves, the number of orders, the decisions they may want to
overrule, and the paths of both files.
