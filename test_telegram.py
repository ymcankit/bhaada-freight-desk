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
import server
from fastapi.testclient import TestClient

E.LLM["key"] = ""
E.llm_ready = lambda: True
SENT = []
server.tg_send = lambda to, text, buttons=None: SENT.append((to, text, buttons))
server.desk.send_wa = server.send
server.desk.settings["mode"] = "live"
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
ok &= check("/start greets the broker", "Bhaada desk" in last_to(BROKER))
for i, ch in enumerate(OWNERS):
    text(ch, f"owner TG Owner {i + 1}, Bhiwandi")
ok &= check("owners register over Telegram", "Registered as a fleet owner" in last_to(OWNERS[0]))
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
n[0] += 1
c.post(URL, json={"update_id": n[0], "callback_query": {"id": "q1", "data": btn[0][0], "message": {"chat": {"id": BROKER}}}})
time.sleep(1)
w = L["winner"]
ok &= check("button approves and owner is asked to hold", "HAAN" in last_to(w[3:]))
text(int(w[3:]), "haan")
end = time.time() + 8
while time.time() < end and L["status"] != "booked":
    time.sleep(0.3)
ok &= check("booked", L["status"] == "booked", L["status"])
ok &= check("state reports Telegram", "telegram" in c.get("/api/state").json())
print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
