# Planner tiers

Which model plans what, how its work orders are checked, and how to turn it off.

## Model per phase

| Phase | Model | Set in |
|---|---|---|
| `project` (the project plan) | Opus | `PLANNER_TIERS` in `src/harness/config.py` |
| `quick` (quick-route plans) | Sonnet | same |
| `staging` (stage orders, incl. the draft-orders turn) | Sonnet | same |
| `fallback` | Opus | same |

`planner_tier(phase)` returns the tier. The project plan stays on Opus; work-order turns run on
one lazily created Sonnet session (`order_session_factory` in `run_planner`).

## Quality checks

Every plan the order session writes must pass `load_plan` and `check_plan_quality`
(`src/harness/order_check.py`, no model call). The latter reports:

- a `files_readonly` path that is missing from the repo and not created by an earlier (transitive)
  dependency;
- a `files_owned` path that is absolute or contains `..`;
- a context item over 6000 characters, or a total over 24000;
- an order with no steps;
- an acceptance command that does not parse.

Problems go back to the same Sonnet session (`FIX_PROMPT` or `QUALITY_FIX_PROMPT`), at most
`MAX_FIX_ROUNDS` times.

## Fallback

After the fix rounds are used up, or when the Sonnet turn errors, the order session is closed,
`Falling back to opus for the work orders: <reason>` is shown, the prompt is rerun on the Opus
session, and the rest of that call stays on Opus. A Sonnet reply that writes no plan is not a
fallback: the existing flow (a retry prompt, then asking you) applies.

## Repo digest

Order prompts end with a cached summary of the repo (see `src/harness/repo_digest.py`,
cached in `.harness/planner/repo_digest.json`, keyed on git HEAD plus `git status --porcelain`).
Each order turn says `Repo digest: reused` or `Repo digest: rebuilt (<n> lines)`.

## Switch off

```bash
HARNESS_PLANNER_ALL_OPUS=1 harness plan ...
```

With `HARNESS_PLANNER_ALL_OPUS=1` every phase is Opus, no order session is created, no checks or
digest are added, and behaviour is as before.
