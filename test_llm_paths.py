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
script(intake_json("Theek hai, truck dhoondh raha hoon", True, origin="Bhiwandi", destination="Surat", material="kapda", budget_rs=30000, pickup_when="kal subah"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: a model that says 'complete' with the weight missing still gets the weight asked",
            miss == ["weight_ton"] and E.ASK["weight_ton"] in reply and "dhoondh" not in reply, f"{miss} {reply}")

msg = "Bhiwandi se Surat 14 ton chemical drums"
script(intake_json("Done, finding a truck", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="chemical drums", budget_rs=30000, pickup_when="kal subah"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: a chemical of unknown danger is asked about, not booked", "hazard" in miss and "khatarnaak" in reply.lower(),
            f"{miss} {reply}")
msg2 = "haan khatarnaak hai, acid"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="chemical drums", hazardous=True, budget_rs=30000, pickup_when="kal subah"))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}, {"role": "bot", "text": reply}, {"role": "user", "text": msg2}], m, msg2)
ok &= check("intake: a hazardous chemical gets the Hazchem read-back", not miss and m["material_class"] == "hazardous"
            and "Hazchem" in reply, f"{miss} {m.get('material_class')} {reply}")

msg = "भिवंडी से सूरत, कल सुबह, 14 टन कपड़ा"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="कपड़ा", pickup_when="tomorrow", budget_rs=30000))
m, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("intake: Devanagari message, the broker's own pickup words beat the model's", not miss
            and E.pickup_day(m["pickup_when"]) == E.pickup_day("kal subah") and m["origin"] == "Bhiwandi", f"{m.get('pickup_when')} {miss}")

msg = "Mumbai se Delhi 10 ton steel"
script(intake_json("ok", True, origin="Mumbai", destination="Delhi", weight_ton=10, material="steel", truck_type="Rocket truck 9000", budget_rs=30000, pickup_when="kal subah"))
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
LANE = {"laneId": "Bhiwandi-Surat", "origin": "Bhiwandi", "destination": "Surat", "distanceKm": 280, "returnLoadProb": 0.75, "memory": {}}
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

# required questions: loading time, then the shipper's price and the broker's margin (asked once)
msg = "Bhiwandi se Surat 14 ton kapda"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="kapda"))
m1, reply, miss = E.intake_turn([{"role": "user", "text": msg}], {}, msg)
ok &= check("required: loading time and pricing are asked before owners are messaged", miss == ["pickup_when", "pricing"]
            and "Loading kab" in reply and "margin" in reply and m1.get("pricingAsked"), f"{miss} {reply[:120]}")
msg2 = "kal subah 9 baje, shipper 25 hazaar de raha hai, 8% margin chahiye"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="kapda", pickup_when="kal subah 9 baje"))
m2, reply2, miss2 = E.intake_turn([{"role": "user", "text": msg}, {"role": "user", "text": msg2}], m1, msg2)
ok &= check("the broker's shipper price and margin are read from their words", not miss2 and m2.get("budget_rs") == 25000
            and m2.get("margin_pct") == 8 and "aapka margin 8%" in reply2, f"{miss2} {m2.get('budget_rs')} {m2.get('margin_pct')} {reply2[:140]}")
msg3 = "kal subah, aap batao"
script(intake_json("Theek hai", True, origin="Bhiwandi", destination="Surat", weight_ton=14, material="kapda", pickup_when="kal subah"))
m3, reply3, miss3 = E.intake_turn([{"role": "user", "text": msg}, {"role": "user", "text": msg3}], m1, msg3)
ok &= check("'aap batao' lets the desk price from the market", not miss3 and m3.get("pricingSkip") and "rate market se" in reply3, f"{miss3} {reply3[:120]}")
ok &= check("an advance % is never read as the broker's margin", E.broker_terms("80% advance chahiye, 30000 milega") == {"budget_rs": 30000})

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
D._owner_reply(load, th, f"{low}/km", read=D._model_read(th, f"{low}/km", load["lane"], load["load"]), pre=True)   # what the worker does when the background read lands
ev = load["events"][-1]
ok &= check("event log: a negotiation step the model read is marked AI", ev["actor"] == "Negotiation" and ev["by"] == "AI", str(ev))
load, th, ec = neg_load()
load["threads"]["k2"] = {**th, "name": "Suresh", "ownerKey": "k2", "messages": list(th["messages"])}
script("not json at all")
D._owner_reply(load, th, f"{low}/km", read=D._model_read(th, f"{low}/km", load["lane"], load["load"]), pre=True)   # what the worker does when the background read lands
ok &= check("event log: the same step read by the rules is marked Code", load["events"][-1]["by"] == "Code", str(load["events"][-1]))

