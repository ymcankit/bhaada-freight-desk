"""The production path: model JSON in, code guardrails out. Every model reply here is scripted (no network, no keys),
so the code that reads real model output is exercised: intake, the broker's router, owner chat, the negotiation reader
and the strategist, with honest replies and with malicious ones (a rate above the ceiling, a rate the code didn't set,
a move that wasn't offered, prose instead of JSON). Also the provider chain and the usage counters behind /healthz.
Run:  .venv/bin/python test_llm_paths.py
"""
import os, json, re, tempfile, types
from pathlib import Path
os.environ["GROQ_API_KEY"] = os.environ["OPENAI_API_KEY"] = ""
import engine as E
import live
live.STATE_FILE = Path(tempfile.gettempdir()) / "bhaada_llm_paths_test.json"
live.STATE_FILE.unlink(missing_ok=True)
live._store = lambda: None
import server
from fastapi.testclient import TestClient

E.LLM.update(key="test-groq", openai_key="test-openai", model="openai/gpt-oss-120b", fallback="openai/gpt-oss-20b",
             openai_model="gpt-5.4-mini", coolUntil=0, cools={})
D = server.desk
D.send_wa = None                                   # nothing leaves the process

# ---------------- a scripted model behind engine.requests.post ----------------
class Resp:
    def __init__(self, code, content=None, text=None, usage=None):
        self.status_code, self.ok, self.headers = code, code < 400, {}
        self.content = content
        self.usage = usage or {"prompt_tokens": 1000, "completion_tokens": 200}
        self.text = text if text is not None else json.dumps({"ok": code < 400})
    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": self.content}}], "usage": self.usage}

CALLS, SCRIPT = [], []      # SCRIPT: replies in order; each is a Resp, or a dict/str taken as a 200 from whoever is asked

def fake_post(url, headers=None, json=None, timeout=None):
    who = "openai" if "openai.com" in url else json["model"]
    CALLS.append({"who": who, "prompt": json["messages"][-1]["content"]})
    if not SCRIPT:
        return Resp(401, text="unexpected model call in the test")       # 401: not retried, so it fails at once
    r = SCRIPT.pop(0)
    if callable(r):
        r = r(who)
    if isinstance(r, Resp):
        return r
    return Resp(200, r if isinstance(r, str) else globals()["json"].dumps(r, ensure_ascii=False))

E.requests = types.SimpleNamespace(post=fake_post)   # only the engine's calls; geo and the rest keep the real module

def script(*replies):
    del CALLS[:]
    SCRIPT[:] = list(replies)
    E.LLM["coolUntil"], E.LLM["cools"] = 0, {}

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

ok = True
E.reset_stats()

# ---------------- intake ----------------
def intake_json(reply, complete, **load):
    base = dict(origin=None, destination=None, weight_ton=None, material=None, truck_type=None, pickup_when=None,
                budget_rs=None, volume_cft=None, hazardous=None, invoice_value_rs=None)
    return {"reply": reply, "load": {**base, **load}, "missing": [], "complete": complete}

msg = "Kal subah Bhiwandi se Surat 14 ton kapda, budget 30000"
script(intake_json("Theek hai, truck dhoondh raha hoon", True, origin="Bhiwandi", destination="Surat", weight_ton=14,
                   material="kapda", pickup_when="kal subah", budget_rs=30000))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: a complete load is read and read back from validated fields",
            not miss and m["origin"] == "Bhiwandi" and m["destination"] == "Surat" and m["weight_ton"] == 14
            and m["truck_type"] in E.KNOWN_TYPES and "Owners ko message kar raha hoon" in reply, f"{miss} {reply}")
ok &= check("intake goes to OpenAI first (tier smart)", CALLS and CALLS[0]["who"] == "openai", str([c["who"] for c in CALLS]))

