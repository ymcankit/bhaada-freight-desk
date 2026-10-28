"""Offline tests for the engine and the orchestrator loop (no API key, no network).
Run:  python test_engine.py
The LLM is replaced by a scripted fake that reads tool results and calls tools the way the real model would.
"""
import json
import engine as E

POOL = E.build_pool()
HERO = dict(origin="Bhiwandi", destination="Surat", weight_ton=18, truck_type="32 MXL", pickup_when="kal subah")

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

def tool_call(i, name, args):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

def fake_llm(script):
    """script(state) -> (name, args) given the last tool results; mimics a tool-calling model."""
    state = {"i": 0}
    def chat(messages, tools=None, json_mode=False, **k):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        name, args = script(results)
        state["i"] += 1
        return {"role": "assistant", "content": "", "tool_calls": [tool_call(state["i"], name, args)]}
    return chat

def sensible_policy(results):
    """What we expect a good model to do: check, search (substituting if needed), broadcast, price, propose."""
    if not results:
        return "check_load", {}
    last = results[-1]
    chk = results[0]
    if "fits" in last:
        return "find_trucks", ({} if last["fits"] else {"truck_type": last["suggest"][0]})
    if "candidates" in last:
        return "message_owners", {"owner_ids": [c["owner_id"] for c in last["candidates"][:5]]}
    if "replies" in last:
        deals = [r["owner_id"] for r in last["replies"] if r["status"] == "deal"]
        if not deals:
            return "find_trucks", {"truck_type": chk["suggest"][0] if not chk["fits"] else None, "max_deadhead_km": 120}
        return "price_quote", {"owner_id": deals[0]}
    if "margin_pct" in last:
        return "propose_quote", {"owner_id": last["owner_id"], "reason": "Best margin above floor."}
    return "escalate", {"reason": "stuck"}

def run_tests():
    ok = True
    real_chat, real_key = E.chat, E.LLM["key"]
    try:
        # 1. matching never overloads a truck or sends one from far away
        ld = E.normalise_load(HERO)
        c = E.find_candidates(POOL, ld, "Taurus (10-tyre)")
        ok &= check("candidates carry the weight", all(p["capacityTon"] >= 18 for p in c))
        ok &= check("candidates near the pickup", all(p["deadheadKm"] <= E.SRC["maxDeadheadKm"] for p in c),
                    str([p["deadheadKm"] for p in c]))
        ok &= check("normaliser maps '32 MXL'", E.norm_type("32 MXL") == "32 ft MXL")
        ok &= check("normaliser asks on ambiguous '32 ft'", E.norm_type("32 ft") is None)
        ok &= check("normaliser maps 'Bhiwandi (Mumbai)'", E.norm_city("Bhiwandi (Mumbai)") == "Bhiwandi")
        ok &= check("kg read as tons", E.norm_tons("18000 kg") == 18 and E.norm_tons(18000) == 18 and E.norm_tons("14 ton") == 14
                    and E.norm_tons("150 quintal") == 15)
        ok &= check("lakh and k budgets", E.norm_money("25k") == 25000 and E.norm_money("1.2 lakh") == 120000
                    and E.norm_money("35 hazaar") == 35000 and E.norm_money("1,20,000") == 120000 and E.norm_money(40) == 40
                    and E.norm_money("40/km") == 40)

        # 2. deterministic planner on the hero load
        r = E.run_quote(HERO, POOL, use_llm=False)
        ok &= check("hero load quoted", r["status"] == "quote", r.get("reason", ""))
        ok &= check("hero load substitutes Taurus", r["search_type"] == "Taurus (10-tyre)")
        ok &= check("quote clears floor", r["quote"]["above_floor"], f'{r["quote"]["margin_pct"]}%')

        # 3. re-plan keeps the shipper price fixed and re-checks the floor
        mem = E.penalise({}, r["owner"]["ownerId"])
        rp = E.run_replan(r, E.build_pool(mem), use_llm=False)
        ok &= check("re-plan recovers", rp["status"] == "recovered", rp.get("reason", ""))
        ok &= check("shipper price unchanged", rp["quote"]["sell_total"] == r["quote"]["sell_total"])
        ok &= check("dropped owner not re-used", rp["owner"]["ownerId"] != r["owner"]["ownerId"])
        ok &= check("reliability penalised", mem["reliability"][r["owner"]["ownerId"]] < 0)

        # 4. LLM orchestrator loop with a scripted model
        E.LLM["key"] = "test"
        E.chat = fake_llm(sensible_policy)
        r2 = E.run_quote(HERO, POOL, use_llm=True)
        ok &= check("LLM planner used", r2["planner"] == "llm", r2["planner"])
        ok &= check("LLM planner quotes", r2["status"] == "quote", r2.get("reason", ""))
        ok &= check("trace recorded", [s["tool"] for s in r2["trace"]][:2] == ["check_load", "find_trucks"])

        # 5. guardrail: the model cannot propose an owner who never agreed
        def rogue(results):
            return ("propose_quote", {"owner_id": "O999", "reason": "x"}) if len(results) < 2 else ("escalate", {"reason": "gave up"})
        E.chat = fake_llm(rogue)
        r3 = E.run_quote(HERO, POOL, use_llm=True)
        ok &= check("rogue proposal rejected", r3["status"] == "escalate" and "⚠" in r3["trace"][0]["summary"])

        # 6. LLM outage falls back to the deterministic planner
        def boom(*a, **k):
            raise RuntimeError("timeout")
        E.chat = boom
        r4 = E.run_quote(HERO, POOL, use_llm=True)
        ok &= check("fallback on LLM failure", r4["planner"] == "fallback" and r4["status"] == "quote")

        # 7. edge cases from the sample inquiries
        E.LLM["key"] = ""
        r7 = E.run_quote(dict(origin="Kolkata", destination="Patna", weight_ton=25, truck_type="Taurus", pickup_when="kal"), POOL)
        ok &= check("25 T escalates", r7["status"] == "escalate", r7.get("reason", ""))
        r8 = E.run_quote(dict(origin="Delhi", destination="Pune", weight_ton=9, truck_type="22 ft"), POOL)
        ok &= check("unknown lane flagged", r8["status"] == "unknown_lane")
        q = E.price_quote(E.find_lane("Chennai", "Hyderabad"), "22 ft", 17318, budget=18000)
        ok &= check("budget below floor -> floor price", q["above_floor"] and q["sell_total"] < 19694, str(q["sell_total"]))

        sim = E.simulate_day(40)
        ok &= check("day simulator margins inside guardrails",
                    E.MARGIN["floorPct"] <= sim["margin_min"] and sim["margin_max"] <= E.MARGIN["ceilingPct"], str(sim))
    finally:
        E.chat, E.LLM["key"] = real_chat, real_key
    ok &= rate_limit_tests()
    ok &= geo_tests()
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return ok

