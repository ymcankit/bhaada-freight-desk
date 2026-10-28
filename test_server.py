"""End-to-end test of the live desk over fake WhatsApp traffic: a real owner registers, answers the check-in,
a broker sends a load, owners negotiate, the broker approves on WhatsApp, the owner holds, then backs out
and the desk re-books. No Meta account and no LLM (rules take over); OSRM/Nominatim are used if reachable.
Run:  .venv/bin/python test_server.py
"""
import os, time, tempfile
from pathlib import Path
os.environ["GROQ_API_KEY"] = ""                       # rules only, so the test is deterministic and free
import engine as E
import live
live.STATE_FILE = Path(tempfile.gettempdir()) / "bhaada_test_state.json"
live.STATE_FILE.unlink(missing_ok=True)
live._store = lambda: None                            # never the real Upstash, even with its keys in the shell
import server
from fastapi.testclient import TestClient

E.LLM["key"] = E.LLM["openai_key"] = ""          # no real model calls: rules take over
E.llm_ready = lambda: True                           # intake is stubbed below; every other model call fails fast to the rules
SENT = []
server.WA_READY = True
server.desk.send_wa = lambda to, text, buttons=None: SENT.append((to, text, buttons))
server.wa_send = server.desk.send_wa
server.desk.settings["mode"] = "live"
server.desk.settings["aiAgents"] = False                # AI owners and the AI broker have their own test (test_agents.py)
server.desk.settings["replyTimeoutSec"]["live"] = 6
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Bhiwandi", destination="Surat", weight_ton=14,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "Finding a truck", [])
c = TestClient(server.app)
BROKER, OWNERS = "919800000001", ["919800000011", "919800000012", "919800000013"]
n = [0]

import hmac, hashlib, json as _json
server.WA_SECRET = "s3cret"                          # Meta signs every POST; the test does the same

def wa(frm, **content):
    n[0] += 1
    body = _json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": f"m{n[0]}", "from": frm, **content}]}}]}]}).encode()
    c.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()})

def text(frm, body):
    wa(frm, type="text", text={"body": body})

def last_to(who):
    return next((t for to, t, b in reversed(SENT) if to == who), "")

def load():
    return server.desk.loads[-1]

def wait(states, secs=20):
    end = time.time() + secs
    while time.time() < end and load()["status"] not in states:
        time.sleep(0.3)
    return load()["status"]

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

ok = True
ok &= check("console page served", c.get("/").status_code == 200 and "Bhaada" in c.get("/").text)
for i, ph in enumerate(OWNERS):
    text(ph, f"owner Test Owner {i + 1}, Bhiwandi")
ok &= check("owners register over WhatsApp", len([o for o in server.desk.owners.values() if not o.get("sandbox")]) == 3)
ok &= check("owner gets a welcome", "fleet owner ke roop mein jud gaye" in last_to(OWNERS[0]))

for ph in OWNERS:
    text(ph, "32 ft MXL, Bhiwandi, aaj, ₹85/km")
time.sleep(1)
pool = [t for t in server.desk.pool if t["source"] == "checkin"]
ok &= check("check-in replies build the pool", len(pool) == 3, str([(t["type"], t["city"], t["ratePerKm"]) for t in pool]))

text(BROKER, "Kal Bhiwandi se Surat 14 ton 32 MXL")
time.sleep(2)
ok &= check("broker load starts", load()["status"] == "sourcing", load()["status"])
ok &= check("owners got the load on WhatsApp", all("Load:" in last_to(p) for p in OWNERS))
ok &= check("lane has a road distance", load()["lane"]["distanceKm"] > 100, load()["lane"]["distanceSrc"])

target = server.desk._econ(load(), load()["threads"][OWNERS[0]])["target"]
text(OWNERS[0], f"{target - 1:.1f} per km")                # under target: accepted outright
text(OWNERS[1], f"{target + 6:.1f}/km lagega")              # above: gets a counter
time.sleep(1)
ok &= check("negotiation countered the high ask", "₹" in last_to(OWNERS[1]) and load()["threads"][OWNERS[1]]["status"] == "countered")
text(OWNERS[1], "theek hai chalega")
st = wait(("needs_approval", "escalated"), 15)               # owner 3 stays silent; the round times out
ok &= check("quote ready after the round", st == "needs_approval", st)
ok &= check("silent owner marked", load()["threads"][OWNERS[2]]["status"] == "silent")
quote_msg = next(((t, b) for to, t, b in reversed(SENT) if to == BROKER and b), (None, None))
ok &= check("broker gets the quote with buttons", quote_msg[1] and quote_msg[1][0][0].startswith("approve:"))
ok &= check("quote clears the floor", load()["quote"]["above_floor"], f"{load()['quote']['margin_pct']}%")

before = len(server.desk.loads)
text(BROKER, "status?")                                     # a question, not a new load
ok &= check("broker 'status?' gets the quote back, not a new intake", len(server.desk.loads) == before
            and "Margin" in last_to(BROKER) and bool(SENT[-1][2]), last_to(BROKER)[:60])
wa("919800000099", type="interactive", interactive={"type": "button_reply", "button_reply": {"id": f"approve:{load()['id']}", "title": "Approve"}})
time.sleep(0.5)
ok &= check("someone else's approve is refused", load()["status"] == "needs_approval" and "button" in last_to("919800000099"))
winner = load()["winner"]
wa(BROKER, type="interactive", interactive={"type": "button_reply", "button_reply": {"id": f"approve:{load()['id']}", "title": "Approve"}})
time.sleep(1)
ok &= check("owner asked to hold", "HAAN" in last_to(winner))
text(winner, "haan")
ok &= check("booked after the hold", wait(("booked",), 8) == "booked")

