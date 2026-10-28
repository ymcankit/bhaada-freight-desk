"""The same live desk over fake Telegram traffic: owners register, check in and quote; a broker sends a load and
approves with the inline button; the owner holds. No Telegram account and no LLM.
Run:  .venv/bin/python test_telegram.py
"""
import os, time, tempfile
from pathlib import Path
os.environ["GROQ_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:" + "x" * 35       # fake, but shaped like a real token
import engine as E
import live
live.STATE_FILE = Path(tempfile.gettempdir()) / "bhaada_tg_test_state.json"
live.STATE_FILE.unlink(missing_ok=True)
live._store = lambda: None                            # never the real Upstash, even with its keys in the shell
import server
from fastapi.testclient import TestClient

E.LLM["key"] = E.LLM["openai_key"] = ""          # no real model calls: rules take over
E.llm_ready = lambda: True
SENT = []
server.tg_send = lambda to, text, buttons=None: SENT.append((to, text, buttons))
server.desk.send_wa = server.send
server.desk.settings["mode"] = "live"
server.desk.settings["aiAgents"] = False                # AI owners and the AI broker have their own test (test_agents.py)
server.desk.settings["replyTimeoutSec"]["live"] = 6
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Bhiwandi", destination="Surat", weight_ton=14,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "ok", [])
c = TestClient(server.app)
URL = f"/telegram/{server.TG_PATH}"
BROKER, OWNERS = 5001, [6001, 6002]
n = [0]

def text(chat, body):
    n[0] += 1
    c.post(URL, json={"update_id": n[0], "message": {"chat": {"id": chat}, "text": body}})

def last_to(chat):
    return next((t for to, t, b in reversed(SENT) if to == f"tg:{chat}"), "")

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

ok = check("wrong webhook path refused", c.post("/telegram/nope", json={}).status_code == 404)
text(BROKER, "/start")
menu = next((b for to, t, b in reversed(SENT) if to == f"tg:{BROKER}"), None)
ok &= check("/start offers owner or broker buttons", menu and [i for i, _ in menu] == ["role:owner", "role:broker"])
def tap(chat, data):
    n[0] += 1
    c.post(URL, json={"update_id": n[0], "callback_query": {"id": f"q{n[0]}", "data": data, "message": {"chat": {"id": chat}}}})
tap(6900, "role:owner")
ok &= check("tapping Truck owner asks for name and city", "naam ya company" in last_to(6900))
text(6900, "Tap Transport, Surat")
time.sleep(0.5)
ok &= check("the next message registers them", "tg:6900" in server.desk.owners and server.desk.owners["tg:6900"]["name"] == "Tap Transport")
text(6900, "hi")
ok &= check("hi again shows their current role", "Tap Transport" in last_to(6900))
tap(6900, "role:broker")
ok &= check("tapping Broker switches them", "tg:6900" not in server.desk.owners and "Aap broker hain" in last_to(6900))
for i, ch in enumerate(OWNERS):
    text(ch, f"owner TG Owner {i + 1}, Bhiwandi")
ok &= check("owners register over Telegram", "fleet owner ke roop mein jud gaye" in last_to(OWNERS[0]))
for ch in OWNERS:
    text(ch, "32 ft MXL, Bhiwandi, aaj, ₹38/km")
time.sleep(1)
text(BROKER, "Kal Bhiwandi se Surat 14 ton 32 MXL")
time.sleep(2)
L = server.desk.loads[-1]
ok &= check("load from Telegram is labelled", L["source"] == "telegram", L["source"])
ok &= check("owners got the load", all("Load:" in last_to(ch) for ch in OWNERS))
target = server.desk._econ(L, L["threads"][f"tg:{OWNERS[0]}"])["target"]
for ch in OWNERS:
    text(ch, f"{target - 1:.1f} per km")
end = time.time() + 15
while time.time() < end and L["status"] not in ("needs_approval", "escalated"):
    time.sleep(0.3)
ok &= check("quote ready", L["status"] == "needs_approval", L["status"])
btn = next((b for to, t, b in reversed(SENT) if to == f"tg:{BROKER}" and b), None)
ok &= check("broker gets inline buttons", bool(btn) and btn[0][0].startswith("approve:"))
text(BROKER, "haan")                                        # typed instead of tapping the button
time.sleep(1)
w = L["winner"]
ok &= check("typed 'haan' approves and owner is asked to hold", L["status"] == "holding" and "HAAN" in last_to(w[3:]), L["status"])
n[0] += 1
c.post(URL, json={"update_id": n[0], "callback_query": {"id": "q1", "data": btn[0][0], "message": {"chat": {"id": BROKER}}}})
time.sleep(0.5)
ok &= check("a second tap says the quote is no longer open", "ab khula nahi" in last_to(BROKER), last_to(BROKER)[:60])
text(int(w[3:]), "haan")
end = time.time() + 8
while time.time() < end and L["status"] != "booked":
    time.sleep(0.3)
ok &= check("booked", L["status"] == "booked", L["status"])
ok &= check("owner asked for vehicle and driver", "gaadi ka number" in last_to(int(w[3:])))
ok &= check("hold message states the payment terms", any("Advance" in t and "POD" in t for to, t, b in SENT if to == w))
text(int(w[3:]), "gaadi MH04AB1234 hai, driver 9812345678")
time.sleep(1)
card = last_to(BROKER)
ok &= check("broker gets a dispatch card with paperwork", "MH04AB1234" in card and "9812345678" in card and "E-way bill" in card
            and "GST" in card, card[:120])
ok &= check("vehicle stored on the load", L["threads"][w].get("vehicle") == "MH04AB1234")
text(OWNERS[1], "broker")
ok &= check("an owner can switch to broker", f"tg:{OWNERS[1]}" not in server.desk.owners and "Ab aap broker" in last_to(OWNERS[1]))
before = len(server.desk.loads)
text(OWNERS[1], "Kal Bhiwandi se Surat 14 ton 32 MXL")
time.sleep(1)
ok &= check("and then sends a load", len(server.desk.loads) == before + 1)
ok &= check("no WhatsApp markup on Telegram", not any("*" in t for to, t, b in SENT))
# a load nobody can serve yet waits, and goes out the moment a matching truck checks in
server.desk.remove_owner(f"tg:{OWNERS[0]}")                    # no owners at all, as after the memory was wiped
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Surat", destination="Bhiwandi", weight_ton=14,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "ok", [])
text(OWNERS[1], "Kal Surat se Bhiwandi 14 ton 32 MXL")
time.sleep(2)
W = server.desk.loads[-1]
ok &= check("no truck yet: load kept open", W["status"] == "waiting_trucks" and "khula rakha" in last_to(OWNERS[1]), W["status"])
text(OWNERS[0], "owner Nikunj Transport, Surat")
time.sleep(2)
ok &= check("an owner who joins later gets the waiting load", W["status"] == "sourcing" and "Load:" in last_to(OWNERS[0]), W["status"])
ok &= check("state reports Telegram", "telegram" in c.get("/api/state").json())
server.TG_BOT["hooked"] = True                                 # setWebhook took the secret: the header is required
upd = {"update_id": 99999, "message": {"chat": {"id": 7777}, "text": "hi"}}
ok &= check("webhook without the secret header refused", c.post(URL, json=upd).status_code == 403
            and c.post(URL, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403)
ok &= check("webhook with the secret header accepted",
            c.post(URL, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": server.TG_SECRET}).status_code == 200)
ok &= check("bot token kept out of logged errors", server.TG_TOKEN not in server.safe(
            ConnectionError(f"Max retries exceeded with url: /bot{server.TG_TOKEN}/sendMessage")))
print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