# ---------------- the review's rules (29 Oct): Indian-market negotiation ----------------
below = round(EC["cost"] - 1, 1)
d, th, ec = owner_says(f"{below} per km", read("quote", below))
ok &= check("an ask below the truck's running cost is confirmed, not taken", d["action"] == "counter" and "Confirm" in d["reply"], str(d)[:120])
c0 = round(EC["target"] + 1, 1)
d, th, ec = owner_says("haan 95 mein", read("accept_offer"), counter=c0)
ok &= check("'haan, 95 mein' is a new rate, not a yes", d["action"] != "accept" or abs(d["rate"] - c0) > 0.5, str(d)[:120])
d, th, ec = owner_says("ok dekhte hain", read("accept_offer"), counter=c0)
ok &= check("'ok dekhte hain' is not a booking", d["action"] != "accept", str(d)[:120])
d, th, ec = owner_says("90% advance chahiye", read("other"))
ok &= check("a bigger advance is traded for a lower total", th.get("advancePct") == 90 and th.get("tradeRs") == 500
            and "90% advance de denge" in d["reply"] and ec["target"] > D._econ(neg_load()[0], th)["target"] - 0.01, d["reply"][:120])
ok &= check("'90 advance' is not read as ₹90/km", D._owner_rate("90 advance chahiye", 280) is None)
hi = round(EC["ceiling"] * 1.3, 1)
d, th, ec = owner_says(f"{hi} per km", read("quote", hi))
tot = round(d["rate"] * 280)
ok &= check("a short-lane counter speaks in totals, rounded to ₹500", d["action"] == "counter" and "/km" not in d["reply"]
            and tot % 500 == 0 and "total" in d["reply"], d["reply"][:140])
ok &= check("round 1 cites the lane, not the owner's own diesel and EMI", "diesel ~" not in d["reply"] and "EMI" not in d["reply"], d["reply"][:140])
edge = round(c0 + 0.4, 1)
d, th, ec = owner_says(f"{edge} per km", read("quote", edge), counter=c0, ask=edge, rounds=2)
ok &= check("edging: last counter + ₹0.4 without coming down is not auto-accepted", d["action"] != "accept", str(d)[:100])
d, th, ec = owner_says(f"{edge} per km", read("quote", edge), counter=c0, ask=edge + 2, rounds=2)
ok &= check("after they come down, a few paise apart is closed", d["action"] == "accept", str(d)[:100])
finals, declines = [], 0
for i in range(12):
    d, th, ec = owner_says(f"{hi} per km", read("quote", hi), ownerKey=f"k{i}", rounds=3, counter=EC["target"], ask=hi + 1)
    if d["action"] == "counter":
        finals.append(d["rate"])
    elif d["action"] == "decline":
        declines += 1
ok &= check("the aakhri offer is not always sent, and not always at the cap",
            finals and declines and any(r < EC["ceiling"] - 0.3 for r in finals) and all(r <= EC["ceiling"] + 0.01 for r in finals),
            f"{len(finals)} finals {sorted(finals)[:4]}, {declines} declines, ceiling {EC['ceiling']}")
terms = D.terms_line()
ok &= check("terms state toll, commission and deductions", "toll shaamil" in terms and "commission nahi" in terms and "POD par likhi" in terms, terms)

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

# the model reads off the worker: a reading that comes back after a newer message, or after the round closed, is dropped
load, th, ec = neg_load()
th["readSeq"], th["status"] = 2, "sent"
before = (th["status"], th.get("ask"))
D._on_owner_read(load["id"] if D._find(load["id"]) else "missing", th["ownerKey"], "50/km", {"intent": "quote", "ask": 50, "answer": ""}, 1)
ok &= check("a stale background reading changes nothing", (th["status"], th.get("ask")) == before)

