import json
from pathlib import Path
from harness.ui.timing import build_thread_timing
repo = Path(".").resolve()
ids = sorted({json.loads(l)["run_id"] for l in open(".harness/planner/events.jsonl") if '"orchestrator_turn"' in l})
rows = []
for t in ids:
    tt = build_thread_timing(repo, t)
    if not tt: continue
    for ex in tt.exchanges:
        r = ex.ms.get("routing", 0)
        rows.append(dict(thread=t, ts=ex.ts, action=ex.action, to=ex.to, effort=ex.effort,
                         routing=r, response=ex.response_ms, model=ex.ms.get("model",0),
                         startup=ex.ms.get("startup",0), handoff=ex.ms.get("handoff",0),
                         tools=ex.ms.get("tools",0), text=ex.text[:50]))
json.dump(rows, open(Path(__import__("sys").argv[1])/"exchanges.json","w"), indent=1)
for r in rows:
    print(r["ts"][:16], str(r["action"]).ljust(8), str(r["to"]).ljust(6), str(r["routing"]).rjust(6), str(r["response"]).rjust(8), r["text"])
