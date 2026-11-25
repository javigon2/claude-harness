"""Summarise results.jsonl + exchanges.json and draw the report plots."""

import json
import statistics as st
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "out"
OUT.mkdir(parents=True, exist_ok=True)

ARMS = ["old", "new", "new_def", "haiku"]
LABEL = {
    "old": "Old (HEAD)\nSonnet, default effort",
    "new": "New (uncommitted)\nSonnet, low, no thinking",
    "new_def": "New prompt\nSonnet, default effort",
    "haiku": "Candidate\nHaiku, low, no thinking",
}
COLOR = {"old": "#2a78d6", "new": "#eb6834", "new_def": "#1baf7a", "haiku": "#eda100"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
EFF = ["low", "medium", "high"]

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 10, "axes.edgecolor": GRID,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": SURF,
    "axes.facecolor": SURF, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True,
})

cases = {c["id"]: c for c in json.loads((HERE / "cases.json").read_text())}
rs = [json.loads(l) for l in (HERE / "results.jsonl").open()]
ok = [r for r in rs if not r.get("error") and r.get("parsed")]


def q(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def action(case, r):
    """What the Orchestrator would do with this routing (current code's rules)."""
    h = case.get("holder")
    if not h:
        return "start"
    if RANK[r["to"]] > RANK[h]:
        return "escalate"
    if RANK[r["to"]] < RANK[h] and h != "opus":
        return "switch"
    if h != "opus" and r.get("effort") and abs(EFF.index(r["effort"]) - EFF.index(case["holder_effort"])) >= 2:
        return "retune"
    return "forward"


summary = {}
for a in ARMS:
    xs = [r for r in rs if r["arm"] == a]
    good = [r for r in xs if not r.get("error") and r.get("parsed")]
    w = [r["wall_ms"] for r in good]
    tier_ok = [r["to"] in cases[r["case"]]["to"] for r in good]
    eff_ok = [r["effort"] in cases[r["case"]]["effort"] for r in good] if a != "old" else []
    plan_ok = [r["plan"] in cases[r["case"]]["plan"] for r in good
               if "plan" in cases[r["case"]] and r["to"] == "opus"]
    fol = [r for r in good if cases[r["case"]].get("holder")]
    keep_ok = [(action(cases[r["case"]], r) == "forward") == cases[r["case"]]["keep"] for r in fol]
    fwd = [action(cases[r["case"]], r) == "forward" for r in fol]
    # consistency: all reps of a case agree on tier
    by = {}
    for r in good:
        by.setdefault(r["case"], set()).add((r["to"], r["effort"]))
    summary[a] = dict(
        n=len(xs), errors=len(xs) - len(good),
        wall_med=st.median(w), wall_p90=q(w, 0.9), wall_mean=st.mean(w),
        connect_med=st.median([r["connect_ms"] for r in good if r.get("connect_ms") is not None]),
        api_med=st.median([r["api_ms"] for r in good if r.get("api_ms") is not None]),
        other_med=st.median([r["wall_ms"] - (r["connect_ms"] or 0) - (r["api_ms"] or 0) for r in good]),
        out_tok_med=st.median([r["out_tokens"] for r in good if r.get("out_tokens") is not None]),
        cost_mean=st.mean([r["cost_usd"] for r in good]),
        tier_acc=sum(tier_ok) / len(tier_ok),
        effort_acc=(sum(eff_ok) / len(eff_ok)) if eff_ok else None,
        plan_acc=(sum(plan_ok) / len(plan_ok)) if plan_ok else None,
        keep_acc=sum(keep_ok) / len(keep_ok),
        forward_share=sum(fwd) / len(fwd),
        stable_cases=sum(1 for v in by.values() if len(v) == 1) / len(by),
    )

# per-case table (new arm vs haiku arm), majority answer
def majority(a, cid):
    xs = [(r["to"], r["effort"]) for r in ok if r["arm"] == a and r["case"] == cid]
    if not xs:
        return "-"
    best = max(set(xs), key=xs.count)
    return f"{best[0]}/{best[1]} ({xs.count(best)}/{len(xs)})"

per_case = []
for cid, c in cases.items():
    row = {"case": cid, "expected": f"{'|'.join(c['to'])}/{'|'.join(c['effort'])}"}
    for a in ARMS:
        row[a] = majority(a, cid)
        xs = [r for r in ok if r["arm"] == a and r["case"] == cid]
        row[a + "_wrong"] = sum(1 for r in xs if r["to"] not in c["to"] or (a != "old" and r["effort"] not in c["effort"]))
    per_case.append(row)

# ---- logs
ex = json.loads((HERE / "exchanges.json").read_text())
routed = [e for e in ex if e["routing"] > 0]
chat_routed = [e for e in routed if e["to"] in ("haiku", "sonnet")]
quick = [e for e in chat_routed if e["response"] < 20000]
logs = dict(
    exchanges=len(ex), routed=len(routed), bypassed=len(ex) - len(routed),
    routing_med=st.median([e["routing"] for e in routed]),
    routing_med_before=st.median([e["routing"] for e in routed if not e.get("effort")]),
    routing_med_after=st.median([e["routing"] for e in routed if e.get("effort")]),
    n_before=len([e for e in routed if not e.get("effort")]),
    n_after=len([e for e in routed if e.get("effort")]),
    quick_n=len(quick),
    quick_share_med=st.median([e["routing"] / e["response"] for e in quick]) if quick else None,
    followups_routed=len([e for e in routed if e["action"] != "start"]),
    followups_forward=len([e for e in routed if e["action"] == "forward"]),
)
json.dump({"bench": summary, "logs": logs, "per_case": per_case}, (OUT / "summary.json").open("w"), indent=1)
print(json.dumps({"bench": summary, "logs": logs}, indent=1))
for row in per_case:
    print(row["case"].ljust(12), row["expected"].ljust(22), *(f"{row[a]:<22}" for a in ARMS))

# ---- plot 1: routing wall time per arm (dot strip + median bar)
fig, ax = plt.subplots(figsize=(8, 4.2))
for i, a in enumerate(ARMS):
    w = [r["wall_ms"] / 1000 for r in ok if r["arm"] == a]
    jitter = [i + ((k * 37) % 21 - 10) / 50 for k in range(len(w))]
    ax.scatter(jitter, w, s=14, color=COLOR[a], alpha=0.55, linewidths=0)
    m = st.median(w)
    ax.plot([i - 0.3, i + 0.3], [m, m], color=INK, lw=2, solid_capstyle="round")
    ax.text(i + 0.33, m, f"{m:.1f} s", va="center", color=INK, fontsize=9)
ax.set_xticks(range(len(ARMS)), [LABEL[a] for a in ARMS], fontsize=8.5)
ax.set_ylabel("Routing time per message (s)")
ax.set_ylim(0, 12)
for i, a in enumerate(ARMS):
    big = [r["wall_ms"] / 1000 for r in ok if r["arm"] == a and r["wall_ms"] > 12000]
    if big:
        ax.annotate(f"↑ {len(big)} off-scale: {', '.join(f'{b:.0f} s' for b in big)}", (i, 11.6),
                    ha="center", fontsize=8, color=INK2)
ax.grid(axis="x", visible=False)
ax.set_title("Router wall time, 20 messages × 3 reps (bar = median)", loc="left", color=INK, fontsize=11)
fig.tight_layout()
fig.savefig(OUT / "1_routing_time_by_arm.png", dpi=160)

# ---- plot 2: where the routing time goes (stacked horizontal medians)
fig, ax = plt.subplots(figsize=(8, 3.6))
parts = [("connect_med", "Start Claude process", "#2a78d6"),
         ("api_med", "Model call", "#eb6834"),
         ("other_med", "Close + overhead", "#1baf7a")]
for i, a in enumerate(ARMS):
    left = 0
    for key, _, col in parts:
        v = summary[a][key] / 1000
        ax.barh(i, v, left=left, color=col, height=0.55, edgecolor=SURF, linewidth=2)
        if v > 0.25:
            ax.text(left + v / 2, i, f"{v:.1f}", ha="center", va="center", color="white", fontsize=8.5)
        left += v
ax.set_yticks(range(len(ARMS)), [LABEL[a].replace("\n", " · ") for a in ARMS], fontsize=8.5)
ax.invert_yaxis()
ax.set_xlabel("Seconds (medians of each part)")
ax.grid(axis="y", visible=False)
ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in parts],
          labels=[l for _, l, _ in parts], loc="lower left", bbox_to_anchor=(0, -0.42), ncol=3, frameon=False, fontsize=8.5)