# ---------------- agents (agents.py): each stage decides, code checks every action ----------------
# booking agent: a question mid-hold gets an answer from the load's facts; a made-up number drops the answer
load, th, ec = neg_load(status="hold_requested", agreedBuy=24000)
script({"reply": "Advance 80% loading pe milega, baaki POD milne ke 7 din mein."})
ans = D._stage_answer(load, th, "payment kab milega?")
ok &= check("booking agent answers a hold-stage question from the facts", "80%" in ans and "7 din" in ans, ans)
script({"reply": "Kal ₹500 extra mil jayega, pakka."})
ok &= check("booking agent: an answer with a number not in the facts is dropped", D._stage_answer(load, th, "kuch extra?") == "")
D.loads.append(load)
before = len(th["messages"])
D._on_owner_answer(load["id"], th["ownerKey"], "Advance 80% loading pe milega.", "Gaadi hold karne ke liye *HAAN* likhiye", "hold_requested")
out = th["messages"][-1]["text"] if len(th["messages"]) > before else ""
ok &= check("the answer goes out with the HAAN reminder after it", "Advance 80%" in out and out.index("Advance") < out.index("*HAAN*"), out[:80])
D._on_owner_answer(load["id"], th["ownerKey"], "late answer", "x", "sent")
ok &= check("an answer that arrives after the deal moved on is not sent", th["messages"][-1]["text"] == out)

# backup owners are told why, and that they're first in line
bk, bth, _ = neg_load(status="deal", agreedBuy=26000, ownerKey="k1")
bk["threads"]["w1"] = {"name": "Gupta", "ownerKey": "w1", "type": TT, "status": "held", "agreedBuy": 24000, "reliability": 4.6,
                        "messages": [], "counter": None, "ask": None, "rounds": 1}
bk["winner"] = "w1"
lines, allowed = D._stage_facts(bk, bth)
facts = " ".join(lines)
ok &= check("a backup owner's facts say why and that they're asked first", "backup" in facts and "lower rate" in facts, facts[-160:])
bk["id"] = "BH-T9"; D.loads.append(bk)
rows, _ = D._owner_story("k1")
ok &= check("the owner chat knows this owner is the backup and why", any("BACKUP" in r and "lower rate" in r for r in rows), str(rows)[:200])

# negotiation agent (agentMode): the model proposes, code holds the line
D.settings["agentMode"] = True
def mv(intent, rate=None, **move):
    return {"intent": intent, "owner_rate_per_km": rate, "owner_total_rs": None, "answer": "", "move": move}
hi = round(EC["ceiling"] * 1.3, 1)
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["ceiling"] * 280 * 2), reason="Is route pe yahi chal raha hai."))
ok &= check("agent counter above the ceiling is held to the ceiling, in a ₹500 lump", d.get("by") == "agent" and d["action"] == "counter"
            and d["rate"] <= EC["ceiling"] + 0.01 and round(d["rate"] * 280) % 500 == 0, str(d)[:140])
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="accept"))
ok &= check("agent can't accept an ask above the ceiling: the rules decide", d.get("by") != "agent" and d["action"] != "accept", str(d)[:120])
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["target"] * 280), reason="Market ₹90/km hai."))
ok &= check("agent's reason with a number is dropped", d.get("by") == "agent" and "90/km" not in d["reply"], d["reply"][:120])
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["target"] * 280),
                      reason="Payment jaldi milegi aur regular kaam bhi milta rahega."))
ok &= check("agent's promise of fast payment or more work is dropped", d.get("by") == "agent"
            and "ayment" not in d["reply"] and "regular" not in d["reply"], d["reply"][:120])
