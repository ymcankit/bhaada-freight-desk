"""AI agents and timers on the live desk, over fake Telegram traffic and without the language model:
AI owners bid when no real owner has a truck, the AI broker posts a demo load to a real owner and approves it,
an owner with two loads is asked which one, stale trucks drop out, the hold reminder and quote expiry fire.
Run:  .venv/bin/python test_agents.py
"""
import os, time, tempfile
from pathlib import Path
os.environ["GROQ_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:" + "x" * 35
import engine as E
import live
live.STATE_FILE = Path(tempfile.gettempdir()) / "bhaada_agents_test.json"
live.STATE_FILE.unlink(missing_ok=True)
live._store = lambda: None
import server
from fastapi.testclient import TestClient

E.LLM["key"] = E.LLM["openai_key"] = ""          # no real model calls: rules take over
E.llm_ready = lambda: True
SENT = []
server.tg_send = lambda to, text, buttons=None: SENT.append((to, text, buttons))
server.desk.send_wa = server.send
D = server.desk
D.settings.update(mode="live", aiAgents=True)
D.settings["replyTimeoutSec"]["live"] = 6
D.settings["replyTimeoutLaterSec"] = {"live": 6, "sandbox": 30}
D.settings["closeoutSec"] = {"live": 3, "sandbox": 3}
D.settings["checkinAuto"] = False                 # its own test; must not fire mid-run
D.settings["holdTimeoutSec"]["live"] = 8
D.sandbox_on()
c = TestClient(server.app, headers={"X-Telegram-Bot-Api-Secret-Token": server.TG_SECRET})   # as Telegram sends it
URL = f"/telegram/{server.TG_PATH}"
n = [0]

def text(chat, body):
    n[0] += 1
    c.post(URL, json={"update_id": n[0], "message": {"chat": {"id": chat}, "text": body}})

def last_to(chat):
    return next((t for to, t, b in reversed(SENT) if to == f"tg:{chat}"), "")

def wait(load, states, secs):
    end = time.time() + secs
    while time.time() < end and load["status"] not in states:
        time.sleep(0.3)
    return load["status"]

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

ok = True
# 1. a real broker, no real owners: AI owners bid and the quote names them
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Bhiwandi", destination="Surat", weight_ton=14,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "ok", [])
text(7001, "Kal Bhiwandi se Surat 14 ton 32 MXL")
time.sleep(1.5)
L = D.loads[-1]
ok &= check("AI owners are asked in live mode", any(t["sandbox"] for t in L["threads"].values()), str(len(L["threads"])))
st = wait(L, ("needs_approval", "escalated"), 25)
ok &= check("AI owners produce a quote", st == "needs_approval", st)
ok &= check("quote says the owner is a simulated demo owner", "(demo owner, simulated)" in last_to(7001))

# 2. a real owner alone: the AI broker posts a demo load, approves it, the owner holds and is booked
text(8001, "owner Solo Transport, Surat")
time.sleep(0.5)
text(8001, "32 ft MXL, Surat, aaj, ₹36/km")
time.sleep(1.5)
ok &= check("demo load is due on the clock, not a timer", "tg:8001" in D.ai_due)
D.ai_due["tg:8001"] = 0                                # in the app it comes 20 s after the check-in
end = time.time() + 15                                 # routing may wait on OSRM
while time.time() < end and "Demo load" not in last_to(8001):
    time.sleep(0.3)
A = D.loads[-1]
ok &= check("AI broker posts a demo load to the owner", A.get("aiBroker") and "Demo load" in last_to(8001), A["id"])
ok &= check("demo load goes to real owners only", all(not t["sandbox"] for t in A["threads"].values()))
tgt = D._econ(A, A["threads"]["tg:8001"])["target"]
text(8001, f"{tgt - 1:.1f} per km")                     # a realistic ask; far below running cost would be confirmed first
st = wait(A, ("holding",), 25)
ok &= check("AI broker approves its own quote", st == "holding" and "HAAN" in last_to(8001), st)
text(8001, "haan")
ok &= check("owner is booked on the demo load", wait(A, ("booked",), 8) == "booked")
D.post("ai_broker", key="tg:8001")
time.sleep(1)
ok &= check("no second demo load within 10 min", D.loads[-1]["id"] == A["id"])

# 3. two open loads: an owner reply without a load number is asked which
D.settings["replyTimeoutSec"]["live"] = 60
D.settings["replyTimeoutLaterSec"]["live"] = 60
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Surat", destination="Pune", weight_ton=10,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "ok", [])
D.settings["aiAgents"] = False
text(7002, "Surat se Pune 10 ton")
time.sleep(1.5)
text(7003, "Surat se Pune 10 ton")
time.sleep(1.5)
X, Y = D.loads[-2], D.loads[-1]
both = "tg:8001" in X["threads"] and "tg:8001" in Y["threads"]
text(8001, "35 per km")
time.sleep(1)
ok &= check("two loads open: owner asked which", both and "kaunse ke liye" in last_to(8001))
text(8001, f"{X['id']} 35 per km")
time.sleep(1)
ok &= check("load number routes the reply", X["threads"]["tg:8001"]["status"] in ("deal", "countered")
            and Y["threads"]["tg:8001"]["status"] == "sent", X["threads"]["tg:8001"]["status"])

# 4. a truck reported 'today' two days ago is stale
old = {"ownerKey": "tg:8001", "source": "checkin", "available": "today", "at": time.time() - 2 * 86400}
ok &= check("yesterday's 'aaj' truck is stale", D._stale(old) and not D._stale({**old, "at": time.time()}))

# 5. a quote nobody approves expires and releases the owners
D.settings["approvalExpireSec"]["live"] = 2
for th in X["threads"].values():
    if th["status"] == "countered":
        text(8001, f"{X['id']} theek hai chalega")
time.sleep(1)
X["deadline"] = 0
st = wait(X, ("needs_approval", "escalated"), 10)
st = wait(X, ("escalated",), 10)
ok &= check("unapproved quote expires", st == "escalated" and X.get("reason", "").startswith("Quote expired"), st)

# 6. a restart: what the clock promised comes back from the saved state
D.ai_due["tg:8001"] = live.now() + 3600
D.loads[-1]["autoApproveAt"] = 12345
ok &= check("shutdown flush lands", D.flush_now())
R = live.Desk()
ok &= check("timers survive a restart", R.ai_due.get("tg:8001") == D.ai_due["tg:8001"]
            and R.loads[-1].get("autoApproveAt") == 12345)
h = c.get("/healthz")
ok &= check("health check reports the worker", h.status_code == 200 and h.json()["worker"] == "ok", h.text)

# 6. the strategist: stuck loads get a recovery move, checked by code; the broker's call books it
def stuck(lid, ask_mult, phase="quote"):
    lane = {"laneId": "Bhiwandi-Surat", "origin": "Bhiwandi", "destination": "Surat", "distanceKm": 284, "returnLoadProb": 0.75}
    sell = E.target_sell(lane, "32 ft MXL")
    ceil = round(sell * (1 - E.band(lane)["floorPct"] / 100) / 284, 1)
    ask = round(ceil * ask_mult, 1)
    th = {"ownerKey": "tg:8001", "name": "Solo Transport", "type": "32 ft MXL", "status": "declined", "messages": [], "sell": sell,
          "rounds": 3, "ask": ask, "counter": ceil - 3, "agreedBuy": None, "round": 1, "deadheadKm": 5, "sandbox": False,
          "reliability": 4.5}
    L = {"id": lid, "createdAt": time.time(), "source": "telegram", "brokerPhone": "tg:7009", "load": {"weight_ton": 14, "truck_type": "32 ft MXL",
         "pickup_when": "kal"}, "status": "sourcing", "events": [], "threads": {"tg:8001": th}, "brokerMessages": [], "quote": None,
         "winner": None, "rounds": 1, "mode": "live", "phase": phase, "fixedSell": None, "contacted": ["tg:8001"], "reason": "",
         "widenLeft": 0, "lane": lane, "searchType": "32 ft MXL", "raw": ""}
    D.loads.append(L)
    return L, ask
D.settings["aiAgents"] = False
D.settings["approvalExpireSec"]["live"] = 3600         # section 5 shortened it; quotes here must not expire mid-check
S1, ask1 = stuck("BH-9001", 1.05)
D._decide(S1, final=True)
ok &= check("strategist makes a final offer to a near owner", "final offer" in S1["events"][-1]["text"] and S1["threads"]["tg:8001"]["finalOffer"]
            and "aakhri koshish" in last_to(8001), S1["events"][-1]["text"][:80])
S2, ask2 = stuck("BH-9002", 1.4)
S2["triedTypes"] = list(E.rates["capacityTonByType"])          # nothing bigger left to try
D._decide(S2, final=True)
ok &= check("far apart: strategist asks the broker", S2["status"] == "needs_call" and "Is rate pe quote banaun" in last_to(7009), S2["status"])
tap_id = next(b for to, t, b in reversed(SENT) if to == "tg:7009" and b)[0][0]
n[0] += 1
c.post(URL, json={"update_id": n[0], "callback_query": {"id": "qz", "data": tap_id, "message": {"chat": {"id": 7009}}}})
time.sleep(1)
ok &= check("broker's yes turns it into a quote at the floor", S2["status"] == "needs_approval" and S2["quote"]["margin_pct"] >= E.band(S2["lane"])["floorPct"] - 0.1,
            f"{S2['status']} {S2.get('quote', {}) and S2['quote'].get('margin_pct')}")

# 7. the shipper says no: the held truck is released and the owner told
S3, _ = stuck("BH-9003", 1.0)
S3.update(status="booked", winner="tg:8001", quote={"sell_total": 30000}, bookedAt=time.time())
S3["threads"]["tg:8001"]["status"] = "held"
D._on_shipper("BH-9003", False)
ok &= check("shipper's no releases the truck", S3["status"] == "lost" and S3["threads"]["tg:8001"]["status"] == "released"
            and "shipper ne mana" in last_to(8001), S3["status"])
D._on_shipper("BH-9003", True)
ok &= check("a second answer changes nothing", S3["status"] == "lost" and S3.get("shipper") == "no")

# 8. a judge on the open console tries a lane with no demo truck nearby: demo owners still bid, nothing waits 6 h
J = D.start_load(E.normalise_load(dict(origin="Hyderabad", destination="Mumbai", weight_ton=5, truck_type="22 ft",
                                       pickup_when="kal")), "console", demo_only=True)
time.sleep(1.5)
ok &= check("open-console load on an uncovered lane reaches demo owners", bool(J["threads"]) and J["status"] != "waiting_trucks"
            and all(t["sandbox"] for t in J["threads"].values()), f"{J['status']} {len(J['threads'])}")

# 8b. a firm hold: the vehicle, driver and reporting time come with the HAAN, so nobody asks again
S4, _ = stuck("BH-9004", 1.0)
S4.update(status="holding", winner="tg:8001", quote={"sell_total": 30000, "buy_total": 27000}, approvedAt=time.time(),
          deadline=time.time() + 600, fixedSell=30000)
S4["threads"]["tg:8001"].update(status="hold_requested", agreedBuy=27000)
D._hold_reply(S4, S4["threads"]["tg:8001"], "HAAN MH04AB1234, 9812345678, subah 8 baje")
T4 = S4["threads"]["tg:8001"]
ok &= check("HAAN with the vehicle is a firm booking", S4["status"] == "booked" and T4.get("vehicle") == "MH04AB1234"
            and T4.get("driverPhone") == "9812345678" and not T4.get("needDispatch") and "subah 8 baje" in (T4.get("reportAt") or "")
            and "with the vehicle number" in next(e["text"] for e in S4["events"] if "confirmed the hold" in e["text"]),
            f"{S4['status']} {T4.get('vehicle')} {T4.get('reportAt')}")

# 8c. the broker names the shipper's price and their margin: that, not the desk's band, decides what we pay
def routed(lid_fields, secs=6):
    L = D.start_load(E.normalise_load(lid_fields), "console", demo_only=True)
    end = time.time() + secs
    while time.time() < end and not L.get("threads") and L["status"] in ("routing", "new"):
        time.sleep(0.2)
    time.sleep(0.5)
    return L
B1 = routed(dict(origin="Bhiwandi", destination="Surat", weight_ton=18, material="kapda", truck_type="32 ft MXL", pickup_when="kal",
                 budget_rs=30000, margin_pct=8))
km1 = B1["lane"]["distanceKm"]
th1 = next(iter(B1["threads"].values()), {})
ok &= check("broker's terms: their margin is the floor and the shipper price is the sell", B1["lane"].get("brokerBand", {}).get("floorPct") == 8
            and B1.get("shipperPrice") == 30000 and th1.get("sell") == 30000
            and abs(D._econ(B1, th1)["ceiling"] - round(30000 * 0.92 / km1, 1)) < 0.11, f"{B1['lane'].get('brokerBand')} {th1.get('sell')}")
B2 = routed(dict(origin="Bhiwandi", destination="Surat", weight_ton=18, material="kapda", truck_type="32 ft MXL", pickup_when="kal",
                 budget_rs=15000, margin_pct=10))
ok &= check("a margin that leaves owners below running cost is flagged to the broker",
            any("kharche" in m["text"] for m in B2.get("brokerMessages", [])) or any("running cost" in e["text"] for e in B2["events"]),
            str([e["text"][:60] for e in B2["events"]][-3:]))

# 8d. the morning check-in goes out by itself, once a day
D.owners["tg:8701"] = {"key": "tg:8701", "phone": "tg:8701", "name": "Subah Transport", "baseCity": "Surat", "reliability": 4.5, "sandbox": False}
D.checkin = {"sentAt": None, "asked": 0}
D.settings.update(checkinAuto=True, checkinHourIST=0, checkinUntilHourIST=24)
time.sleep(2.5)
first = [t for to, t, b in SENT if to == "tg:8701" and "roz subah" in t]
time.sleep(2.0)
again = [t for to, t, b in SENT if to == "tg:8701" and "roz subah" in t]
D.settings["checkinAuto"] = False
ok &= check("the morning check-in goes out on its own and asks type, place and day", len(first) == 1 and "kahan khadi" in first[0]
            and "kab se free" in first[0] and bool(D.checkin.get("sentAt")), str(first)[:120])
ok &= check("and only once a day", len(again) == 1, len(again))

# 9. lane memory: five deals before it counts; an owner who backed out is forgotten
M = {"id": "BH-9100", "createdAt": time.time() - 5, "lane": {"laneId": "Pune-Nashik", "distanceKm": 210},
     "threads": {f"o{i}": {"ownerKey": f"o{i}", "type": "22 ft", "agreedBuy": 210 * (60 + i)} for i in range(5)}}
for i in range(4):
    D._remember_rate(M, M["threads"][f"o{i}"])
ok &= check("lane memory ignores fewer than five deals", "22 ft" not in D._lane_memory("Pune-Nashik"))
D._remember_rate(M, M["threads"]["o4"])
ok &= check("five deals make a lane rate", D._lane_memory("Pune-Nashik").get("22 ft") == 62.0, str(D._lane_memory("Pune-Nashik")))
D._forget_rate(M, "o4")
ok &= check("a backed-out owner's rate is forgotten", "22 ft" not in D._lane_memory("Pune-Nashik"))

print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
