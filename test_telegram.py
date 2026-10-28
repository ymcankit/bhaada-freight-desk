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
DOCS = []
server.tg_send_document = lambda to, att, caption="": DOCS.append((to, att))
FILES = {}                                            # file_id -> bytes, in place of Telegram's getFile and download
server.tg_fetch = lambda fid: FILES[fid]
server.desk.send_wa = server.send
server.desk.settings["mode"] = "live"
server.desk.settings["aiAgents"] = False                # AI owners and the AI broker have their own test (test_agents.py)
server.desk.settings["replyTimeoutSec"]["live"] = 6
E.intake_turn = lambda hist, load, msg: (E.normalise_load(dict(origin="Bhiwandi", destination="Surat", weight_ton=14,
                                                               truck_type="32 ft MXL", pickup_when="kal")), "ok", [])
c = TestClient(server.app, headers={"X-Telegram-Bot-Api-Secret-Token": server.TG_SECRET})   # as Telegram sends it
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
fwd = next((t for to, t, b in SENT if to == f"tg:{BROKER}" and "Shipper ke liye" in t), "")
ok &= check("broker gets a forwardable shipper message", f"{L['quote']['sell_total']:,}" in fwd and "GST" in fwd and "valid" in fwd
            and L["threads"][w]["name"] not in fwd and f"{L['quote']['buy_total']:,}" not in fwd, fwd[:160])
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
btn = next(b for to, t, b in reversed(SENT) if to == f"tg:{BROKER}" and b and b[0][0].startswith("ship_yes"))
ok &= check("after the hold the broker relays the shipper's answer", btn[0][0] == f"ship_yes:{L['id']}")
n[0] += 1
c.post(URL, json={"update_id": n[0], "callback_query": {"id": "qs", "data": btn[0][0], "from": {"id": BROKER},
                                                        "message": {"chat": {"id": BROKER, "type": "private"}}}})
time.sleep(0.5)
ok &= check("shipper's yes is recorded", L.get("shipper") == "yes")
text(BROKER, f"mera rate {L['id']} 28,000 tha")
time.sleep(0.5)
ok &= check("shadow mode records the broker's own quote", L.get("brokerRate") == 28000 and "28,000" in last_to(BROKER)
            and c.get("/api/state").json()["kpis"]["shadowCompared"] >= 1)
import io
from pypdf import PdfReader, PdfWriter
lr = next((a for to, a in DOCS if to == f"tg:{BROKER}"), None)
lr_text = PdfReader(io.BytesIO(lr[1])).pages[0].extract_text() if lr else ""
ok &= check("dispatch sends an LR (bilty) PDF draft", lr and lr[0] == "LR-" + L["id"][3:] + ".pdf" and "MH04AB1234" in lr_text
            and "XXXXXX5678" in lr_text and "9812345678" not in lr_text and "DRAFT" in lr_text
            and f"{L['quote']['sell_total']:,}" in lr_text and "Advance" in lr_text and "to fill" in lr_text, lr_text[:200])
got = c.get(f"/api/loads/{L['id']}/lr.pdf")
ok &= check("console downloads the LR", got.status_code == 200 and got.content[:5] == b"%PDF-"
            and got.headers["content-type"] == "application/pdf")