from agents import _reason
ok &= check("lane and cost reasons pass the promise filter",
            bool(_reason({"reason": "Is lane pe market rate yahi chal raha hai, diesel bhi dekh liya hai."}))
            and not _reason({"reason": "Agla load bhi aapko denge."}), "")
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="decline", reason="Nahi ho payega."))
ok &= check("agent can't walk away in round 1: the rules decide", d.get("by") != "agent", str(d)[:120])
c0 = round(EC["target"], 1)
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["ceiling"] * 280)), counter=c0, ask=hi, rounds=2)
ok &= check("agent can't step up when the owner didn't come down", d.get("by") == "agent" and abs(d["rate"] - c0) < 0.01, str(d)[:140])
mid = round((EC["target"] + EC["ceiling"]) / 2, 1)
d, th, ec = owner_says(f"{mid}/km", mv("quote", mid, action="accept"))
ok &= check("agent can't pay above target on the first reply (the rules decide)", d.get("by") != "agent", str(d)[:120])
d, th, ec = owner_says(f"{mid}/km", mv("quote", mid, action="accept"), counter=EC["target"], ask=mid + 1, rounds=2)
ok &= check("from round 2 the agent may accept within the broker's limit", d["action"] == "accept" and abs(d["rate"] - mid) < 0.01, str(d)[:120])
low = round(EC["target"] - 1, 1)
d, th, ec = owner_says(f"{low}/km", mv("quote", low, action="accept"))
ok &= check("agent accepts a fair ask", d["action"] == "accept" and abs(d["rate"] - low) < 0.01, str(d)[:120])
D.settings["agentMode"] = False
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["ceiling"] * 280)))
ok &= check("agentMode off: a model's move is ignored", d.get("by") != "agent", str(d)[:120])

# sourcing agent: picks from the ranked list; bad picks are skipped, nonsense falls back to the ranked order
cands = [{"ownerKey": f"s{i}", "type": TT, "city": "Bhiwandi", "deadheadKm": 5 + i, "ratePerKm": None} for i in range(8)]
sl, _, _ = neg_load()
sl["searchType"] = TT
script({"pick": [3, 1, 99, "x", 3], "why": "Fast repliers first."})
got, why = D._agent_pick(sl, cands, 5)
ok &= check("sourcing agent's picks are kept only if they're real candidates", [c["ownerKey"] for c in got] == ["s3", "s1"] and bool(why), str([c["ownerKey"] for c in got]))
script("not json at all")
got, why = D._agent_pick(sl, cands, 5)
ok &= check("sourcing agent: unreadable output falls back to the ranked order", [c["ownerKey"] for c in got] == [f"s{i}" for i in range(5)])

# lane learning: each quoted load leaves a record; the playbook appears once a lane has history
D.lane_book = {}
def learned_load(i, first, close=None, counter=None, out="deal", demo=False):
    load, th, ec = neg_load(firstAsk=round(EC["target"] * first, 1), ask=round(EC["target"] * first, 1), rounds=2,
                            firstCounter=round(EC["target"] * first * counter, 1) if counter else None,
                            agreedBuy=round(EC["target"] * first * close * 280) if close else None,
                            status="deal" if close else out)
    load["id"], load["quoteAt"] = f"BH-L{i}", live.now()
    if demo:
        load["demoOnly"] = True
    return load
L1 = learned_load(1, 1.15, 0.92, 0.88)
D._learn(L1, "quoted")
D._learn(L1, "quoted")
ok &= check("lane learning records a load once", len(D.lane_book.get("Bhiwandi-Surat", {}).get("loads", [])) == 1)
ok &= check("one load is not a playbook yet", D._playbook(learned_load(9, 1.1)) == [])
D._learn(learned_load(2, 1.12, 0.94, 0.95), "quoted")
D._learn(learned_load(3, 1.30, None, 0.85, out="declined"), "escalated")
pb = D._playbook(learned_load(9, 1.1), TT)
ok &= check("after three negotiations the lane has a playbook", any("opening ask" in x for x in pb)
            and any("closed a median" in x for x in pb) and any("no deal" in x for x in pb), str(pb)[:200])
ok &= check("the playbook compares first counters that closed with ones that didn't",
            any("first counters within 7%" in x for x in pb), str(pb)[:300])
D._learn(learned_load(4, 1.5, 0.7, 0.7, demo=True), "quoted")
ok &= check("demo loads teach demo loads only", "Bhiwandi-Surat|demo" in D.lane_book
            and len(D.lane_book["Bhiwandi-Surat"]["negs"]) == 3, str(list(D.lane_book))[:120])

D.settings["agentMode"] = True
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["target"] * 280)))
ok &= check("the negotiation agent reads the lane's playbook", "learned on this lane" in CALLS[0]["prompt"], CALLS[0]["prompt"][-300:])