def rate_limit_tests():
    """A 429 makes every caller wait or fail fast together; a bad key is not retried."""
    class R:
        def __init__(self, code, text="", headers=None):
            self.status_code, self.text, self.headers, self.ok = code, text, headers or {}, code < 400
        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}
    calls, real_post, real_key = [], E.requests.post, E.LLM["key"]
    def post(*a, **k):
        calls.append(1)
        return replies.pop(0)
    E.requests.post, E.LLM["key"] = post, "test"
    ok = True
    try:
        replies = [R(429, headers={"retry-after": "40"})]
        try:
            E.chat([{"role": "user", "content": "x"}], max_wait=8)
        except RuntimeError:
            pass
        n = len(calls)
        try:
            E.chat([{"role": "user", "content": "x"}], max_wait=8)
            failed_fast = False
        except RuntimeError:
            failed_fast = True
        ok &= check("rate limit: later calls fail fast without a request", failed_fast and len(calls) == n == 1)
        E.LLM["coolUntil"] = 0
        replies = [R(401, "bad key")] * 3
        try:
            E.chat([{"role": "user", "content": "x"}])
        except Exception:
            pass
        ok &= check("bad key is not retried", len(calls) == 2)
    finally:
        E.requests.post, E.LLM["key"], E.LLM["coolUntil"] = real_post, real_key, 0
    return ok

def geo_tests():
    """A place the geocoders don't know is asked once, not on every message; a service outage is not remembered."""
    import geo
    calls, real_get, real_save = [], geo.requests.get, geo._save
    class R:
        ok = True
        def json(self):
            return [] if "nominatim" in calls[-1] else {"features": []}
    def get(url, **k):
        calls.append(url)
        if down[0]:
            raise ConnectionError("down")
        return R()
    geo.requests.get, geo._save, down = get, (lambda: None), [True]
    try:
        geo.resolve_city("Qwxzplace")
        geo.resolve_city("Qwxzplace")
        ok = check("outage not remembered as 'no such place'", len(calls) == 4)
        down[0] = False
        geo.resolve_city("Qwxzplace")
        n = len(calls)
        geo.resolve_city("Qwxzplace")
        ok &= check("unknown place looked up once", len(calls) == n == 6)
    finally:
        geo.requests.get, geo._save = real_get, real_save
    return ok

if __name__ == "__main__":
    raise SystemExit(0 if run_tests() else 1)