ok &= check("no LR before dispatch", c.get(f"/api/loads/BH-0000/lr.pdf").status_code == 404)
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
upd = {"update_id": 99999, "message": {"chat": {"id": 7777}, "text": "hi"}}
ok &= check("webhook without the secret header refused", TestClient(server.app).post(URL, json=upd).status_code == 403
            and c.post(URL, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403)
ok &= check("webhook with the secret header accepted",
            c.post(URL, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": server.TG_SECRET}).status_code == 200)
def post(update):
    n[0] += 1
    c.post(URL, json={"update_id": n[0], **update})
post({"message": {"chat": {"id": -1008, "type": "group"}, "text": "hi"}})
post({"callback_query": {"id": "qg", "data": "role:owner", "from": {"id": 9999}, "message": {"chat": {"id": 8801, "type": "private"}}}})
ok &= check("groups and other people's taps are ignored", not last_to(-1008) and not last_to(8801))
post({"message": {"chat": {"id": 8802}, "voice": {"file_id": "f", "duration": 900, "mime_type": "audio/ogg"}}})
ok &= check("a long voice note is refused before download", "2 minute" in last_to(8802))
server.RATE["perMin"] = 3
for _ in range(6):
    text(8803, "hi")
mine = [t for to, t, b in SENT if to == "tg:8803"]
ok &= check("a flood from one chat is cut off, told once", len(mine) == 4 and "ruk ke" in mine[-1], str(len(mine)))
server.RATE["perMin"] = 12
text(8804, "hi " + "x" * 5000)
time.sleep(0.5)
raw = [l["raw"] for l in server.desk.broker_loads("tg:8804")]
ok &= check("long messages are cut before anything reads them", raw and len(raw[0]) <= server.MAX_TEXT, str([len(r) for r in raw]))
ok &= check("the welcome says what is kept and how to delete it", "delete my data" in next(
            (t for to, t, b in SENT if to == f"tg:{OWNERS[0]}" and "jud gaye" in t), ""))
# a broker forwards a PDF load request, or a photo of a group post
FILES["pdf1"] = (Path(__file__).parent / "data" / "sample_load_request.pdf").read_bytes()
before = len(server.desk.loads)
post({"message": {"chat": {"id": 8810}, "document": {"file_id": "pdf1", "mime_type": "application/pdf", "file_size": len(FILES["pdf1"])}}})
time.sleep(1)
raw = [l["raw"] for l in server.desk.broker_loads("tg:8810")]
ok &= check("a PDF load request is read and becomes a load", "Padha:" in "".join(t for to, t, b in SENT if to == "tg:8810")
            and "Bhiwandi" in next((t for to, t, b in SENT if to == "tg:8810" and "Padha" in t), "")
            and len(server.desk.loads) == before + 1 and raw and "Ahmedabad" in raw[0], str(raw)[:80])
w6 = PdfWriter()
for _ in range(6):
    w6.add_blank_page(100, 100)
buf = io.BytesIO(); w6.write(buf); FILES["pdf6"] = buf.getvalue()
post({"message": {"chat": {"id": 8811}, "document": {"file_id": "pdf6", "mime_type": "application/pdf", "file_size": 900}}})
ok &= check("a PDF over 5 pages is refused", "5 page" in last_to(8811), last_to(8811))
post({"message": {"chat": {"id": 8812}, "document": {"file_id": "x", "mime_type": "application/pdf", "file_size": 5_000_000}}})
ok &= check("a file over 4 MB is refused before download", "4 MB" in last_to(8812))
FILES["img"] = b"\xff\xd8fake-jpeg"
photo = {"chat": {"id": 8813}, "photo": [{"file_id": "small", "file_size": 10}, {"file_id": "img", "file_size": 11}]}
post({"message": photo})
ok &= check("a photo with no OpenAI key: asked to type it", "type kar dijiye" in last_to(8813))
seen = []
E.LLM["openai_key"] = "test-key"
real_chat, E.chat = E.chat, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model in tests"))
E.read_image = lambda data, mime: seen.append((data, mime)) or "Surat se Bhiwandi\n14 ton kapda, 32 ft MXL, kal"
post({"message": {**photo, "caption": "urgent"}})
time.sleep(1)
E.LLM["openai_key"], E.chat = "", real_chat
ok &= check("a photo is read by the vision model (largest size) and becomes a load", seen == [(FILES["img"], "image/jpeg")]
            and "Padha: Surat se Bhiwandi" in "".join(t for to, t, b in SENT if to == "tg:8813")
            and any("urgent" in l["raw"] for l in server.desk.broker_loads("tg:8813")))
post({"message": {"chat": {"id": OWNERS[0]}, "document": {"file_id": "pdf1", "mime_type": "application/pdf", "file_size": 900}}})
ok &= check("an owner's file is not read as a load", "sirf brokers" in last_to(OWNERS[0]))
post({"message": {"chat": {"id": -1009, "type": "group"}, "document": {"file_id": "pdf1", "mime_type": "application/pdf"}}})
ok &= check("files in groups are ignored", not last_to(-1009))
post({"message": {"chat": {"id": 8814}, "sticker": {"file_id": "s"}}})
ok &= check("other media gets the 'type it' reply", "PDF aur photo" in last_to(8814))
import json as _json
held =lambda who: any(f"tg:{who}" in _json.dumps(x, ensure_ascii=False) for x in
                       (server.desk.owners, server.desk.loads, server.desk.brokers, server.desk.pool))
before = held(OWNERS[0]) and held(BROKER)
text(OWNERS[0], "delete my data")
text(BROKER, "Delete my data")
time.sleep(1)
ok &= check("'delete my data' removes the person from the desk", before and not held(OWNERS[0]) and not held(BROKER)
            and "hata diye" in last_to(BROKER))
ok &= check("bot token kept out of logged errors", server.TG_TOKEN not in server.safe(
            ConnectionError(f"Max retries exceeded with url: /bot{server.TG_TOKEN}/sendMessage")))
print("\nALL PASS" if ok else "\nSOME FAILED")
import sys; sys.stdout.flush(); os._exit(0 if ok else 1)
