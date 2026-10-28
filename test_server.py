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
import server
from fastapi.testclient import TestClient

E.LLM["key"] = ""
E.llm_ready = lambda: True                           # intake is stubbed below; every other model call fails fast to the rules
SENT = []
server.WA_READY = True
server.desk.send_wa = lambda to, text, buttons=None: SENT.append((to, text, buttons))
server.wa_send = server.desk.send_wa
server.desk.settings["mode"] = "live"
server.desk.settings["replyTimeoutSec"]["live"] = 6
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Bhiwandi", destination="Surat", weight_ton=14,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "Finding a truck", [])
c = TestClient(server.app)
BROKER, OWNERS = "919800000001", ["919800000011", "919800000012", "919800000013"]
n = [0]

def wa(frm, **content):
    n[0] += 1
    c.post("/webhook", json={"entry": [{"changes": [{"value": {"messages": [{"id": f"m{n[0]}", "from": frm, **content}]}}]}]})

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
ok &= check("owner gets a welcome", "Registered as a fleet owner" in last_to(OWNERS[0]))

for ph in OWNERS:
    text(ph, "32 ft MXL, Bhiwandi, aaj, ₹38/km")
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
ok &= check("broker told about the recovery", "re-booked" in last_to(BROKER))
state = c.get("/api/state").json()
ok &= check("state API reports the day", state["kpis"]["received"] == 1 and state["kpis"]["booked"] == 1, str(state["kpis"]))
ok &= check("owner phones masked in the API", all(o["phone"].startswith("••••") for o in state["owners"]))
R = live.Desk._owner_rate
ok &= check("owner rates read in context", R("₹38/km lagega", 280) == 38 and R("38k", 1000) == 38 and R("35 hazaar total", 1000) == 35
            and R("32 ft hai, 36 per km, 1500 toll alag", 280) == 36 and R("14 ton ka 40", 280) == 40 and R("kal 10 baje", 280) is None)
ok &= check("'haan na' is not a no", not live.NO.search("haan na bhai") and bool(live.NO.search("na") and live.NO.search("nahi")))

print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