ax.set_title("Where routing time goes", loc="left", color=INK, fontsize=11)
fig.tight_layout()
fig.savefig(OUT / "2_routing_breakdown.png", dpi=160)

# ---- plot 3: quality per arm (grouped bars)
fig, ax = plt.subplots(figsize=(8, 3.6))
metrics = [("tier_acc", "Model right"), ("effort_acc", "Effort right"), ("keep_acc", "Keep/move right (follow-ups)")]
mcol = ["#2a78d6", "#eb6834", "#1baf7a"]
wdt = 0.25
for j, (key, lab) in enumerate(metrics):
    for i, a in enumerate(ARMS):
        v = summary[a][key]
        if v is None:
            ax.text(i + (j - 1) * wdt, 0.02, "n/a", ha="center", fontsize=7.5, color=INK2)
            continue
        ax.bar(i + (j - 1) * wdt, v * 100, width=wdt - 0.03, color=mcol[j], label=lab if i == 1 else None)
        ax.text(i + (j - 1) * wdt, v * 100 + 1.5, f"{v*100:.0f}", ha="center", fontsize=7.5, color=INK)
ax.set_xticks(range(len(ARMS)), [LABEL[a] for a in ARMS], fontsize=8.5)
ax.set_ylim(0, 110)
ax.set_ylabel("% of calls matching the label")
ax.grid(axis="x", visible=False)
ax.legend(frameon=False, fontsize=8.5, ncol=3, loc="upper left", bbox_to_anchor=(0, -0.22))
ax.set_title("Routing quality against hand labels", loc="left", color=INK, fontsize=11)
fig.tight_layout()
fig.savefig(OUT / "3_quality_by_arm.png", dpi=160)

