# Plan: evaluating tier routing in the harness

Status: planned, nothing built yet. Written 2026-10-26.

## Goal
Find out whether the harness picks the cheapest tier that gets an order accepted, and when starting lower or higher would have been cheaper at the same quality.

## 1. Questions, and the metric that answers each
| Question | Metric |
|---|---|
| Is starting at a lower tier safe? | First-attempt pass rate per tier and order type |
| What does a lower tier save? | Cost per accepted order, counting failed attempts and escalations |
| Does escalation recover failures cheaply? | Escalation rate, and cost wasted on attempts that failed |
| How far is the current policy from ideal? | Regret: policy cost minus the cheapest passing tier (the "oracle") |
| Is acceptance too lenient at lower tiers? | Quality score from a check outside the harness (section 3) |
| Is it also fast enough? | Worker time per accepted order, kept separate from verification time |

## 2. Experiment design
1. **Order set:** about 12 real orders from the nine plans already in `plans/`. Mix small edits, multi-file changes and test-writing orders. Run each from the commit it originally started on, recorded as `base` in the run events, so the work still needs doing.
2. **Arms:** force haiku-only, sonnet-only and opus-only, plus the current "planned tier, then escalate" policy.
3. **Repeats:** three per order and arm. That is 12 x 4 x 3 = 144 runs at most.
4. **Fixed:** same acceptance commands, same worker prompt, same cache state, and `--max-cost` as a safety cap.

## 3. Quality check beyond acceptance
- Hidden tests for each order that the worker never sees, and a Sonnet reviewer scoring a sample of diffs against the order's goal.
- Report "passed acceptance but failed the quality check" per tier. That rate shows how far to trust a cheaper tier's passes.

## 4. Decision rules
- **Start lower** when the pass probability p exceeds roughly cost_low / cost_high, and the quality-check failure rate does not rise. A failed attempt still costs money and then you pay for the higher tier too, so a Haiku attempt at one tenth the cost of Opus only needs about a 10% pass chance.
- **Start higher** for order types where the lower tier's expected cost, including the failed attempt, exceeds starting at the higher tier.
- **Change the planner's tier assignments** only where the table shows a gap of at least one tier in at least two thirds of an order type's repeats.

## 5. What to build (each small)
1. A **forced-tier run option**: one `--force-tier` flag plus a checkbox in Run options.
2. A **bench script** that takes the order set, runs the arms from each base commit in fresh worktrees, and writes one row per run (order, arm, tier, pass, attempts, cost, worker time, verify time, quality-check result) to a results file.
3. A **Tier matrix view** in the Dashboard: order type by tier, with pass rate, cost and regret, and the oracle policy next to the current one.
4. The **quality check** from section 3, wired into the bench script.

## 6. Budget and time (rough)
- **Cost:** about $15-20, extrapolated from only five real attempts so far (Haiku about $0.01, Sonnet about $0.17, Opus about $0.20-0.37). Treat it as a guess. A small pilot, one order across all arms, would firm it up before the full run.
- **Time:** verification runs the full test suite for about four minutes per attempt, so that dominates. At five runs in parallel, expect roughly 1.5-2 hours.

## 7. Risks
- **Too few orders** make the rates noisy. Twelve orders cannot prove small differences, so report intervals, not just means.
- **Prompt caching** can make later runs look cheaper. Run the arms in interleaved order, not in blocks.
- **These tests are specific to this repo.** The results may not carry over to other kinds of projects.

## Open decisions
1. Is about $15-20 and a couple of hours acceptable, or should the first pass be the one-order pilot?
2. For the quality check: hidden tests, a Sonnet review of sampled diffs, or both?
