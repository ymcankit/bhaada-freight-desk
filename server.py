"""
Bhaada server: the broker's console (static/), its JSON API, and the WhatsApp Cloud API webhook, on one live desk.

Run:   uvicorn server:app --port 8000          then open http://localhost:8000
Config (environment variables, or .streamlit/secrets.toml locally):
    GROQ_API_KEY                       language model (intake, negotiation, check-in parsing)
    WHATSAPP_TOKEN, WHATSAPP_PHONE_ID  Meta WhatsApp Cloud API (leave empty to run the console alone)
    WHATSAPP_VERIFY_TOKEN              any string; the same one goes into Meta's webhook settings
    TELEGRAM_BOT_TOKEN                 a bot from @BotFather: the same desk on Telegram, free and with no phone number
    PUBLIC_URL                         this server's address, for the Telegram webhook (Render sets RENDER_EXTERNAL_URL itself)
"""
import os, re, threading, hashlib
from pathlib import Path
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import engine as E
import live

ROOT = Path(__file__).parent
_sec = E.local_secrets()
def cfg(k, default=""):
    return os.getenv(k) or _sec.get(k, default)

E.configure(key=cfg("GROQ_API_KEY"), model=cfg("GROQ_MODEL"), whisper=cfg("GROQ_WHISPER"))
WA_TOKEN, WA_PHONE_ID = cfg("WHATSAPP_TOKEN"), cfg("WHATSAPP_PHONE_ID")
WA_VERIFY = cfg("WHATSAPP_VERIFY_TOKEN", "bhaada_verify_123")
WA_READY = len(WA_TOKEN) > 40 and WA_PHONE_ID.isdigit()        # placeholders like "later" mean not connected
GRAPH = "https://graph.facebook.com/v21.0"
TG_TOKEN = cfg("TELEGRAM_BOT_TOKEN").strip()
TG_READY = bool(re.fullmatch(r"\d+:[\w-]{30,}", TG_TOKEN))
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_PATH = hashlib.sha256(TG_TOKEN.encode()).hexdigest()[:24]   # unguessable webhook path, derived from the token
TG_BOT = {"username": None}

# ---------------- WhatsApp sending ----------------
def wa_send(to, text, buttons=None):
    if buttons:
        payload = {"to": to, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": text[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]}}}
    else:
        payload = {"to": to, "type": "text", "text": {"body": text[:4000]}}
    try:
        r = requests.post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"},
                          json={"messaging_product": "whatsapp", **payload}, timeout=20)
        if not r.ok:
            print("WhatsApp send failed:", r.status_code, r.text[:300])
    except Exception as e:
        print("WhatsApp send error:", repr(e))

def wa_media(media_id):
    h = {"Authorization": f"Bearer {WA_TOKEN}"}
    meta = requests.get(f"{GRAPH}/{media_id}", headers=h, timeout=20).json()
    data = requests.get(meta["url"], headers=h, timeout=60)
    data.raise_for_status()
    return data.content, meta.get("mime_type", "audio/ogg")

# ---------------- Telegram sending (chat ids are kept as "tg:<id>" so they never clash with phone numbers) ----------------
def tg_send(to, text, buttons=None):
    body = {"chat_id": to[3:], "text": text[:4000]}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": [[{"text": t[:30], "callback_data": i[:64]} for i, t in buttons[:3]]]}
    try:
        r = requests.post(f"{TG_API}/sendMessage", json=body, timeout=20)
        if not r.ok:
            print("Telegram send failed:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram send error:", repr(e))

def send(to, text, buttons=None):
    """One sender for the desk: picks the channel from the address."""
    if str(to).startswith("tg:"):
        text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text.replace("*", ""))   # WhatsApp *bold*/_italic_ shows literally on Telegram
        return tg_send(to, text, buttons) if TG_READY else None
    return wa_send(to, text, buttons) if WA_READY else None