# coordinator: plans the load from tools code offers; code applies the plan
real_cands = D._candidates
D._candidates = lambda load, radius, n=None: [{"ownerKey": f"c{i}", "type": TT, "city": "Bhiwandi", "deadheadKm": 5}
                                              for i in range(3 if radius <= D.settings["maxDeadheadKm"] else 9)]
def plan(model, **over):
    load, _, _ = neg_load()
    load.update(searchType=TT, widenLeft=1, contacted=[], **over)
    load["threads"] = {}
    script(model)
    D._coordinator_plan(load)
    return load
pl = plan({"tool": "source_wide", "stance": "firm", "why": "Few trucks nearby on this lane."})
ev = [e for e in pl["events"] if e["actor"] == "Coordinator"][-1]
ok &= check("coordinator's wide search is applied: wider radius now, the later widen used up",
            pl["plan"] == {"tool": "source_wide", "stance": "firm"} and pl["wideStart"] and pl["widenLeft"] == 0
            and ev["by"] == "AI" and ev["chosen"] == "source_wide", str(pl.get("plan")))
ok &= check("the coordinator is shown the lane's playbook", "opening ask" in CALLS[0]["prompt"], "")
ok &= check("the playbook is logged on the load", any(e["actor"] == "Learning" for e in pl["events"]))
pl = plan({"tool": "book_it_now", "stance": "reckless"})
ok &= check("an unknown tool or stance falls back to source nearby, standard", pl["plan"] == {"tool": "source_nearby", "stance": "standard"})
pl = plan({"tool": "regulars_first", "stance": "standard"})
ok &= check("regulars first is offered only when the lane has regulars", pl["plan"]["tool"] == "source_nearby")
D.lane_rates["Bhiwandi-Surat|" + TT] = [(80.0, live.now(), "c1"), (81.0, live.now(), "c1")]
pl = plan({"tool": "regulars_first", "stance": "quick"})
ok &= check("regulars first puts the lane's regulars at the front", pl["plan"]["tool"] == "regulars_first" and "c1" in pl["prefer"], str(pl.get("prefer")))
del D.lane_rates["Bhiwandi-Surat|" + TT]
D._candidates = real_cands
script({"intent": "quote", "owner_rate_per_km": hi, "move": {"action": "counter", "amount_total": round(EC["target"] * 280)}})
load, th, ec = neg_load()
load["plan"] = {"tool": "source_nearby", "stance": "firm"}
th["messages"].append({"dir": "in", "text": f"{hi}/km", "t": 1})
D._negotiate(load, th, f"{hi}/km", ec)
ok &= check("the negotiation agent is told the coordinator's stance", "bargain hard" in CALLS[0]["prompt"], "")

# coordinator's winner: only among offers within 2 points of the best margin
load, _, _ = neg_load()
load["threads"] = {k: {"name": k.upper(), "ownerKey": k, "deadheadKm": 5, "status": "deal"} for k in ("a", "b", "c")}
offers = [(14.0, 4.0, "a"), (12.5, 4.8, "b"), (10.0, 5.0, "c")]
script({"pick": 1, "why": "B is rated higher and the margin gap is small."})
got, why = D._coordinator_pick(load, offers)
ok &= check("coordinator may pick a surer owner within 2 points of margin", got == offers[1] and bool(why), str(got))
ok &= check("an offer 4 points worse is never shown to the coordinator", "C," not in CALLS[0]["prompt"], "")
script({"pick": 7})
ok &= check("an out-of-range pick falls back to the rules", D._coordinator_pick(load, offers)[0] is None)
D.settings["agentMode"] = False

# owner trust: no "Done" before the broker approves, no margin talk, backups get a kept promise
low = round(EC["target"] - 1, 1)
d, th, ec = owner_says(f"{low}/km", read("quote", low))
ok &= check("an owner's yes is noted, not promised", d["action"] == "accept" and "Done" not in d["reply"] and "pakka nahi" in d["reply"], d["reply"][:90])
D.settings["agentMode"] = True
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["target"] * 280),
                      reason="Thoda aur margin rakhen to gaadi lock kar lete hain."))
