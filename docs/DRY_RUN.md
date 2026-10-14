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

# T1.5: Worker

Date: 2026-10-14. Plan: `plans/t1.5-worker/plan.json` (2 orders, 2 waves, every order on
opus by the user's choice for this build). Orchestrator: Opus 5.5 in Claude Code. Workers:
the `implementer` subagent with model opus, one worktree per order, created and merged
with `harness.worktree.WorktreeManager` (run id `t15`). Review: the `reviewer` subagent
(opus).

## Result

**2 of 2 orders passed on the first attempt**, in the orchestrator's own acceptance run
and in review. Merged on main as `56409f1` (worker-core) and `029bf6b` (sdk-worker).
Main: 96 tests, ruff clean.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | worker-core | opus | pass | pass | 17 | 46.6k | 124 s |
| 2 | sdk-worker | opus | pass | pass | 7 | 33.5k | 88 s |

Both diffs stayed inside `files_owned`. Both workers wrote the JSON block and printed
real exit codes (`; echo "exit=$?"`). Both mentioned the global work-log rule in `notes`
and skipped it (rule 9 works).

## SDK interface as built (claude-agent-sdk 0.2.167, CLI 2.1.299 bundled)

- `query(prompt=..., options=ClaudeAgentOptions(...))` returns an async iterator.
  `SdkWorker.options()` sets `model`, `system_prompt` (a plain str, which replaces
  Claude Code's prompt), `tools` and `allowed_tools` (the six tools), `permission_mode="acceptEdits"`,
  `cwd`, `max_turns`, `setting_sources=[]`, `strict_mcp_config=True`,
  `verbatim_prompts=True`, and `env` (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`,
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`).
- `setting_sources=None` (the default) loads **all** sources, including
  `~/.claude/CLAUDE.md`. That is why earlier headless sessions wrote to the work log.
  Auto memory and claude.ai connectors are not controlled by `setting_sources`.
- A failed session yields its `ResultMessage(is_error=True)` and then raises
  `ResultError`, so the worker keeps the usage it saw before the exception.
- `ResultMessage.usage` covers the main loop only. `model_usage` (camelCase, per model)
  covers every call. `total_cost_usd` is a client-side estimate.

## Manual SdkWorker call (done-when)

One real call: opus, a trivial order (create `greet.py`), in a throwaway `git init` repo,
`max_turns=10`.

- `status=done`, 3 turns, 11.3 s. The orchestrator's own acceptance run exited 0.
- **Cost $0.0793.** opus $0.07919, plus $0.00016 for a Claude Code internal haiku call
  that shows up only in `model_usage`.
- Usage, summed over `model_usage`: input 1,450, output 533, cache read 16,332, cache
  creation 8,214. The cache writes use the 1h TTL.
- Isolation: init `tools` = exactly the six; `mcp_servers` = [] (no claude.ai connectors);
  `permissionMode` = acceptEdits; no permission denials; work log hash unchanged.

## Findings

| # | Finding | Where | Kind |
|---|---|---|---|
| G7 | The order said to catch `JSONDecodeError` and `ValidationError` only, but `json.loads` raises a plain `ValueError` for ints over 4300 digits (and can hit `RecursionError`). That contradicts the order's own "never raises". The worker caught `(ValueError, RecursionError)` and said so. | worker-core | order gap (planner) |
| G8 | With `setting_sources=[]` the init message still lists 19 built-in skills, 5 agents and 4 builtin plugins. They can't be called (no Skill or Agent tool), but they cost listing tokens. Possible hardening: `skills=[]`, `agents={}`. | SdkWorker | SDK behaviour |
| G9 | The worker's Python run left `__pycache__/` in an otherwise clean repo. T1.6's "diff only touches files_owned" check must ignore untracked caches (respect `.gitignore`, or skip `__pycache__`/`.pytest_cache`/`.venv`), or every Python task fails. | T1.6 input | design note |
| G10 | The 24 KB orders (byte-exact text inside) were handed to the implementer as a file path instead of pasted. The runner's `build_prompt` pastes them, so this applies only to Phase-0-style runs. | orchestration | process |

None of these is a work-order format problem, so `work_order.md` and `plan-stage` are
unchanged. Note for planners: when an order says a function "never raises" and also
lists the exceptions to catch, check that the list really covers every case.

# T1.6–T1.7: Verifier and router

Date: 2026-10-14. Plan: `plans/t1.6-t1.7/plan.json` (3 orders, 3 waves, all opus by the
user's choice for this build). Orchestrator: Opus 5.5 in Claude Code, running without the
user (decisions recorded in the plan and the final report). Workers: the `implementer`
subagent with model opus, one worktree per order via `WorktreeManager` (run id `t16`).
Review: the `reviewer` subagent (opus). Each order was handed over as a JSON file path.

## Result

**3 of 3 orders passed on the first attempt**, in the orchestrator's own acceptance run and
in review. Merged on main as `fbde06e` (review), `9683444` (verify) and `b2adf49` (router).
Main: 196 tests, ruff clean.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | review | opus | pass | pass | 7 | 33.5k | 109 s |
| 2 | verify | opus | pass | pass | 12 | 34.8k | 169 s |
| 3 | router | opus | pass | pass | 9 | 38.1k | 134 s |

Reviewer calls: 33.0k, 33.6k and 37.0k tokens. All diffs stayed inside `files_owned`; the
pasted whole-file targets (`models.py`) matched byte for byte. Every worker printed real
exit codes and wrote the JSON block, and every one mentioned the global work-log rule in
`notes` and skipped it.

## What the workers did beyond the order

- **verify:** replaced the pinned `await asyncio.wait_for(proc.wait(), timeout_s)` with a
  private `_wait_exit` that polls `proc.returncode` every 20 ms (see G11). It read
  `asyncio/base_subprocess.py` to confirm, and said so in `notes`. The reviewer judged the
  deviation necessary and within intent.
- **router:** one extra test (a monkeypatched `verify` decides the outcome), which checks a
  hook the order asked for. The usage test derives the review cost from the number of
  reviewer calls instead of fixing it.

## Order gaps

| # | Gap | Order | Kind |
|---|---|---|---|
| G11 | The pinned `run_command` algorithm waited with `Process.wait()`, which on Python 3.12 only returns after every pipe closes, so a background child holding stdout blocks it. The order's own test (`sleep 30 & echo bg` returns within 10 s) could not pass as specified. | verify | order gap (planner) |

G11 is planner knowledge, not a format problem, so `work_order.md` is unchanged. It is the
second gap of this kind (after G7), so `plan-stage` got one self-check question: where an
order pins both an algorithm and a test of it, or says "never raises" and lists the
exceptions, run that algorithm against that test in a scratch file first.

# T1.8–T1.9: Scheduler, CLI and report

Date: 2026-10-14. Plan: `plans/t1.8-t1.9/plan.json` (5 orders, 3 waves, all opus). Same
setup as T1.6–T1.7 (run id `t18`); the two orders of each of waves 1 and 2 ran in parallel.

## Result

**5 of 5 orders passed on the first attempt**, in the orchestrator's own acceptance run and
in review. Merged on main as `5892353` (state), `ec12415` (worktree-discard), `19c6467`
(report), `20fd8f7` (scheduler) and `8c0eda7` (cli). Main after the stage: 246 tests, ruff
clean.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | state | opus | pass | pass | 6 | 18.0k | 72 s |
| 1 | worktree-discard | opus | pass | pass | 9 | 20.6k | 69 s |
| 2 | scheduler | opus | pass | pass | 12 | 44.8k | 261 s |
| 2 | report | opus | pass | pass | 5 | 21.6k | 86 s |
| 3 | cli | opus | pass | pass | 7 | 31.6k | 114 s |

Reviewer calls: 16.8k, 17.8k, 37.7k, 21.2k and 29.9k tokens. All diffs stayed inside
`files_owned`; `worktree.py` matched its pasted target byte for byte.

## What the workers did beyond the order

- **scheduler:** a private `_emit` helper; `_git_out` also turns an `OSError` into
  `WorktreeError`, and `_run_task` also catches `OSError`, so "never raises" holds when
  `create` fails writing `.git/info/exclude`. The reviewer judged all three within intent
  and found no lock, semaphore or state-save bug. It read `worker.py` and `verify.py`, which
  were not in `files_readonly` (G12).
- **cli:** `raise AssertionError("unreachable")` after each `_fail(...)`, because the
  pinned `_fail` is annotated `-> None` (G13).
- **worktree-discard:** counts `git worktree list --porcelain` entries instead of searching
  for the path, because macOS temp paths show up as `/private/var/...`.

## Order gaps and orchestration findings

| # | Gap | Order | Kind |
|---|---|---|---|
| G12 | The scheduler order pasted the router and worker interfaces but not `verify.py`'s or FakeWorker's full behaviour, so the worker opened both files to check details. | scheduler | order gap (minor) |
| G13 | The pinned `_fail(message) -> None` always raises; it should have been `-> NoReturn`, so callers' return types narrow without dead `raise` lines. | cli | order gap (planner) |
| O1 | `WorktreeManager.cleanup()` removes **every** worktree under `.harness/worktrees`, including those of other runs; only branches are filtered by run id. Cleaning up `t18` deleted the in-flight `t110/planner` worktree (its branch and commit survived and were re-attached). | worktree.py (T1.4) | harness bug, open |
| O2 | The orchestrator's own scope check diffed against `main`, which had moved after a parallel merge, and reported the other order's files as "outside". Fixed by diffing against `git merge-base HEAD main`. The runner's verifier uses the recorded base sha, so it is not affected. | orchestration | process |

No format change: G12 and G13 are planner care, covered by the existing "paste every
interface the worker calls" and lint-the-skeleton rules.

# T1.10: `harness plan`

Date: 2026-10-14. Plan: `plans/t1.10-plan/plan.json` (2 orders, 2 waves, both opus; run id
`t110`). `planner` ran in parallel with T1.8–T1.9's `cli` order (no shared files);
`cli-plan` was regenerated after `cli` merged, so it pasted the current `cli.py`.

## Result

**2 of 2 orders passed on the first attempt**, in the orchestrator's own acceptance run and
in review. Merged as `945f19c` (planner) and `6f91cdd` (cli-plan). Main: **279 tests**,
ruff clean; `harness validate` accepts all five plans in `plans/`.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | planner | opus | pass | pass | 7 | 36.7k | 153 s |
| 2 | cli-plan | opus | pass | pass | 10 | 25.8k | 124 s |

Reviewer calls: 38.3k and 22.1k tokens.

## SDK facts used (installed claude-agent-sdk 0.2.167 + docs, via a claude-code-guide agent)

- Skills in `<dir>/.claude/skills/` load for `add_dirs=[dir]` when `setting_sources`
  includes `"project"`; `add_dirs`' own CLAUDE.md files do not load.
- With `skills=[...]` and `setting_sources=None`, the SDK silently sets
  `setting_sources=["user", "project"]` (`_apply_skills_defaults` in
  `subprocess_cli.py`), which would load `~/.claude`. The planner passes `["project"]`.
- `"project"` also loads CLAUDE.md from every parent of `cwd` (e.g. `~/dev/CLAUDE.md`);
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` turns all CLAUDE.md loading off. Whether it also
  affects skills is not documented: the first real `harness plan` must check the init
  message's `skills` list.
- In a `ClaudeSDKClient` session every turn has its own ResultMessage, but
  `total_cost_usd` and `model_usage` are running totals, so the planner reports the last one.

## What the workers did beyond the order

- **planner:** catches `(PlanError, ValueError, OSError)` instead of the pinned
  `(ValueError, OSError)` so the binding `PlanError` import is used (G14).
- **cli-plan:** typer renders the argument as lowercase `{description}` in `--help`, so the
  help test compares case-insensitively (the reviewer called it weak but acceptable). It
  ran one acceptance command a second time with `>/dev/null`, against implementer rule 4
  (no harm: the orchestrator's run is the one that counts).

## Order gaps

| # | Gap | Order | Kind |
|---|---|---|---|
| G14 | The pinned skeleton imported `PlanError` but the pinned flow never named it, so plain `ruff check` would fail (F401) unless the worker changed the flow. The skeleton was linted with F401 ignored, which hid this. | planner | order gap (planner) |
| G15 | The order asserted `DESCRIPTION` in `plan --help`; the installed typer prints `{description}`. Exact CLI help text should be checked by the planner before pinning. | cli-plan | order gap (minor) |

G14 is a hole in the skeleton lint rule: ignoring F401 for `...` bodies also hides imports
that the pinned algorithm never uses. Fix applied to `plan-stage`: after linting a
skeleton with F401 ignored, check that every import is used by the pinned decisions.

# Build summary (T1.6–T1.10)

- **10 of 10 orders passed on the first attempt**, all on Opus, with reviewer passes; no
  retries, so the build itself never needed escalation. Main went from 96 to 279 tests.
- Worker tokens 306k across 10 orders; reviewer tokens 288k across 10 reviews (subagent
  token counts as reported by the Agent tool).
- Not exercised for real (paid, needs the user): `SdkReviewer`, `SdkPlannerSession`, and
  `harness run` with SdkWorker. T1.11 is their first real use; see `docs/RESULTS.md`.

# T1.11 fixes: open issues before the MVP run

Date: 2026-10-14. Plan: `plans/t1.11-fixes/plan.json` (6 orders, 2 waves, all opus; run id
`t111f`). Same setup as T1.6–T1.10: one worktree per order via `WorktreeManager`, the
`implementer` subagent (opus) given the order as a JSON file path, the orchestrator's own
acceptance run and scope check against `git merge-base HEAD main`, then the `reviewer`
subagent (opus). The three wave-1 orders ran in parallel. `worktree-per-run` was added
after the other five merged, when the post-merge rehearsal found O3.

## Result

**5 of 6 orders passed on the first attempt**; `planner-session-init` needed one retry,
caused by the order (G16), not the worker. Every orchestrator acceptance run passed and
every review passed; all diffs stayed inside `files_owned`. Merged on main as `448a67c`
(cli-noreturn), `dc49e3b` (worktree-cleanup), `5bac693` (sdk-session-init), `671bbba`
(router-session-init), `1f2e431` (planner-session-init) and `9fee63a`
(worktree-per-run). Main: **304 tests**, ruff clean; `harness validate` accepts every
plan in `plans/`.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | worktree-cleanup | opus | pass | pass | 7 | 18.3k | 119 s |
| 1 | sdk-session-init | opus | pass | pass | 5 | 34.7k | 124 s |
| 1 | cli-noreturn | opus | pass | pass | 4 | 14.9k | 94 s |
| 2 | router-session-init | opus | pass | pass | 5 | 19.3k | 107 s |
| 2 | planner-session-init | opus | pass (constraint broken, G16) | pass after retry | 6 + 2 | 26.6k + 1.5k | 122 + 59 s |
| 2 | worktree-per-run | opus | pass | pass | 4 | 14.9k | 92 s |

Reviewer calls: 15.0k, 32.8k, 14.2k, 17.5k, 25.8k and 14.3k tokens.

## Issues fixed

| # | Issue | Fix |
|---|---|---|
| O1 | `cleanup()` removed every run's worktrees | only worktrees whose branch is under `refs/heads/harness/<run_id>/` (r1 vs r10 and detached worktrees tested) |
| O3 | a new run's task failed with 0 attempts ("already exists") when an earlier run had kept a worktree with the same task id (found by the rehearsal below) | worktrees live in `.harness/worktrees/<run_id>/<task_id>` |
| G8 | built-in skills/agents listed in worker and reviewer sessions | `skills=[]` + `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1` + `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` (see below: it cost 0 tokens) |
| G13 | `_fail -> None` forced dead raises | `-> NoReturn`, 6 raises removed |
| — | no way to check model, tools, login and isolation of a real session | `session_init` events (role worker/reviewer) in `events.jsonl`; `harness plan` prints the planner's init and writes `<repo>/.harness/planner/session_init.json` |

## G8 check (no model call)

A `ClaudeSDKClient` that connects but never sends a prompt makes no model call, yet
`get_server_info()` and `get_context_usage()` answer. With worker options:

| Variant | commands | agents | context (excl. buffer) |
|---|---|---|---|
| as before | 54 | 5 | system prompt 10 + tools 4,333 |
| `skills=[]` | 54 | 5 | same |
| + the two env vars | 38 | 0 | same |

- The worker and reviewer context **never held a skill or agent listing**, because they
  have no Skill or Agent tool. G8 cost 0 tokens; the fix tidies the init listing and
  guards against a future tool-list change.
- `agents={}` is a no-op in SDK 0.2.167 (`if self._agents:` never sends an empty dict).
  The init `skills` array is not evidence of what the model sees (docs, and the table).
- Planner options, same probe: skills in context = exactly `plan-project` (114 tokens) and
  `plan-stage` (79); `memoryFiles: []`, so `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` does not
  drop project skills (the open question from T1.10). The harness's three project agents
  are counted in context (268 tokens) although the planner has no Agent tool. The account
  shows `subscriptionType: Claude Enterprise`, `apiProvider: firstParty`.

## Free rehearsal (`harness run --fake`)

A copy of `examples/toy_repo` (git init, one commit on main) and a hand-written 4-order
plan: a passing `uv run pytest -q` order, an independent one, one whose acceptance can't
pass (max_attempts 3) and its dependent. Run 1: waves in order, escalation
haiku → sonnet → opus, `blocked` with a replan note, the dependent `blocked`, exit 1,
`status`/`report`/`state.json` consistent, `--resume` re-ran only the unfinished task, the
blocked task's worktree kept for inspection. Run 2 (new run id, same repo) exposed O3;
after the fix, two runs on one repo each keep their own `needs-file` worktree.

## Order gaps

| # | Gap | Order | Kind |
|---|---|---|---|
| G16 | The pinned `format_session_init` docstring held a literal model id, against the order's own "model ids only in config.py" constraint. The worker copied it and flagged the conflict; fixed on retry. | planner-session-init | order gap (planner) |
| G17 | The order said 7 dead raises; there were 6. | cli-noreturn | order gap (minor) |

G16 is a hole in the self-check, so `plan-stage` got one more question: does all pinned
text obey the order's own constraints (grep it for what they forbid)?

# T1.11 findings: H1-H3

Date: 2026-10-14. Plan: `plans/t1.11-findings/plan.json` (4 orders, 2 waves, all opus;
run id `t111h`). Same process as above. Worktrees now use the per-run layout
(`.harness/worktrees/t111h/<task>`) from O3.

## Result

**4 of 4 orders passed on the first attempt**, in the orchestrator's own acceptance run and
in review; all diffs stayed inside `files_owned`; no model id outside `config.py`. Merged as
`6ec49a7` (attempt-review-usage), `9406e89` (worktree-ensure-excluded), `a037837`
(report-review-tier) and `cb4ba98` (plan-stage-n). Main: **319 tests**, ruff clean.

| Wave | Order | Tier | 1st attempt | Reviewer | Tool calls | Worker tokens | Time |
|---|---|---|---|---|---|---|---|
| 1 | attempt-review-usage | opus | pass | pass | 4 | 19.8k | 113 s |
| 1 | worktree-ensure-excluded | opus | pass | pass | 4 | 15.5k | 120 s |
| 2 | report-review-tier | opus | pass | pass | 5 | 20.2k | 93 s |
| 2 | plan-stage-n | opus | pass | pass | 10 | 39.8k | 140 s |

Reviewer calls: 18.3k, 14.5k, 19.1k and 36.9k tokens.

## What changed

- **H1:** `AttemptRecord.usage` is the worker session only; the review is in
  `review_usage` on `review_tier`. `tier_totals` returns (attempts, reviews, usage) and
  the per-tier table has a `reviews` column, so a haiku run with sonnet reviews shows a
  sonnet row. State files written before the fix (e.g. T1.11's `t111`) still show the old
  single row: no migration.
- **H2:** `harness plan --stage N` plans stage N of an approved PROJECT_PLAN.md (no
  description; it refuses if the plan is missing or not approved).
- **H3:** all planner output lives in `<repo>/.harness/plans/` (`PROJECT_PLAN.md`,
  `stage-<n>/plan.json`, `plan.md`), and `harness plan` first calls the new
  `harness.worktree.ensure_excluded(repo)`, so it is never committed or linted.

## Order gaps

None. The pinned table text was produced by running the algorithm in a scratch file first,
and the pinned snippets were linted and grepped for model ids (the G16 check) before
handing off.

# Backend stage: progress, rate limits, cost cap

Done in one session by a single Sonnet 5.5 agent (no orders), each step on its own task branch
via `WorktreeManager` (run id `be1`), merged fast-forward. No paid model calls; no changes to
`pyproject.toml`/`uv.lock`.

| Step | Task | Main sha | Tests |
|---|---|---|---|
| 1 | worker-progress | `bef6ad9` | 330 |
| 2a | rate-limit-core (models, worker, review) | `f0d3cb9` | 340 |
| 2b | rate-limit-pause (router, scheduler, report, cli) | `9c112b0` | 348 |
| 3 | cost-cap | `a87b77a` | 355 |

Baseline was 319 tests (main moved only by docs commits meanwhile).

## Where the prompt was wrong or silent

- **Exit code for a cost stop.** The prompt reused "any task `paused`" for exit 3, but a cost
  stop at a task boundary leaves only `pending` tasks, so no task is paused. `Scheduler` now
  has a public `stop_reason`; the CLI exits 3 with the resume hint when a task is paused *or*
  `stop_reason` is set. The message uses the paused task's error, else `stop_reason`.
- **Step 2 split into two branches** (core, then pause), as the prompt allowed.
- **`rate_limited` forces status `"error"`** even if the session ended with a successful
  result after a `rejected` event (the prompt says any of the three signals counts).
- **Reviewer rate limit:** the worker usage is already in `state.usage` before verification,
  so only the review usage is added on pause (no double count).
- **Cap text rounds to cents:** `--max-cost 0.015` prints "passed the $0.01 cap". The
  `budget_exceeded` payload keeps the exact floats.
- **`budget_exceeded` ordering:** it is queued by the sync `_should_stop()` and emitted from
  `_run_task` (before a task starts, and right after `router.route` returns), so it can come
  after the router's own `task_paused` for the same stop.
- **Stale base:** the prompt said main was at `cb4ba98`; the work started from `cf040e3`
  (plus a docs commit `74a0ca7` landed meanwhile). No code contradiction.

## New status and events (for the UI)

`TaskStatus` gains **`paused`**: the task stopped on purpose (rate limit, cost cap, stop
reason); its worktree is kept, `attempts` does not count the interrupted attempt, `error`
holds the reason. `--resume` restarts it from attempt 1. Tasks never started stay `pending`.

| Event | `task_id` | Payload |
|---|---|---|
| `worker_progress` | yes | `{"attempt": int, "tier": str, "tool": str, "detail": str}` (`detail` <= 80 chars, may be `""`; emitted at most every 2 s unless the tool name changes) |
| `task_paused` | yes | `{"attempt": int, "tier": str, "reason": str, "resets_at": int \| null}` (`resets_at` = unix timestamp, only for rate limits) |
| `run_stopping` | no | `{"reason": str}` (after the wave in which the stop was set; later waves never start) |
| `budget_exceeded` | no | `{"cost_usd": float, "max_cost_usd": float}` (once per run) |
| `run_finished` | no | adds `"paused": int` and `"stopped_reason": str \| null` to `done/failed/blocked/cost_usd` |

Rate-limit `reason`: `rate limited; resets at <ISO-8601 UTC>` or `... resets at unknown`.
Cost `reason`: `estimated cost $X.XX passed the $Y.YY cap`.

## CLI

`harness run ... --max-cost USD` (must be > 0, else exit 1). Exit **3** = the run stopped
(paused task or stop reason) and the last line is
`Run stopped: <reason>. Resume with: harness run <plan> --repo <repo> --resume [--review]`.
Exit 1 stays "finished but not all done". `Totals:` now reads
`..., {blocked} blocked, {paused} paused, {other} not run; ...`.

## Rehearsal (free, `--fake`)

3 chained orders on a copy of `examples/toy_repo`, fake worker costing $0.01 per attempt,
`--max-cost 0.015` (worker patched through `harness.cli._make_worker`, since `--fake` costs
nothing): a and b merged, c stayed `pending`, exit 3, the resume hint printed, events
`budget_exceeded` -> `wave_finished` -> `run_stopping` -> `run_finished` (`stopped_reason` set),
`harness report` showed "2 done, 0 paused, 1 not run". `--resume` without the cap ran c and
finished 3/3 done. The rate-limit pause path is covered by the tests only (no way to trigger a
real one for free).