# ---- plot 4: real exchanges — routing vs rest of the wait (quick chat replies)
qs = sorted(quick, key=lambda e: e["ts"])
fig, ax = plt.subplots(figsize=(9, 4.4))
for i, e in enumerate(qs):
    r = e["routing"] / 1000
    rest = (e["response"] - e["routing"]) / 1000
    ax.barh(i, r, color="#eb6834", height=0.6, edgecolor=SURF, linewidth=2)
    ax.barh(i, rest, left=r, color="#2a78d6", height=0.6, edgecolor=SURF, linewidth=2)
    ax.text(r + rest + 0.2, i, f"{100*e['routing']/e['response']:.0f}% routing", va="center", fontsize=8, color=INK2)
ax.set_yticks(range(len(qs)), [f"{e['ts'][5:16].replace('T',' ')} {e['text'][:17]}" for e in qs], fontsize=7.5)
ax.invert_yaxis()
ax.set_xlabel("Seconds from message to reply")
ax.grid(axis="y", visible=False)
ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color="#eb6834"), plt.Rectangle((0, 0), 1, 1, color="#2a78d6")],
          labels=["Routing", "Everything else (hand-off, agent)"], frameon=False, fontsize=8.5, loc="lower left", bbox_to_anchor=(0, -0.3), ncol=2)
ax.set_title("Real quick chat messages (reply < 20 s): share of the wait spent routing", loc="left", color=INK, fontsize=11)
fig.tight_layout()
fig.savefig(OUT / "4_real_wait_share.png", dpi=160)

# ---- plot 5: projected saving per routed message, by option
opts = [
    ("Low effort + no thinking (done)", (summary["old"]["wall_med"] - summary["new"]["wall_med"]) / 1000),
    ("Haiku as router", (summary["new"]["wall_med"] - summary["haiku"]["wall_med"]) / 1000),
    ("Warm router process\n(connect ahead, close in background)", (summary["new"]["connect_med"] + summary["new"]["other_med"]) / 1000),
    ("Haiku + warm process", (summary["new"]["wall_med"] - summary["haiku"]["wall_med"] + summary["haiku"]["connect_med"] + summary["haiku"]["other_med"]) / 1000),
    ("Don't block forwards\n(~25% of routed msgs in logs)", 0.25 * summary["new"]["wall_med"] / 1000),
]
fig, ax = plt.subplots(figsize=(8, 3.6))
for i, (lab, v) in enumerate(opts):
    ax.barh(i, v, color="#2a78d6", height=0.55)
    ax.text(max(v, 0) + 0.05, i, f"{v:.1f} s  ({100*v/(summary['new']['wall_med']/1000):.0f}%)", va="center", fontsize=8.5, color=INK)
ax.set_yticks(range(len(opts)), [o[0] for o in opts], fontsize=8.5)
ax.invert_yaxis()
ax.set_xlim(0, 2.2)
ax.set_xlabel("Median seconds saved per routed message (vs today's 4.3 s)")
ax.grid(axis="y", visible=False)
ax.set_title("Upside of each option (from bench medians; last is an average)", loc="left", color=INK, fontsize=11)
fig.tight_layout()
fig.savefig(OUT / "5_option_ceiling.png", dpi=160)
print("plots ->", OUT)
