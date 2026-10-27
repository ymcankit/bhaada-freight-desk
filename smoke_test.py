"""Live smoke test: runs every sample load through the real LLM orchestrator, then a re-plan.
Run before freezing the build:  GROQ_API_KEY=... python smoke_test.py
Reports which planner ran (llm / fallback), the outcome and the tool sequence, so you can see
whether the model is actually driving or the fallback is quietly doing the work."""
import os, re, sys, time
from pathlib import Path
import engine as E

# same key the app uses: .streamlit/secrets.toml, else the environment
sec = Path(__file__).parent / ".streamlit" / "secrets.toml"
if sec.exists():
    vals = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"', sec.read_text(), flags=re.MULTILINE))
    E.configure(key=vals.get("GROQ_API_KEY"), model=vals.get("GROQ_MODEL"), whisper=vals.get("GROQ_WHISPER"))
if not E.llm_ready() or "paste" in E.LLM["key"]:
    sys.exit("No Groq key found: put GROQ_API_KEY in .streamlit/secrets.toml or the environment.")
pool = E.build_pool()
fails = 0
for q in [x for x in E.sample_inq if x.get("load")]:
    t0 = time.time()
    r = E.run_quote(q["load"], pool, use_llm=True)
    tools = " > ".join(s["tool"] for s in r.get("trace", []))
    line = f'{q["id"]:3} {r["status"]:9} planner={r.get("planner")} {time.time() - t0:4.1f}s  {tools}'
    if r["status"] == "quote":
        line += f'  | margin {r["quote"]["margin_pct"]}%'
        rp = E.run_replan(r, E.build_pool(E.penalise({}, r["owner"]["ownerId"])), use_llm=True)
        line += f'  | re-plan {rp["status"]} ({rp.get("planner")})'
        fails += rp.get("planner") != "llm"
    fails += r.get("planner") != "llm"
    print(line)
    print("     expected:", q["expect"], "| reason:", r.get("reason", ""))
    why = [t["summary"] for t in r.get("trace", []) if t["tool"] == "fallback"]
    if why:
        print("     fallback because:", why[0])
print("\nAll runs used the LLM planner." if not fails else f"\n{fails} run(s) fell back to the deterministic planner: check the trace.")