ok &= check("the agent never tells an owner about our margin", d.get("by") == "agent" and "margin" not in d["reply"].lower(), d["reply"][:120])
D.settings["agentMode"] = False
load, _, _ = neg_load()
load.update(searchType=TT, winner=None, quote=None, mode="live", id="BH-HEADROOM")
load["load"].update(truck_type=TT, origin="Bhiwandi", destination="Surat")
for k, nm, buy, rel in (("tb1", "Backup One", 21500, 4.0), ("tb2", "Winner Two", 21000, 4.0)):
    D.owners[k] = {"key": k, "name": nm, "sandbox": False, "phone": "9800000" + k[-3:]}
    load["threads"][k] = {"name": nm, "ownerKey": k, "type": TT, "status": "deal", "agreedBuy": buy, "reliability": rel, "sandbox": False,
                          "sell": round(E.target_sell(LANE, TT)), "messages": [], "rounds": 1, "ask": buy / 280, "counter": None}
del load["threads"]["k1"]
D._decide(load)
msg = load["threads"]["tb1"]["messages"][-1]["text"] if load["threads"]["tb1"]["messages"] else ""
ok &= check("the backup hears the truth and a promise", load["winner"] == "tb2" and "backup" in msg and "agla load" in msg, msg[:120])
ok &= check("the desk records the promise and will ask them first", D._first_call("tb1", LANE) and not D._first_call("tb2", LANE))
# budget headroom: quote higher only inside the shipper's own budget
q0 = load["quote"]["sell_total"]
load["load"]["budget_rs"] = q0 + 6000
load["status"] = "needs_approval"
D.loads.append(load)
h = D.headroom(load)
ok &= check("headroom is the budget above our quote", h and h["gap"] == 6000 and q0 < h["ask"] <= q0 + 6000 and h["askMargin"] > load["quote"]["margin_pct"], str(h))
D._on_requote(load["id"], q0 + 9000)
ok &= check("never quote above the shipper's budget", load["quote"]["sell_total"] == q0)
D._on_requote(load["id"], h["ask"])
ok &= check("quote raised inside the budget: same owner, better margin", load["quote"]["sell_total"] == h["ask"] and load["winner"] == "tb2"
            and load["quote"]["buy_total"] == 21000 and D.headroom(load) is None, str(load["quote"].get("sell_total")))
D.loads.remove(load)
for k in ("tb1", "tb2"):
    D.owners.pop(k, None)

# memory: the owner's own past deal on this lane reaches the negotiator, and only that amount may be quoted
past = {"id": "BH-PAST1", "createdAt": live.now() - 3600, "bookedAt": live.now() - 3000, "status": "booked", "winner": "k1",
        "lane": dict(LANE), "load": {}, "events": [], "threads": {"k1": {"ownerKey": "k1", "agreedBuy": 21500, "status": "held"}}}
D.loads.append(past)
load, th, ec = neg_load()
lines, amounts = D._owner_history(load, th)
ok &= check("the desk remembers this owner's last trip on the lane", any("last trip" in x and "₹21,500" in x for x in lines) and "21500" in amounts, str(lines))
D.settings["agentMode"] = True
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["target"] * 280),
                      reason="Pichli baar aapne isi route pe ₹21,500 mein gaadi di thi."))
ok &= check("the negotiator may remind an owner of their own past rate", "₹21,500 mein gaadi di thi" in d["reply"]
            and "history with us" in CALLS[0]["prompt"], d["reply"][:140])
d, th, ec = owner_says(f"{hi}/km", mv("quote", hi, action="counter", amount_total=round(EC["target"] * 280),
                      reason="Pichli baar aapne ₹19,000 mein di thi."))
ok &= check("an amount that isn't their own past deal is dropped", "19,000" not in d["reply"], d["reply"][:120])
D.settings["agentMode"] = False
d, th, ec = owner_says(f"{hi}/km", read("quote", hi))
ok &= check("the rules' first counter also cites their own last rate", "Pichli baar aapne isi route pe ₹21,500" in d["reply"], d["reply"][:200])
D.loads.remove(past)
# chat memory is saved with the desk, newest people kept
import collections as _c
D.convo = {"tg:old": _c.deque(["Owner: hi"], maxlen=12), "tg:new": _c.deque(["Owner: 32 MXL free", "Bhaada: noted"], maxlen=12)}
snap = D._convo_snapshot()
ok &= check("recent chat lines are part of the saved state", snap.get("tg:new") == ["Owner: 32 MXL free", "Bhaada: noted"] and "tg:old" in snap)
D.convo = server.CONVO

print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
