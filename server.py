"""
Bhaada server: the broker's console (static/), its JSON API, and the WhatsApp Cloud API webhook, on one live desk.

Run:   uvicorn server:app --port 8000          then open http://localhost:8000
Config (environment variables, or .streamlit/secrets.toml locally):
    GROQ_API_KEY                       language model (intake, negotiation, check-in parsing)
    WHATSAPP_TOKEN, WHATSAPP_PHONE_ID  Meta WhatsApp Cloud API (leave empty to run the console alone)
    WHATSAPP_VERIFY_TOKEN              any string; the same one goes into Meta's webhook settings
    WHATSAPP_APP_SECRET                Meta app secret; when set, webhook POSTs without a valid signature are refused
    CONSOLE_PASSWORD                   when set, the console and its API ask for it (leave empty for an open demo)
    TELEGRAM_BOT_TOKEN                 a bot from @BotFather: the same desk on Telegram, free and with no phone number
    PUBLIC_URL                         this server's address, for the Telegram webhook (Render sets RENDER_EXTERNAL_URL itself)
"""
import os, re, json, threading, hashlib, hmac, collections
from pathlib import Path
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Depends
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
def keep_awake():
    """Render's free plan sleeps after 15 idle minutes and the desk's timers stop with it: knock every 10 minutes."""
    base = (cfg("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    while base:
        threading.Event().wait(600)
        try:
            requests.get(f"{base}/healthz", timeout=20)
        except Exception:
            pass

threading.Thread(target=keep_awake, daemon=True).start()
if desk.settings["mode"] == "sandbox" or desk.settings.get("aiAgents"):
    desk.sandbox_on()                                   # the persona owners: sandbox mode, and AI owners in live mode

app = FastAPI(title="Bhaada freight desk")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

CONSOLE_PW = cfg("CONSOLE_PASSWORD").strip()        # empty: the console is open (sandbox demos)

def console_ok(request: Request):
    got = request.headers.get("x-console-key", "")
    return not CONSOLE_PW or hmac.compare_digest(got.encode(), CONSOLE_PW.encode())

def console_auth(request: Request):
    if not console_ok(request):
        raise HTTPException(401, "Console password needed")

@app.get("/")
def console():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/api/state")
def state(request: Request):
    if not console_ok(request):
        return {"locked": True}                    # 200, so a health check on this path keeps passing
    v = desk.view()
    v["whatsapp"] = WA_READY
    v["telegram"] = TG_BOT["username"] if TG_READY else None
    v["build"] = (os.getenv("RENDER_GIT_COMMIT") or "local")[:7]
    v["memory"] = "upstash" if live._store() else "local file (lost on restart)"
    v["llm"] = E.llm_ready()
    return v

# ---------------- console intake: a short chat that turns text into a load ----------------
@app.post("/api/intake", dependencies=[Depends(console_auth)])
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

@app.post("/api/loads/{lid}/{action}", dependencies=[Depends(console_auth)])
def load_action(lid: str, action: str):
    if action not in ("approve", "reject", "backout"):
        raise HTTPException(404)
    desk.post(action, lid=lid)
    return {"ok": True}

@app.post("/api/mode", dependencies=[Depends(console_auth)])
async def set_mode(request: Request):
    mode = (await request.json()).get("mode")
    if mode not in ("live", "sandbox"):
        raise HTTPException(400)
    desk.settings["mode"] = mode
    if mode == "sandbox":
        desk.sandbox_on()
    desk.post("save")
    return {"mode": mode}

@app.post("/api/settings", dependencies=[Depends(console_auth)])
async def set_settings(request: Request):
    b = await request.json()
    s = dict(desk.settings)                        # checked as a whole before any of it goes live
    try:
        for k in ("dieselPerLitre", "floorPct", "targetPct", "ceilingPct", "maxDeadheadKm"):
            if k in b:
                s[k] = float(b[k])
    except (TypeError, ValueError):
        raise HTTPException(400, "Numbers only for diesel, margins and deadhead")
    if "aiAgents" in b:
        s["aiAgents"] = bool(b["aiAgents"])
    if "dieselSource" in b:
        s["dieselSource"] = str(b["dieselSource"])[:120]
    if "dieselAsOf" in b:
        s["dieselAsOf"] = str(b["dieselAsOf"])[:20]
    if not (0 < s["floorPct"] <= s["targetPct"] <= s["ceilingPct"] < 50):
        raise HTTPException(400, "Need 0 < floor ≤ target ≤ ceiling < 50")
    if not (0 < s["dieselPerLitre"] < 500 and 0 < s["maxDeadheadKm"] <= 1000):
        raise HTTPException(400, "Diesel must be ₹/L and deadhead a distance in km")
    desk.settings.update(s)
    s = desk.settings
    desk._apply_settings()
    desk.post("save")
    return s

@app.post("/api/owners", dependencies=[Depends(console_auth)])
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

@app.post("/api/checkin", dependencies=[Depends(console_auth)])
def checkin():
    if desk.settings["mode"] == "sandbox":
        desk.sandbox_on()
        return {"asked": len([o for o in desk.owners.values() if o.get("sandbox")]), "mode": "sandbox"}
    return {"asked": desk.send_checkin(), "mode": "live"}

# ---------------- WhatsApp webhook ----------------
SEEN, SEEN_ORDER, LOCKS = set(), collections.deque(), {}
WA_SECRET = cfg("WHATSAPP_APP_SECRET").strip()      # Meta app secret: proves a webhook POST came from Meta
if WA_READY and not WA_SECRET:
    print("WARNING: WHATSAPP_APP_SECRET is not set, so WhatsApp webhook POSTs are not verified")

def first_time(mid):
    """Meta and Telegram retry deliveries; remember the last few thousand ids, not every id forever."""
    if mid in SEEN:
        return False
    SEEN.add(mid)
    SEEN_ORDER.append(mid)
    if len(SEEN_ORDER) > 5000:
        SEEN.discard(SEEN_ORDER.popleft())
    return True

@app.get("/webhook")
def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == WA_VERIFY:
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("verification failed", status_code=403)

@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if WA_SECRET:
        good = "sha256=" + hmac.new(WA_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-hub-signature-256", "").encode(), good.encode()):
            raise HTTPException(403, "bad signature")
    body = json.loads(raw or b"{}")
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if first_time("wa:" + str(msg.get("id"))):
                    bg.add_task(handle_message, msg)
    return {"ok": True}

@app.post("/telegram/{path}")
async def telegram(path: str, request: Request, bg: BackgroundTasks):
    if not TG_READY or path != TG_PATH:
        raise HTTPException(404)
    u = await request.json()
    if first_time("tg:" + str(u.get("update_id"))):
        bg.add_task(handle_telegram, u)
    return {"ok": True}

ONLY_TEXT = "Abhi sirf text aur voice note samajh pata hoon 🙏 Type kijiye ya bol ke bhejiye."
OOPS = "Maaf kijiye, mere taraf se kuch gadbad hui 🙏 Thodi der baad wahi message dobara bhejiye."

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
                print(f"voice {key[-4:]}: {media.get('mime_type')} {len(audio)} bytes {media.get('duration')}s -> {len(text)} chars")
                if not text:
                    return send(key, "🎤 Awaaz saaf nahi aayi. Phone muh ke paas rakh ke dobara bolo, ya type kar do.")
                send(key, f"🎤 Suna: {text}")
            elif m.get("text"):
                text = m["text"].strip()
                if text.startswith("/start"):
                    text = "hi"
            else:
                return send(key, ONLY_TEXT)
            on_text(key, text)
        except Exception as e:
            print("telegram message failed:", repr(e))
            send(key, OOPS)

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
                send(wa, f"🎤 Suna: _{text}_")
            elif kind == "text":
                text = msg["text"]["body"].strip()
            else:
                return send(wa, ONLY_TEXT)
            on_text(wa, text)
        except Exception as e:
            print("message failed:", repr(e))
            send(wa, OOPS)

REGISTER = re.compile(r"^\s*(owner|register|transporter)\b[\s:,-]*(.*)$", re.I)
NOT_A_NAME = re.compile(r"\?|\d+\s*(ton|tonne|t|mt|kg)\b|chahiye|chaiye|chahie|chiye|\bkya\b|\bkab\b|\bne\b|\bka\b|\bko\b", re.I)
AS_BROKER = re.compile(r"^\s*(broker|i am (a )?broker|main broker hoon)\s*$", re.I)
APPROVE = re.compile(r"^\s*(?:(approved?|haan|haa|ha|yes|ok|okay|done|book|confirm|pakka|theek hai|thik hai|chalega)\b|👍|✅)", re.I)
REJECT = re.compile(r"^\s*(?:(reject(ed)?|nahi|nahin|nhi|no|mat|rehne do|cancel)\b|❌|👎)", re.I)
STATUS_Q = re.compile(r"\b(status|update|kya hua|kya scene|kahan tak pahunch|progress)\b", re.I)
LOAD_ID = re.compile(r"\bBH[-\s]?(\d{4,})\b", re.I)

def on_text(wa, text):
    m = REGISTER.match(text)
    # "owner ne cancel kiya?" or "transporter chahiye Surat se 14 ton" is a broker talking, not a registration
    if m and not NOT_A_NAME.search(m.group(2)) and ("," in m.group(2) or not desk.broker_loads(wa)):
        parts = [p.strip() for p in re.split(r",|\bfrom\b|\bse\b", m.group(2)) if p.strip()]
        if not parts:
            return send(wa, "Aise likhiye: *owner Sharma Transport, Bhiwandi*")
        o = desk.register_owner(wa, parts[0], parts[1] if len(parts) > 1 else None)
        if desk.settings["mode"] != "live":
            desk.settings["mode"] = "live"
            desk.post("save")
        return send(wa, f"Namaste {o['name']} 🙏 Aap fleet owner ke roop mein jud gaye"
                           + (f" ({o['baseCity']})" if o.get("baseCity") else "") + ".\nAaj ki gaadi bhejiye: "
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
        return send(wa, "Namaste 🙏 Bhaada desk. Load text ya voice note mein bhejiye.\n"
                           "e.g. _Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL_\n"
                           "Truck owner ho? *owner Naam, Shehar* likhiye.")
    if broker_command(wa, text):
        return
    if not E.llm_ready():
        return send(wa, "Desk abhi load padh nahi sakta (language model band hai) 🙏 Thodi der baad bhejiye.")
    intent, said = broker_brain(wa, text, s)
    if intent == "reply":
        return send(wa, said)
    s["chat"].append({"role": "user", "text": text})
    try:
        merged, reply, miss = E.intake_turn(s["chat"], s["load"], text)
    except Exception as e:                                 # Groq busy (429), timeout or a reply that isn't JSON
        print("intake failed:", repr(e))
        s["chat"].pop()
        return send(wa, "Abhi system thoda busy hai 🙏 1 minute baad wahi message dobara bhejiye.")
    s["load"] = {k: v for k, v in merged.items() if v not in (None, "")}
    s["chat"].append({"role": "bot", "text": reply})
    if miss:
        desk.post("save")                                    # a half-told load survives a restart
        return send(wa, reply)
    load = desk.start_load(merged, "telegram" if wa.startswith("tg:") else "whatsapp", broker_phone=wa,
                           raw=" / ".join(m["text"] for m in s["chat"] if m["role"] == "user"))
    desk.brokers[wa] = {"chat": [], "load": {}}
    send(wa, f"🔍 {load['id']}: {merged['origin']} ke paas ke truck owners ko message kar raha hoon…\n"
                f"({load['id']} is load ka number hai; isi se updates aayenge. Haal jaanna ho to *status* likhiye.)")

def broker_facts(wa):
    """The broker's recent loads as plain lines, newest last (read-only, safe on the server thread)."""
    for _ in range(3):
        try:
            return [desk.broker_status(l)[0].replace("\n", " · ") for l in desk.broker_loads(wa)[-5:]]
        except RuntimeError:                               # the worker changed a load mid-read
            continue
    return []

def broker_brain(wa, text, s):
    """What does the broker mean? ("load", None): load details or a truck request, for intake.
    ("reply", text): thanks, small talk or a question, answered from their own loads. Falls back to intake."""
    loads = broker_facts(wa)
    asked = next((m["text"] for m in reversed(s["chat"]) if m["role"] == "bot"), None)
    try:
        out = E.parse_json(E.gen(
            "You are Bhaada, an AI freight desk chatting with a BROKER (they send loads; you find trucks and quote).\n"
            "Their recent loads (newest last):\n" + ("\n".join("- " + x for x in loads) or "- none") + "\n"
            + (f"A new load is being collected: known so far {json.dumps(s['load'], ensure_ascii=False)}; "
               f"your last question was: \"{asked}\"\n" if s["chat"] else "")
            + f'Broker\'s message: "{text}"\n'
            "Decide:\n"
            "- intent=\"load\" if the message gives or changes load details (cities, weight, truck, pickup day, budget), asks "
            "for a truck, or answers your last question. Leave reply empty.\n"
            "- intent=\"reply\" for everything else: thanks, greetings, ok, small talk, questions about their loads, owners, "
            "prices or how the desk works. Answer in 1-3 short lines using ONLY the facts above; never invent a load, "
            "truck, owner or price. Thanks/ok gets a short warm reply (e.g. \"Shukriya ji 🙏 Aur koi load ho to bhejiye.\") "
            "and never a question about tons or cities. If a load is being collected, you may gently remind what is "
            "still needed. To cancel a quote they write *reject BH-xxxx*; a booked trip is changed by calling the owner.\n"
            "Reply in Hinglish (Hindi words in Roman script), never plain English.\n"
            'Return ONLY JSON {"intent":"load|reply","reply":""}', json_mode=True))
    except Exception as e:
        print("broker brain failed, reading it as a load:", repr(e))
        return "load", None
    reply = (out.get("reply") or "").strip()
    return ("reply", reply[:700]) if out.get("intent") == "reply" and reply else ("load", None)

def broker_command(wa, text):
    """A reply to a quote typed instead of tapped, or a question about loads already sent. True if handled."""
    mine = desk.broker_loads(wa)
    hit = LOAD_ID.search(text)
    named = next((l for l in mine if l["id"] == f"BH-{hit.group(1)}"), None) if hit else None
    short = len(text.split()) <= 5
    mid_intake = bool(desk.brokers.get(wa, {}).get("chat"))   # then a bare "haan"/"nahi" answers the intake question
    yes_no = APPROVE.match(text) or (REJECT.match(text) and not re.search(r"\d", LOAD_ID.sub("", text)))
    if short and yes_no and (named or not mid_intake):
        pending = [l for l in mine if l["status"] == "needs_approval"]
        target = named or (pending[0] if len(pending) == 1 else None)
        if not target and len(pending) > 1:
            send(wa, "Kaunsa load? " + ", ".join(l["id"] for l in pending) + ". Quote ke neeche button dabaiye ya "
                     f"*approve {pending[0]['id']}* likhiye.")
            return True
        if target:
            act = "approve" if APPROVE.match(text) else "reject"
            on_button(wa, f"{act}:{target['id']}")
            return True
    if STATUS_Q.search(text) or (named and short):
        if not mine:
            send(wa, "Aapka koi load abhi chal nahi raha. Naya load bhejiye, e.g. _Kal Bhiwandi se Surat, 18 ton, 32 MXL_")
            return True
        recent = [l for l in mine if l["status"] not in live.Desk.CLOSED or live.now() - l["createdAt"] < 12 * 3600]
        for l in ([named] if named else (recent or mine)[-3:]):
            send(wa, *desk.broker_status(l))
        return True
    return False

def on_button(wa, bid):
    action, _, lid = bid.partition(":")
    load = desk._find(lid) if lid else None
    if action in ("approve", "reject") and load and load.get("brokerPhone") == wa:
        if load["status"] != "needs_approval":
            text, buttons = desk.broker_status(load)
            return send(wa, "Ye quote ab khula nahi hai. " + text, buttons)
        desk.post(action, lid=lid)
        return send(wa, "✅ Approve ho gaya. Owner se gaadi hold karwa raha hoon…" if action == "approve" else "Reject kar diya. Kuch book nahi hua.")
    send(wa, "Ye button ab kaam ka nahi hai. Load ka haal jaanne ke liye *status* likhiye, ya naya load seedha bhejiye.")