msg = "Bhiwandi se Surat kapda bhejna hai"
script(intake_json("Theek hai, truck dhoondh raha hoon", True, origin="Bhiwandi", destination="Surat", material="kapda"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: a model that says 'complete' with the weight missing still gets the weight asked",
            miss == ["weight_ton"] and E.ASK["weight_ton"] in reply and "dhoondh" not in reply, f"{miss} {reply}")

msg = "Bhiwandi se Surat 14 ton chemical drums"
script(intake_json("Done, finding a truck", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="chemical drums"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: a chemical of unknown danger is asked about, not booked", "hazard" in miss and "khatarnaak" in reply.lower(),
            f"{miss} {reply}")
msg2 = "haan khatarnaak hai, acid"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="chemical drums", hazardous=True))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}, {"role": "bot", "text": reply}, {"role": "user", "text": msg2}], m, msg2)
ok &= check("intake: a hazardous chemical gets the Hazchem read-back", not miss and m["material_class"] == "hazardous"
            and "Hazchem" in reply, f"{miss} {m.get('material_class')} {reply}")

msg = "भिवंडी से सूरत, कल सुबह, 14 टन कपड़ा"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="कपड़ा", pickup_when="tomorrow"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: Devanagari message, the broker's own pickup words beat the model's", not miss
            and E.pickup_day(m["pickup_when"]) == E.pickup_day("kal subah") and m["origin"] == "Bhiwandi", f"{m.get('pickup_when')} {miss}")