text(winner, "payment kab milega? advance nahi mila na")    # a booked owner's question is not a back-out
time.sleep(1)
ok &= check("booked owner's question keeps the booking", load()["status"] == "booked" and load()["threads"][winner]["status"] == "held"
            and "booking pakki" in last_to(winner), last_to(winner))
text(winner, "sorry bhai gaadi nahi aa payegi, cancel")
backup = [k for k in OWNERS if k not in (winner, OWNERS[2])][0]
time.sleep(1)
ok &= check("backup asked to re-confirm", load()["threads"][backup]["status"] == "reconfirm" and "HAAN" in last_to(backup))
text(backup, "haan chalega")
st = wait(("recovered", "escalated"), 15)
ok &= check("back-out re-booked with the backup", st == "recovered" and load()["winner"] == backup, st)
ok &= check("shipper price unchanged", load()["quote"]["sell_total"] == load()["fixedSell"])
ok &= check("backed-out owner penalised", server.desk.reliability.get(winner, 0) < 0)
ok &= check("broker told about the recovery", "dobara book" in last_to(BROKER))
state = c.get("/api/state").json()
ok &= check("state API reports the day", state["kpis"]["received"] == 1 and state["kpis"]["booked"] == 1, str(state["kpis"]))
ok &= check("owner phones masked in the API", all(o["phone"].startswith("••••") for o in state["owners"]))
raw_state = c.get("/api/state").text
ok &= check("no phone number anywhere in the API", not any(p in raw_state for p in [BROKER] + OWNERS))
# Upstash down at boot: the blank desk must not overwrite what Upstash holds, and merges it once it answers
REMOTE = {"up": False, "sets": [], "state": {"owners": {"91999": {"key": "91999", "phone": "91999", "name": "Old Owner"}}, "seq": 1500}}
def fake_redis(cmd):
    if not REMOTE["up"]:
        raise ConnectionError("upstash down")
    if cmd[0] == "SET":
        REMOTE["sets"].append(cmd[2])
    if cmd[0] == "HSET":
        REMOTE.setdefault("archived", []).extend(cmd[2::2])
    return live.json.dumps(REMOTE["state"]) if cmd[0] == "GET" else "OK"
real_store, real_redis = live._store, live._redis
live._store, live._redis = (lambda: ("https://x", "t")), fake_redis
d2 = live.Desk()
d2.save()
ok &= check("Upstash down at boot: nothing overwritten", not d2.remote_ok and not REMOTE["sets"])
REMOTE["up"] = True
d2._remote_retry = 0                                        # skip the 30 s back-off between reconnect tries
d2.flush()
ok &= check("Upstash back: old owners merged, then saved", "91999" in d2.owners and d2._seq >= 1500 and len(REMOTE["sets"]) == 1)
d2.flush()
ok &= check("unchanged state is not re-sent", len(REMOTE["sets"]) == 1)
for i in range(20):
    d2.reliability[f"x{i}"] = -0.1
    d2.save()
ok &= check("a burst of saves is coalesced", len(REMOTE["sets"]) == 1 and d2._dirty)
d2.flush()
ok &= check("and written once", len(REMOTE["sets"]) == 2 and not d2._dirty)
old = live.now() - 5 * 86400
d2.loads.append({"id": "BH-1", "createdAt": old, "closedAt": old, "status": "booked"})
d2.loads.append({"id": "BH-2", "createdAt": old, "status": "waiting_trucks"})
d2.flush()
ok &= check("old closed loads move to the archive, open ones stay", REMOTE.get("archived") == ["BH-1"]
            and "BH-2" in [l["id"] for l in d2.loads] and "BH-1" not in [l["id"] for l in d2.loads]
            and '"BH-1"' not in REMOTE["sets"][-1])
h = d2.health()
ok &= check("health reports the worker and the store", h["worker"] == "ok" and h["store"] == "upstash", str(h))
live._store, live._redis = real_store, real_redis
text(BROKER, "owner ne rate kya bola?")                    # after the day's checks: the stubbed intake makes it a load
ok &= check("a broker saying 'owner ...' is not registered as an owner", BROKER not in server.desk.owners)

# webhook signature, console password and settings validation
body = _json.dumps({"entry": []}).encode()
sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
ok &= check("unsigned WhatsApp POST refused", c.post("/webhook", content=body).status_code == 403)
ok &= check("signed WhatsApp POST accepted", c.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200)
server.WA_SECRET = ""
ok &= check("no app secret: WhatsApp POSTs refused, not trusted", c.post("/webhook", content=body).status_code == 403)
server.CONSOLE_PW = "pw"
ok &= check("console locked without the password", c.get("/api/state").json() == {"locked": True}
            and c.post("/api/mode", json={"mode": "live"}).status_code == 401 and c.get("/healthz").status_code == 200)
ok &= check("console opens with the password", "kpis" in c.get("/api/state", headers={"X-Console-Key": "pw"}).json())
server.CONSOLE_PW = ""
floor = server.desk.settings["floorPct"]
ok &= check("bad margins refused and not kept", c.post("/api/settings", json={"floorPct": 60}).status_code == 400
            and server.desk.settings["floorPct"] == floor and c.post("/api/settings", json={"floorPct": "abc"}).status_code == 400)

R = live.Desk._owner_rate
ok &= check("owner rates read in context", R("₹38/km lagega", 280) == 38 and R("38k", 1000) == 38 and R("35 hazaar total", 1000) == 35
            and R("32 ft hai, 36 per km, 1500 toll alag", 280) == 36 and R("14 ton ka 40", 280) == 40 and R("kal 10 baje", 280) is None)
ok &= check("'haan na' is not a no", not live.NO.search("haan na bhai") and bool(live.NO.search("na") and live.NO.search("nahi")))

print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