def tg_setup():
    base = (cfg("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    try:
        TG_BOT["username"] = requests.get(f"{TG_API}/getMe", timeout=15).json()["result"]["username"]
        if base:
            r = requests.post(f"{TG_API}/setWebhook", json={"url": f"{base}/telegram/{TG_PATH}",
                              "allowed_updates": ["message", "callback_query"]}, timeout=15)
            print("Telegram webhook:", r.json().get("description"))
        else:
            print("Telegram: set PUBLIC_URL so the bot can reach this server")
    except Exception as e:
        print("Telegram setup failed:", repr(e))

desk = live.Desk(sender=send if (WA_READY or TG_READY) else None)
if TG_READY:
    threading.Thread(target=tg_setup, daemon=True).start()
if desk.settings["mode"] == "sandbox":
    desk.sandbox_on()

app = FastAPI(title="Bhaada freight desk")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.get("/")
def console():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/state")
def state():
    v = desk.view()
    v["whatsapp"] = WA_READY
    v["telegram"] = TG_BOT["username"] if TG_READY else None
    v["build"] = (os.getenv("RENDER_GIT_COMMIT") or "local")[:7]
    v["llm"] = E.llm_ready()
    return v

# ---------------- console intake: a short chat that turns text into a load ----------------
@app.post("/api/intake")
async def intake(request: Request):
    body = await request.json()
    hist = [m for m in body.get("chat", []) if m.get("role") in ("user", "bot")][-12:]
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    if not E.llm_ready():
        return {"reply": "The language model isn't configured (GROQ_API_KEY), so I can't read free text yet.", "missing": ["all"]}
    hist.append({"role": "user", "text": text})
    try:
        merged, reply, miss = E.intake_turn(hist, body.get("load") or {}, text)
    except Exception as e:
        return {"reply": f"I couldn't read that ({str(e)[:80]}). Try again.", "missing": ["all"], "load": body.get("load") or {}}
    clean = {k: v for k, v in merged.items() if v not in (None, "")}
    out = {"reply": reply, "missing": miss, "load": clean}
    if not miss:
        raw = " / ".join(m["text"] for m in hist if m["role"] == "user")
        out["loadId"] = desk.start_load(merged, "console", raw=raw)["id"]
    return out

@app.post("/api/loads/{lid}/{action}")
def load_action(lid: str, action: str):
    if action not in ("approve", "reject", "backout"):
        raise HTTPException(404)
    desk.post(action, lid=lid)
    return {"ok": True}

@app.post("/api/mode")
async def set_mode(request: Request):
    mode = (await request.json()).get("mode")
    if mode not in ("live", "sandbox"):
        raise HTTPException(400)
    desk.settings["mode"] = mode
    if mode == "sandbox":
        desk.sandbox_on()
    desk.save()
    return {"mode": mode}

@app.post("/api/settings")
async def set_settings(request: Request):
    b = await request.json()
    s = desk.settings
    for k in ("dieselPerLitre", "floorPct", "targetPct", "ceilingPct", "maxDeadheadKm"):
        if k in b:
            s[k] = float(b[k])
    if "dieselSource" in b:
        s["dieselSource"] = str(b["dieselSource"])[:120]
    if "dieselAsOf" in b:
        s["dieselAsOf"] = str(b["dieselAsOf"])[:20]
    if not (0 < s["floorPct"] <= s["targetPct"] <= s["ceilingPct"] < 50):
        raise HTTPException(400, "Need 0 < floor ≤ target ≤ ceiling < 50")
    desk._apply_settings()
    desk.save()
    return s

@app.post("/api/owners")
async def add_owner(request: Request):
    b = await request.json()
    phone = re.sub(r"\D", "", str(b.get("phone", "")))
    if len(phone) == 10:
        phone = "91" + phone
    if len(phone) < 11 or not (b.get("name") or "").strip():
        raise HTTPException(400, "Name and a WhatsApp number with country code are needed")
    o = desk.register_owner(phone, b["name"], b.get("city"))
    if WA_READY:
        wa_send(phone, f"Namaste {o['name']} 🙏 Aap Bhaada freight desk se jud gaye hain. Roz subah hum poochenge "
                       "ki gaadi khaali hai ya nahi; loads seedhe WhatsApp par aayenge.")
    return {"ok": True}

@app.post("/api/checkin")
def checkin():
    if desk.settings["mode"] == "sandbox":
        desk.sandbox_on()
        return {"asked": len([o for o in desk.owners.values() if o.get("sandbox")]), "mode": "sandbox"}
    return {"asked": desk.send_checkin(), "mode": "live"}

# ---------------- WhatsApp webhook ----------------
SEEN, LOCKS = set(), {}

@app.get("/webhook")
def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == WA_VERIFY:
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("verification failed", status_code=403)

@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    body = await request.json()
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("id") in SEEN:
                    continue
                SEEN.add(msg.get("id"))
                bg.add_task(handle_message, msg)
    return {"ok": True}

@app.post("/telegram/{path}")
async def telegram(path: str, request: Request, bg: BackgroundTasks):
    if not TG_READY or path != TG_PATH:
        raise HTTPException(404)
    u = await request.json()
    if u.get("update_id") in SEEN:
        return {"ok": True}
    SEEN.add(u.get("update_id"))
    bg.add_task(handle_telegram, u)
    return {"ok": True}

def handle_telegram(u):
    if "callback_query" in u:
        q = u["callback_query"]
        try:
            requests.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": q["id"]}, timeout=10)
        except Exception:
            pass                                         # only stops the button's spinner; the action still runs
        return on_button(f"tg:{q['message']['chat']['id']}", q.get("data", ""))
    m = u.get("message") or {}
    if not m.get("chat"):
        return
    key = f"tg:{m['chat']['id']}"
    with LOCKS.setdefault(key, threading.Lock()):
        try:
            media = m.get("voice") or m.get("audio") or m.get("video_note")
            if media:
                f = requests.get(f"{TG_API}/getFile", params={"file_id": media["file_id"]}, timeout=15).json()["result"]
                audio = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{f['file_path']}", timeout=60).content
                text = E.transcribe(audio, media.get("mime_type") or "audio/ogg", name=f.get("file_path"))
                print(f"voice {key[-4:]}: {media.get('mime_type')} {f.get('file_path')} {len(audio)} bytes "
                      f"{media.get('duration')}s -> {text[:60]!r}")
                if not text:
                    return send(key, "🎤 Awaaz saaf nahi aayi. Phone muh ke paas rakh ke dobara bolo, ya type kar do.")
                send(key, f"🎤 Heard: {text}")
            elif m.get("text"):
                text = m["text"].strip()
                if text.startswith("/start"):
                    text = "hi"
            else:
                return send(key, "I read text and voice notes for now. Please type or record it.")
            on_text(key, text)
        except Exception as e:
            print("telegram message failed:", repr(e))
            send(key, "Something went wrong on my side. Send new to start again.")

def handle_message(msg):
    wa = msg["from"]
    with LOCKS.setdefault(wa, threading.Lock()):
        try:
            kind = msg.get("type")
            if kind == "interactive":
                return on_button(wa, msg["interactive"]["button_reply"]["id"])
            if kind == "button":
                return on_button(wa, msg["button"].get("payload", ""))
            if kind == "audio":
                audio, mime = wa_media(msg["audio"]["id"])
                text = E.transcribe(audio, mime)
                if not text:
                    return send(wa, "🎤 Awaaz saaf nahi aayi. Phone muh ke paas rakh ke dobara bolo, ya type kar do.")
                send(wa, f"🎤 Heard: _{text}_")
            elif kind == "text":
                text = msg["text"]["body"].strip()
            else:
                return send(wa, "I read text and voice notes for now. Please type or record it.")
            on_text(wa, text)
        except Exception as e:
            print("message failed:", repr(e))
            send(wa, "Something went wrong on my side. Send *new* to start again.")

REGISTER = re.compile(r"^\s*(owner|register|transporter)\b[\s:,-]*(.*)$", re.I)
AS_BROKER = re.compile(r"^\s*(broker|i am (a )?broker|main broker hoon)\s*$", re.I)

def on_text(wa, text):
    m = REGISTER.match(text)
    if m:
        parts = [p.strip() for p in re.split(r",|\bfrom\b|\bse\b", m.group(2)) if p.strip()]
        if not parts:
            return send(wa, "Aise likhiye: *owner Sharma Transport, Bhiwandi*")
        o = desk.register_owner(wa, parts[0], parts[1] if len(parts) > 1 else None)
        if desk.settings["mode"] != "live":
            desk.settings["mode"] = "live"
            desk.save()
        return send(wa, f"Namaste {o['name']} 🙏 Registered as a fleet owner"
                           + (f" in {o['baseCity']}" if o.get("baseCity") else "") + ".\nAaj ki gaadi bhejiye: "
                           "*32 ft MXL, Bhiwandi, aaj, ₹38/km*\n(Load bhejna hai? *broker* likhiye.)")
    if AS_BROKER.match(text):
        was = desk.remove_owner(wa) if wa in desk.owners and not desk.owners[wa].get("sandbox") else None
        desk.brokers[wa] = {"chat": [], "load": {}}
        return send(wa, ("Owner list se hata diya. " if was else "") + "Ab aap broker hain 👍 Load bhejiye (text ya voice note).\n"
                        "e.g. _Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL_")
    if wa in desk.owners and not desk.owners[wa].get("sandbox"):
        return desk.post("owner_message", key=wa, text=text)
    # anyone else is a broker
    s = desk.brokers.setdefault(wa, {"chat": [], "load": {}})
    if text.lower() in ("hi", "hello", "new", "reset", "namaste", "start"):
        desk.brokers[wa] = {"chat": [], "load": {}}
        return send(wa, "Namaste 🙏 Bhaada desk. Send a load as text or a voice note.\n"
                           "e.g. _Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL_\n"
                           "Truck owner ho? *owner Naam, Shehar* likhiye.")
    if not E.llm_ready():
        return send(wa, "My language model isn't configured yet.")
    s["chat"].append({"role": "user", "text": text})
    merged, reply, miss = E.intake_turn(s["chat"], s["load"], text)
    s["load"] = {k: v for k, v in merged.items() if v not in (None, "")}
    s["chat"].append({"role": "bot", "text": reply})
    if miss:
        return send(wa, reply)
    load = desk.start_load(merged, "telegram" if wa.startswith("tg:") else "whatsapp", broker_phone=wa,
                           raw=" / ".join(m["text"] for m in s["chat"] if m["role"] == "user"))
    desk.brokers[wa] = {"chat": [], "load": {}}
    send(wa, f"🔍 {load['id']}: got it. Messaging owners near {merged['origin']} now…")

def on_button(wa, bid):
    action, _, lid = bid.partition(":")
    if action in ("approve", "reject") and lid:
        desk.post(action, lid=lid)
        return send(wa, "✅ Approved. Asking the owner to hold the truck…" if action == "approve" else "Rejected. Nothing booked.")
    send(wa, "That button has expired. Send *new* to start again.")