msg = "Mumbai se Delhi 10 ton steel"
script(intake_json("ok", True, origin="Mumbai", destination="Delhi", weight_ton=10, material="steel", truck_type="Rocket truck 9000"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: a truck type the model made up is replaced by the desk's advice", m["truck_type"] in E.KNOWN_TYPES, m.get("truck_type"))

msg = "Bhiwandi se Surat 14 ton kapda"
script("Sure! Here is your load: Bhiwandi to Surat.")
m, r, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: prose instead of JSON falls back to the rules reading of the broker's own words",
            m.get("origin") == "Bhiwandi" and m.get("destination") == "Surat" and m.get("weight_ton") == 14
            and m.get("material_class") == "textile", str({k: m.get(k) for k in ("origin", "destination", "weight_ton")}))

# ---------------- broker router (server.broker_brain) ----------------
s = {"chat": [], "load": {}}
script({"intent": "reply", "reply": "Shukriya ji 🙏 Aur koi load ho to bhejiye."})
ok &= check("broker brain: thanks gets a warm reply", server.broker_brain("919811110001", "thank you bhai", s)[0] == "reply")
script({"intent": "reply", "reply": "Aapka BH-1 abhi owners se baat mein hai."})
ok &= check("broker brain: a status question is answered", server.broker_brain("919811110001", "mera load kahan tak pahuncha?", s)
            == ("reply", "Aapka BH-1 abhi owners se baat mein hai."))
script({"intent": "reply", "reply": "14 T kapde ke liye 32 ft MXL sahi rahegi. Ye gaadi dhoondh dun?"})
r = server.broker_brain("919811110001", "14 ton kapde ke liye kaunsi gaadi?", s)
ok &= check("broker brain: a truck-choice question gets the desk's computed pick in the prompt",
            r[0] == "reply" and "computed pick" in CALLS[-1]["prompt"], str(r))
script({"intent": "load", "reply": ""})
ok &= check("broker brain: load details go to intake", server.broker_brain("919811110001", "Pune se Nagpur 9 ton", s) == ("load", None))
script({"intent": "reply", "reply": ""})
ok &= check("broker brain: a 'reply' with nothing to say goes to intake instead", server.broker_brain("919811110001", "hmm", s) == ("load", None))
script("Haan ji, main aapki madad kar sakta hoon!")
ok &= check("broker brain: prose instead of JSON falls back to intake", server.broker_brain("919811110001", "kya haal", s) == ("load", None))

# ---------------- owner chat (Desk._owner_chat_bg) ----------------
SENT, POSTED = [], []
D._wa = lambda o, text: SENT.append(text)
real_post = D.post
D.post = lambda kind, **data: POSTED.append((kind, data))
o = {"key": "919822220001", "name": "Ramesh", "baseCity": "Surat", "phone": "919822220001"}
script({"intent": "reply", "reply": "Shukriya ji 🙏 Koi load aaya to turant bataunga."})
del SENT[:], POSTED[:]
D._owner_chat_bg(o, "dhanyavaad", [], [])
ok &= check("owner chat: thanks gets the model's warm reply, nothing else", SENT == ["Shukriya ji 🙏 Koi load aaya to turant bataunga."]
            and not POSTED, f"{SENT} {POSTED}")
script({"intent": "ask_load", "reply": "Abhi aapke liye koi load nahi hai, aate hi bataunga."})
del SENT[:], POSTED[:]
D._owner_chat_bg(o, "koi load hai?", [], [])
ok &= check("owner chat: ask_load answers and queues the AI broker", len(SENT) == 1 and POSTED and POSTED[0][0] == "ai_broker", str(POSTED))
script({"intent": "checkin", "reply": ""},
       {"trucks": [{"type": "32 ft MXL", "city": "Surat", "available": "today", "rate_per_km": 85}]})
del SENT[:], POSTED[:]
D._owner_chat_bg(o, "32 ft MXL khali hai Surat mein, 85/km", [], [])
ok &= check("owner chat: a check-in is parsed by the model into trucks for the worker",
            not SENT and POSTED and POSTED[0][0] == "checkin_apply" and POSTED[0][1]["trucks"][0]["type"] == "32 ft MXL", str(POSTED))
ok &= check("owner chat: the truck extraction runs on the cheap tier (Groq first)", CALLS[-1]["who"] == "openai/gpt-oss-120b",
            CALLS[-1]["who"])
script("I am not sure what you mean.", Resp(401), Resp(401))
del SENT[:], POSTED[:]
D._owner_chat_bg(o, "32 ft MXL Surat aaj", [], [])
ok &= check("owner chat: prose instead of JSON falls back to the rules check-in reader",
            POSTED and POSTED[0][0] == "checkin_apply" and POSTED[0][1]["trucks"] and POSTED[0][1]["trucks"][0]["type"] == "32 ft MXL",
            str(POSTED))
D.post = real_post

# ---------------- negotiation reader (Desk._negotiate) ----------------
LANE = {"laneId": "Bhiwandi-Surat", "origin": "Bhiwandi", "destination": "Surat", "distanceKm": 280, "returnLoadProb": 0.5, "memory": {}}
TT = "32 ft MXL"

def neg_load(**th_over):
    th = {"name": "Ramesh", "ownerKey": "k1", "type": TT, "ask": None, "counter": None, "rounds": 1, "round": 0, "status": "sent",
          "deadheadKm": 5, "reliability": 4.5, "sandbox": False, "sell": round(E.target_sell(LANE, TT)), "agreedBuy": None,
          "messages": [{"dir": "out", "text": "Bhiwandi → Surat, 14 T, rate?", "t": 0}]}
    th.update(th_over)
    load = {"id": "BH-T1", "createdAt": live.now(), "load": {"weight_ton": 14, "pickup_when": "kal"}, "lane": dict(LANE),
            "threads": {"k1": th}, "fixedSell": None, "status": "sourcing", "rounds": 0, "events": [], "phase": "quote",
            "brokerMessages": []}
    return load, th, D._econ(load, th)

def owner_says(text, model, **th_over):
    load, th, ec = neg_load(**th_over)
    th["messages"].append({"dir": "in", "text": text, "t": 1})
    script(*model) if isinstance(model, list) else script(model)
    return D._negotiate(load, th, text, ec), th, ec

def read(intent, rate=None, total=None, answer=""):
    return {"intent": intent, "owner_rate_per_km": rate, "owner_total_rs": total, "answer": answer}

_, _, EC = neg_load()
low = round(EC["target"] - 1, 1)
d, th, ec = owner_says(f"{low} per km lagega", read("quote", low))
ok &= check("negotiation: an ask under the target is accepted at the owner's own rate", d["action"] == "accept" and d["rate"] == low,
            str(d))
d, th, ec = owner_says("theek hai chalega", read("accept_offer"), counter=EC["target"])
ok &= check("negotiation: accept_offer closes at our counter", d["action"] == "accept" and d["rate"] == EC["target"], str(d))
d, th, ec = owner_says("nahi bhai gaadi free nahi", read("refuse"))
ok &= check("negotiation: refuse walks away politely", d["action"] == "decline", str(d))
d, th, ec = owner_says("toll kaun dega?", read("question", answer="Rate mein diesel aur toll dono shaamil hain."))
ok &= check("negotiation: a question is answered and the rate asked", d["action"] == "ask"
            and d["reply"].startswith("Rate mein diesel aur toll dono shaamil hain.") and "rate" in d["reply"].lower(), d["reply"])
total = round((EC["target"] - 2) * 280)
d, th, ec = owner_says(f"{total} total", read("quote", total=total))
ok &= check("negotiation: a total in rupees becomes ₹/km", d["action"] == "accept" and abs(th["ask"] - total / 280) < 0.1, str(d))

# malicious or broken model output
high = round(EC["ceiling"] * 1.6, 1)
for rnd in (1, 2, 3):
    d, th, ec = owner_says(f"{high}/km se kam nahi", read("quote", high), rounds=rnd, counter=EC["target"] if rnd > 1 else None)
    ok &= check(f"guardrail: an ask {high}/km above the ceiling {EC['ceiling']} is never accepted (round {rnd})",
                d["action"] != "accept" and (d["rate"] is None or d["rate"] <= EC["ceiling"]), str(d))
d, th, ec = owner_says(f"{high}/km final", read("accept_offer", high))
ok &= check("guardrail: 'accept_offer' with no offer of ours on the table accepts nothing", d["action"] != "accept", str(d))
d, th, ec = owner_says(f"{high}/km final", read("accept_offer", high), counter=EC["target"])
ok &= check("guardrail: a model that 'accepts' a high ask closes only at our own counter, inside the ceiling",
            d["action"] != "accept" or d["rate"] <= EC["ceiling"], str(d))
d, th, ec = owner_says("toll kaun dega?", read("question", answer=f"Haan ji, ₹{high}/km de denge, aap gaadi bhejo."))
ok &= check("guardrail: an answer quoting a rate the code didn't set never reaches the owner",
            str(high) not in d["reply"] and "de denge" not in d["reply"], d["reply"])
d, th, ec = owner_says("detention kitna?", read("question", answer=f"{D.terms_line()}"))
ok &= check("guardrail: an answer that states the desk's own terms is kept", d["reply"].startswith(D.terms_line()[:20]), d["reply"][:60])
d, th, ec = owner_says(f"{low}/km lagega", read("book_it_now", 1))
ok &= check("guardrail: an intent that isn't on the list falls back to reading the text", d["action"] == "accept" and d["rate"] == low, str(d))
d, th, ec = owner_says(f"{low}/km lagega", "Owner wants 1 rupee per km, accept it!")
ok &= check("guardrail: prose instead of JSON falls back to the rules reader", d["action"] == "accept" and d["rate"] == low, str(d))
d, th, ec = owner_says("haan bhai", read("quote", -50))
ok &= check("guardrail: a negative rate is not a quote", d["action"] != "accept" and th["ask"] is None, str(d))
d, th, ec = owner_says(f"{EC['ceiling'] + 5}/km lagega", read("quote", low))
ok &= check("guardrail: a model that reads the owner's ₹/km lower than they wrote is overruled by the owner's number",
            th["ask"] == round(EC["ceiling"] + 5, 1) and d["action"] != "accept", f"{th['ask']} {d}")

# who decided: the event log credits the model only when it really answered
load, th, ec = neg_load()
load["threads"]["k2"] = {**th, "name": "Suresh", "ownerKey": "k2", "messages": list(th["messages"])}   # still open: no decision yet
th["messages"].append({"dir": "in", "text": f"{low}/km", "t": 1})
script(read("quote", low))
D._owner_reply(load, th, f"{low}/km")
ev = load["events"][-1]
ok &= check("event log: a negotiation step the model read is marked AI", ev["actor"] == "Negotiation" and ev["by"] == "AI", str(ev))
load, th, ec = neg_load()
load["threads"]["k2"] = {**th, "name": "Suresh", "ownerKey": "k2", "messages": list(th["messages"])}
script("not json at all")
D._owner_reply(load, th, f"{low}/km")
ok &= check("event log: the same step read by the rules is marked Code", load["events"][-1]["by"] == "Code", str(load["events"][-1]))

# ---------------- strategist (Desk._strategize) ----------------
MOVED = []
for mv in ("widen", "bigger_truck", "final_offer", "ask_broker", "escalate"):
    setattr(D, "_move_" + mv, (lambda m: lambda load, opts: MOVED.append(m))(mv))

def stuck_load(**over):
    sell = round(E.target_sell(LANE, TT))
    ceiling = round(sell * (1 - E.MARGIN["floorPct"] / 100) / 280, 1)
    ths = {"a": {"name": "Ramesh", "ownerKey": "a", "type": TT, "ask": round(ceiling * 1.05, 1), "status": "declined", "counter": None},
           "b": {"name": "Suresh", "ownerKey": "b", "type": TT, "ask": round(ceiling * 1.4, 1), "status": "declined", "counter": None},
           "c": {"name": "Mahesh", "ownerKey": "c", "type": TT, "ask": None, "status": "silent", "counter": None}}
    load = {"id": "BH-T2", "createdAt": live.now(), "load": {"weight_ton": 14, "pickup_when": "kal"}, "lane": dict(LANE),
            "threads": ths, "fixedSell": None, "status": "sourcing", "rounds": 1, "events": [], "phase": "quote",
            "searchType": TT, "widenLeft": 1, "brokerMessages": []}
    load.update(over)
    return load

def strategize(model, final=False, **over):
    load = stuck_load(**over)
    del MOVED[:]
    script(model)
    D._strategize(load, final)
    return load, load["events"][-1]

for mv in ("widen", "bigger_truck", "final_offer", "ask_broker"):
    load, ev = strategize({"move": mv, "why": f"{mv} is the best next step"})
    ok &= check(f"strategist: the model's '{mv}' is carried out and marked AI", MOVED == [mv] and ev["by"] == "AI"
                and ev["chosen"] == mv and mv in ev["moves"] and bool(ev["why"]), f"{MOVED} {ev.get('by')}")
load, ev = strategize({"move": "escalate", "why": "give up"}, strategySteps=1)
ok &= check("strategist: escalating at a later dead end is the model's call", MOVED == ["escalate"] and ev["by"] == "AI", str(MOVED))
load, ev = strategize({"move": "escalate", "why": "give up"})
ok &= check("guardrail: escalating at the first dead end, with moves left, is overruled by the fixed order",
            MOVED == ["widen"] and ev["by"] == "Code" and ev["text"].startswith("(rules)"), f"{MOVED} {ev['text'][:40]}")
load, ev = strategize({"move": "lower_the_floor", "why": "margin is overrated"})
ok &= check("guardrail: a move that doesn't exist falls back to the fixed order", MOVED == ["widen"] and ev["by"] == "Code", str(MOVED))
load, ev = strategize({"move": "widen", "why": "more owners"}, final=True, widenLeft=0)
ok &= check("guardrail: a move that wasn't offered (widen, none left) is refused", MOVED and MOVED[0] != "widen"
            and "widen" not in ev["moves"] and ev["by"] == "Code", f"{MOVED} {list(ev['moves'])}")
load, ev = strategize("I think you should widen the search radius.")
ok &= check("guardrail: prose instead of JSON falls back to the fixed order", MOVED == ["widen"] and ev["by"] == "Code", str(MOVED))
ok &= check("strategist event lists every move it weighed", set(ev["moves"]) >= {"widen", "final_offer", "ask_broker", "escalate"},
            str(list(ev["moves"])))

# ---------------- provider chain and counters ----------------
E.reset_stats()
quota = Resp(429, text='{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}')
script(lambda who: quota, {"intent": "reply", "reply": "ok"})
out = E.chat([{"role": "user", "content": "x"}], tier="smart", max_wait=8)
st = E.llm_stats()
ok &= check("chain: OpenAI out of credit (429 insufficient_quota) hands the call to Groq 120b",
            [c["who"] for c in CALLS] == ["openai", "openai/gpt-oss-120b"] and "reply" in out["content"], str([c["who"] for c in CALLS]))
ok &= check("chain: OpenAI cools down for an hour after insufficient_quota", E._cool("openai") - live.now() > 3000)
ok &= check("stats: the failure, the fallback and Groq's tokens are counted",
            st["providers"]["openai"]["failures"] == 1 and st["providers"]["openai"]["fallbacks"] == 1
            and st["providers"]["groq"]["ok"] == 1 and st["providers"]["groq"]["promptTokens"] == 1000, json.dumps(st["providers"]))
SCRIPT[:] = [{"intent": "reply", "reply": "ok"}]                 # still cooling: OpenAI isn't even asked
E.chat([{"role": "user", "content": "x"}], tier="smart", max_wait=8)
ok &= check("chain: while OpenAI cools, Groq answers without a request to OpenAI", CALLS[-1]["who"] == "openai/gpt-oss-120b"
            and sum(c["who"] == "openai" for c in CALLS) == 1)

E.reset_stats()
script(quota, Resp(429, text="Rate limit reached. Please try again in 2m0s."), Resp(401, text="bad key"))
try:
    E.chat([{"role": "user", "content": "x"}], tier="smart", max_wait=8)
    failed = False
except Exception:
    failed = True
st = E.llm_stats()
ok &= check("chain: OpenAI → Groq 120b → Groq 20b all failing raises", failed and [c["who"] for c in CALLS]
            == ["openai", "openai/gpt-oss-120b", "openai/gpt-oss-20b"], str([c["who"] for c in CALLS]))
ok &= check("stats: all providers down counts one rules fallback", st["rulesFallbacks"] == 1 and st["calls"] == 1, json.dumps(st))
script()
E.LLM["cools"] = {"openai": live.now() + 3600, "groq-fallback": live.now() + 3600}
E.LLM["coolUntil"] = live.now() + 3600
r = server.broker_brain("919811110001", "kya haal", {"chat": [], "load": {}})
ok &= check("chain: with every provider cooling, the broker router falls back to intake without a request", r == ("load", None)
            and not CALLS, str([c["who"] for c in CALLS]))
load, th, ec = neg_load()
th["messages"].append({"dir": "in", "text": f"{low}/km lagega", "t": 1})
d = D._negotiate(load, th, f"{low}/km lagega", ec)
ok &= check("chain: with every provider down, negotiation runs on the rules", d["action"] == "accept" and d["rate"] == low and not CALLS,
            str(d))

E.reset_stats()
script({"intent": "reply", "reply": "SECRET-OWNER-TEXT"})
E.chat([{"role": "user", "content": "SECRET-PROMPT-TEXT"}], tier="smart")
st = E.llm_stats()
want = round((1000 * 0.75 + 200 * 4.50) / 1e6, 6)
ok &= check("stats: OpenAI tokens priced at $0.75/$4.50 per million", abs(st["providers"]["openai"]["usd"] - want) < 1e-9
            and st["tokens"] == 1200, json.dumps(st["providers"]["openai"]))
script({"x": 1})
E.chat([{"role": "user", "content": "x"}], tier="cheap")
st = E.llm_stats()
ok &= check("stats: Groq 120b tokens priced at $0.15/$0.60 per million",
            abs(st["providers"]["groq"]["usd"] - (1000 * 0.15 + 200 * 0.60) / 1e6) < 1e-9, json.dumps(st["providers"]["groq"]))
h = TestClient(server.app).get("/healthz")
body = h.text
ok &= check("/healthz shows calls, tokens and cost", "llmStats" in h.json() and h.json()["llmStats"]["calls"] == 2
            and h.json()["llmStats"]["tokens"] == 2400 and h.json()["llmStats"]["usd"] > 0, body[:300])
ok &= check("/healthz carries no message content and no keys", not any(x in body for x in
            ("SECRET-PROMPT-TEXT", "SECRET-OWNER-TEXT", "test-openai", "test-groq")))

print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
