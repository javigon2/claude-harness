# T0.4 dry run: Phase 1 foundation

Date: 2026-10-14. Plan: `plans/phase1-foundation/plan.json` (5 orders, 3 waves).
Orchestrator: Opus 5.5 in Claude Code. Workers: the `implementer` subagent with the order's
tier as its model. Review: the `reviewer` subagent (Sonnet).

## Result

**5 of 5 orders passed on the first attempt.** The done-when bar was 4 of 5. No retries
were needed, so escalation was never exercised.

After all merges, main passes `uv run pytest -q` (39 passed), `ruff check` and
`ruff format --check`. The merged `load_plan` loads this plan and reproduces its waves:
`[['scaffold'], ['models', 'worktree'], ['events', 'plan-validate']]`.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | scaffold | haiku | pass | pass | 18 | 17.6k | 36 s |
| 2 | models | haiku | pass | pass | 10 | 17.6k | 33 s |
| 2 | worktree | sonnet | pass | pass | 9 | 17.0k | 52 s |
| 3 | events | haiku | pass | pass | 12 | 19.1k | 45 s |
| 3 | plan-validate | sonnet | pass | pass | 8 | 15.4k | 41 s |

"1st attempt" is the orchestrator's own acceptance run in the worktree, not the worker's
report. Every order's `git diff --name-only` stayed inside `files_owned`. Each reviewer
call used 12–17k tokens.

## Per order

Workers can't ask questions in this setup: a subagent runs to the end without a channel
back. So "questions" here means doubts written in `notes`. No worker raised a doubt about
the order itself.

### scaffold (haiku)
- **Exploring:** `pwd`, `git status`, `ls`, `which uv`. These were orientation checks, not
  context the order was missing.
- **Problems:** two commands ran without `cd` into the worktree. They landed in the
  orchestrator's checkout (the main repo), failed there ("No `pyproject.toml` found") and
  were re-run correctly. Main had no `pyproject.toml` yet, so this did no harm. At a later
  wave it would have run `uv sync` or the tests against main.
- **Root cause:** orchestration, not the order. Phase-0 subagents share the parent
  session's cwd, and Bash resets to it between calls. T1.5 sets `cwd` per worker, so the
  runner won't have this problem.
- All 7 literal files matched the pasted text byte for byte.

### models (haiku)
- **Exploring:** used `cat` on `config.py` and `test_smoke.py`, both already pasted whole
  in `context`. The reads were redundant, not needed.
- **Additions:** a module-level `_ID_PATTERN` regex. This is harmless, and the reviewer
  accepted it.
- **Self-report:** listed exit code 0 for all four commands but said they were "inferred
  from successful output". The Bash tool shows no exit code on success. This breaks
  implementer rule 4 ("never report an exit code you did not observe").

### worktree (sonnet)
- **Exploring:** `ls src/harness tests` only.
- The first write passed all 7 tests. No fix loop was needed.
- **Reviewer nit:** the exit code of `git rebase --abort` is not checked. The order
  doesn't say what to do if the abort itself fails. This is an order gap, minor.

### events (haiku)
- **Exploring:** `Read` on `cli.py` (owned, being edited) and `models.py` (readonly,
  pasted). Both are allowed. Reading `cli.py` before editing it is needed; reading
  `models.py` is not.
- Edited `cli.py` with a Python string-replace script instead of Edit. This is allowed
  (the file is owned), but it is fragile.
- **Self-report:** **no final JSON block.** A runner would parse this as
  `status=unknown`. It also inferred exit codes, as in `models`.
- **Docstring:** in `emit`, the worker wrote `"\\n"` where the pasted interface had
  `"\n"`. That is arguably more correct, since a raw `\n` in a docstring becomes a real
  newline. It does show that "implement every docstring" skeletons aren't checked like
  "write exactly" code.
- `follow()` checked by hand, since the order excluded tests for it: it waits for the
  file, skips blank lines, and holds a partial line until its newline arrives.

### plan-validate (sonnet)
- **Exploring:** none. The worker wrote both files, formatted them, and ran acceptance.
  It did not open a single file.
- **Reviewer note:** the overlap message prints the normalized path (`x.py` for
  `./x.py`). The rule doesn't say which value fills `<path>`. This is an order gap,
  minor.

## Orchestration findings (not order problems)

1. **Main moved during the run.** Main was amended after wave 1 (same tree, new message,
   pushed), so wave 2's `--ff-only` failed until the branches were rebased. T1.4's
   `merge()` rebases before fast-forwarding, so the runner handles a moving integration
   branch. A rewritten history still means the run must not assume main is stable.
2. **Workers report exit codes they can't see** (2 of 5 said so, and the other 3 may have
   done the same silently). Re-running acceptance in the orchestrator is the only
   reliable check, as PLAN.md already says.
3. **The JSON block was missing in 1 of 5 reports** (Haiku). The T1.5 parser's
   `status=unknown` path will be needed.
4. **Log-keeping rule:** 3 of 5 workers mentioned the global work-log rule in `notes` and
   correctly skipped it (implementer rule 9 works). This adds noise but no harm.
5. **No worker piped acceptance through `tail`** this time. The T0.3 concern didn't show
   up.
6. **The order JSON was pasted in full** into each prompt, with a one-paragraph preamble
   giving the worktree path. The runner will need the same preamble, or `cwd`.

## How hard was this test?

The Haiku orders were mostly copy-and-paste: the planner wrote `scaffold` in full and
`models` almost in full. So their passes mainly show that Haiku copies faithfully and
runs commands. The real test of the format is the two Sonnet orders, `worktree` and
`plan-validate`. Both specified behaviour and an interface but no implementation, and both
passed on the first try with almost no exploring.

The stage also contained no order whose acceptance depended on another order's
behaviour, beyond imports. This dry run says little about cross-order interface drift.

## Order gaps found (none caused a failure)

| # | Gap | Order | Kind |
|---|---|---|---|
| G1 | Placeholder in an exact message doesn't say which value fills it (raw vs normalized path) | plan-validate | order gap |
| G2 | No rule for a failing cleanup or abort command (`git rebase --abort`) | worktree | order gap |
| G3 | Interface skeletons ("implement every docstring") aren't checked like literal code; a `\n` escape in a docstring was ambiguous | events | order gap |
| G4 | Files pasted whole in `context` were also opened by the worker | models, events | order wording |
| G5 | Worker ran commands outside the worktree | scaffold | orchestration |
| G6 | Exit codes inferred, JSON block missing | models, events | worker / agent definition |

## Fixes applied (2026-10-14, approved by the user)

- `docs/work_order.md`: a "whole file" `context` item is current, so there is no need to
  open the file (G4); new rules to name what fills each placeholder (G1) and to say what
  happens when a command fails (G2).
- `.claude/skills/plan-stage/SKILL.md`: interface skeletons and their docstrings are
  syntax- and lint-checked like literal code (G3); orders state placeholders and command
  failures (G1, G2); whole files are labelled "(whole file, current)" (G4); a dry-run plan
  keeps one behaviour-specified order per tier; two new self-check questions.
- `.claude/agents/implementer.md`: start every Bash command with `cd <root> && ` (G5);
  run acceptance as `<cmd>; echo "exit=$?"` so the exit code is visible; the JSON block
  is mandatory, and a reply without one counts as `status: unknown` (G6).
