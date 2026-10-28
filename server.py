"""
Bhaada server: the broker's console (static/), its JSON API, and the WhatsApp Cloud API webhook, on one live desk.

Run:   uvicorn server:app --port 8000          then open http://localhost:8000
Config (environment variables, or .streamlit/secrets.toml locally):
    GROQ_API_KEY                       language model (intake, negotiation, check-in parsing)
    WHATSAPP_TOKEN, WHATSAPP_PHONE_ID  Meta WhatsApp Cloud API (leave empty to run the console alone)
    WHATSAPP_VERIFY_TOKEN              any string; the same one goes into Meta's webhook settings
    WHATSAPP_APP_SECRET                Meta app secret; when set, webhook POSTs without a valid signature are refused
    CONSOLE_PASSWORD                   when set, the console and its API ask for it. Unset on a host (Render), the
                                       console is view-only unless CONSOLE_OPEN=1; on a laptop it stays open
    PUBLIC_VIEW                        1: with a password set, anyone may watch the desk; acting still needs the password
    TELEGRAM_BOT_TOKEN                 a bot from @BotFather: the same desk on Telegram, free and with no phone number
    PUBLIC_URL                         this server's address, for the Telegram webhook (Render sets RENDER_EXTERNAL_URL itself)
"""
import os, re, json, threading, hashlib, hmac, collections, queue, time
from pathlib import Path
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import engine as E
import live

ROOT = Path(__file__).parent
_sec = E.local_secrets()
def cfg(k, default=""):
    return (os.getenv(k) or _sec.get(k, default)).strip().strip('"').strip("'")   # a pasted key often brings quotes

E.configure(key=cfg("GROQ_API_KEY"), model=cfg("GROQ_MODEL"), whisper=cfg("GROQ_WHISPER"),
            openai_key=cfg("OPENAI_API_KEY"), openai_model=cfg("OPENAI_MODEL"))
WA_TOKEN, WA_PHONE_ID = cfg("WHATSAPP_TOKEN"), cfg("WHATSAPP_PHONE_ID")
WA_VERIFY = cfg("WHATSAPP_VERIFY_TOKEN", "bhaada_verify_123")
WA_READY = len(WA_TOKEN) > 40 and WA_PHONE_ID.isdigit()        # placeholders like "later" mean not connected
GRAPH = "https://graph.facebook.com/v21.0"
TG_TOKEN = cfg("TELEGRAM_BOT_TOKEN").strip()
TG_READY = bool(re.fullmatch(r"\d+:[\w-]{30,}", TG_TOKEN))
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_PATH = hashlib.sha256(TG_TOKEN.encode()).hexdigest()[:24]   # unguessable webhook path, derived from the token
TG_SECRET = hashlib.sha256(("secret:" + TG_TOKEN).encode()).hexdigest()[:48]   # sent back by Telegram in a header
TG_BOT = {"username": None, "hooked": False}

def safe(e):
    """An error for the logs: requests puts the URL in its errors, and Telegram's URLs carry the bot token."""
    s = repr(e)
    return s.replace(TG_TOKEN, "<bot-token>") if TG_TOKEN else s

MAX_TEXT = 1000                                         # a load fits in a few lines; longer text is cut, not sent to a model

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

MAX_VOICE_SEC, MAX_VOICE_BYTES = 120, 4_000_000        # a load is said in under a minute; Whisper minutes are quota
META_MEDIA_HOSTS = (".fbsbx.com", ".facebook.com", ".fbcdn.net", ".whatsapp.net")

def fetch_capped(url, headers=None, cap=MAX_VOICE_BYTES):
    """Download at most cap bytes; a bigger file is refused part-way instead of filling memory."""
    with requests.get(url, headers=headers, timeout=60, stream=True) as r:
        r.raise_for_status()
        out = bytearray()
        for chunk in r.iter_content(65536):
            out += chunk
            if len(out) > cap:
                raise ValueError("voice note too large")
        return bytes(out)

def wa_media(media_id):
    """(bytes, mime), or None when the note is too big. The token only goes to Meta's own media hosts."""
    h = {"Authorization": f"Bearer {WA_TOKEN}"}
    meta = requests.get(f"{GRAPH}/{media_id}", headers=h, timeout=20).json()
    if int(meta.get("file_size") or 0) > MAX_VOICE_BYTES:
        return None
    url = urlparse(meta["url"])
    if url.scheme != "https" or not (url.hostname or "").endswith(META_MEDIA_HOSTS):
        raise ValueError(f"unexpected media host {url.hostname}")
    return fetch_capped(meta["url"], headers=h), meta.get("mime_type", "audio/ogg")

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
        print("Telegram send error:", safe(e))

CONVO = {}                                            # address -> the last few lines both ways, so replies follow the chat

def remember(who, role, text):
    log = CONVO.setdefault(who, collections.deque(maxlen=12))
    log.append(f"{role}: {str(text)[:300]}")

def send(to, text, buttons=None):
    """One sender for the desk: picks the channel from the address."""
    remember(to, "Bhaada", text)
    if str(to).startswith("tg:"):
        text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text.replace("*", ""))   # WhatsApp *bold*/_italic_ shows literally on Telegram
        return tg_send(to, text, buttons) if TG_READY else None
    return wa_send(to, text, buttons) if WA_READY else None

def tg_setup():
    """Point the bot at this server, with a secret header; retried, since a cold boot often meets a slow network."""
    base = (cfg("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    for wait in (0, 5, 20, 60):
        threading.Event().wait(wait)
        try:
            TG_BOT["username"] = requests.get(f"{TG_API}/getMe", timeout=15).json()["result"]["username"]
            if not base:
                return print("Telegram: set PUBLIC_URL so the bot can reach this server")
            r = requests.post(f"{TG_API}/setWebhook", json={"url": f"{base}/telegram/{TG_PATH}", "secret_token": TG_SECRET,
                              "allowed_updates": ["message", "callback_query"]}, timeout=15).json()
            print("Telegram webhook:", r.get("description"))
            if r.get("ok"):
                TG_BOT["hooked"] = True
                return
        except Exception as e:
            print("Telegram setup failed:", safe(e))

OUTBOX = queue.Queue()

def outbox():
    """The desk's messages go out from here, in order, so a slow Telegram or Meta call never holds up its worker."""
    while True:
        to, text, buttons = OUTBOX.get()
        try:
            send(to, text, buttons)
        except Exception as e:
            print("send failed:", safe(e))

def desk_send(to, text, buttons=None):
    OUTBOX.put((to, text, buttons))

threading.Thread(target=outbox, daemon=True).start()
desk = live.Desk(sender=desk_send if (WA_READY or TG_READY) else None)
desk.convo = CONVO
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

app = FastAPI(title="Bhaada freight desk", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
HEADERS = [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
           (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"),
           (b"strict-transport-security", b"max-age=31536000"),
           (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
MAX_BODY = 1_000_000                                   # webhooks and console posts are a few KB

class Guard:
    """Security headers on every response (no framing, no sniffing, scripts only from this site; the console's
    text never becomes HTML anyway) and a cap on request bodies, which are read whole before a signature check."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = dict(scope.get("headers") or []).get(b"content-length", b"0")
        if not size.isdigit() or int(size) > MAX_BODY:
            return await PlainTextResponse("request too large", status_code=413)(scope, receive, send)
        seen = [0]
        async def capped():
            msg = await receive()
            seen[0] += len(msg.get("body", b""))
            if seen[0] > MAX_BODY:                       # chunked, with no length up front
                raise HTTPException(413)
            return msg
        api = scope["path"].startswith("/api/")
        async def with_headers(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] = list(msg.get("headers") or []) + HEADERS + ([(b"cache-control", b"no-store")] if api else [])
            await send(msg)
        await self.app(scope, capped, with_headers)

app.add_middleware(Guard)

CONSOLE_PW = cfg("CONSOLE_PASSWORD").strip()        # set: reading and acting need it (unless PUBLIC_VIEW)
YES_WORDS = ("1", "true", "yes", "on")
PUBLIC_VIEW = cfg("PUBLIC_VIEW").lower() in YES_WORDS     # with a password: anyone may watch, only the password acts
CONSOLE_OPEN = cfg("CONSOLE_OPEN").lower() in YES_WORDS   # no password on a host: acting needs this explicit opt-in
ON_HOST = bool(os.getenv("RENDER"))                        # on a laptop an open console is fine; on the internet it isn't
if not CONSOLE_PW and ON_HOST:
    print("WARNING: CONSOLE_PASSWORD is not set: anyone with the URL can read every chat"
          + (" and approve or change settings (CONSOLE_OPEN)" if CONSOLE_OPEN else "; the console is read-only"))

def console_ok(request: Request):
    """May read the whole desk."""
    got = request.headers.get("x-console-key", "")
    return not CONSOLE_PW or hmac.compare_digest(got.encode(), CONSOLE_PW.encode())

def console_can_act(request: Request):
    """May approve, reject, change settings, add owners or spend model calls on intake."""
    if CONSOLE_PW:
        return console_ok(request)
    return CONSOLE_OPEN or not ON_HOST

def console_auth(request: Request):
    if not console_can_act(request):
        if CONSOLE_PW:
            raise HTTPException(401, "Console password needed")
        raise HTTPException(403, "This public console is read-only. Set CONSOLE_PASSWORD on the server to act from it.")

@app.get("/")
def console():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/healthz")
async def healthz():
    """Liveness for Render's health check and keep_awake. async: answered even when the thread pool is full.
    503 only when the desk's worker is stuck, so a restart is the right cure; the rest is for a human to read."""
    h = desk.health()
    if h.get("storeError"):                           # public: the kind of failure, not the Upstash host in the message
        h["storeError"] = h["storeError"].split("(")[0]
    h.update(ok=h["worker"] == "ok", llm=E.llm_ready(), llmChain=[n for n, *_ in E._providers("smart")], llmCoolingSec=max(0, round(E.LLM.get("coolUntil", 0) - time.time())),
             telegram=("webhook set" if TG_BOT["hooked"] else "webhook not set") if TG_READY else None,
             outbox=OUTBOX.qsize())
    return JSONResponse(h, status_code=200 if h["ok"] else 503)

@app.on_event("shutdown")
def shutdown():
    """Render stops the old instance on every deploy: write the last few seconds of changes first."""
    desk.flush_now(wait=10)
    end = time.time() + 5
    while not OUTBOX.empty() and time.time() < end:
        time.sleep(0.1)

@app.get("/api/state")
def state(request: Request):
    if not console_ok(request) and not PUBLIC_VIEW:
        return {"locked": True}                    # 200, so a health check on this path keeps passing
    v = desk.view()
    v["readOnly"] = not console_can_act(request)
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
    hist = [{"role": m["role"], "text": str(m.get("text") or "")[:MAX_TEXT]} for m in (body.get("chat") or [])[-12:]
            if isinstance(m, dict) and m.get("role") in ("user", "bot")]
    text = str(body.get("text") or "").strip()[:MAX_TEXT]       # every character is model tokens someone pays for
    if not isinstance(body.get("load") or {}, dict):
        raise HTTPException(400, "load must be an object")
    if not text:
        raise HTTPException(400, "empty message")
    if not E.llm_ready():
        return {"reply": "The language model isn't configured (GROQ_API_KEY), so I can't read free text yet.", "missing": ["all"]}
    hist.append({"role": "user", "text": text})
    try:
        # a model call (and geocoding) off the event loop, or every webhook and poll waits for it
        merged, reply, miss = await run_in_threadpool(E.intake_turn, hist, body.get("load") or {}, text)
    except Exception as e:                       # the provider's error text stays in the log, not on the page
        print("console intake failed:", safe(e)[:200])
        return {"reply": "I couldn't read that just now. Try again in a minute.", "missing": ["all"], "load": body.get("load") or {}}
    clean = {k: v for k, v in merged.items() if v not in (None, "")}
    out = {"reply": reply, "missing": miss, "load": clean}
    if not miss:
        raw = " / ".join(m["text"] for m in hist if m["role"] == "user")
        out["loadId"] = desk.start_load(merged, "console", raw=raw)["id"]
    return out

@app.post("/api/loads/{lid}/{action}", dependencies=[Depends(console_auth)])
def load_action(lid: str, action: str):
    if action not in ("approve", "reject", "backout", "call_yes", "call_no"):
        raise HTTPException(404)
    if action.startswith("call_"):
        desk.post("broker_call", lid=lid, yes=action == "call_yes")
    else:
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
    if len(phone) > 15:
        raise HTTPException(400, "That is not a phone number")
    o = await run_in_threadpool(desk.register_owner, phone, str(b["name"])[:40], str(b.get("city") or "")[:60] or None)   # geocoding is a network call
    if WA_READY:
        desk_send(phone, f"Namaste {o['name']} 🙏 Aap Bhaada freight desk se jud gaye hain. Roz subah hum poochenge "
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
    print("WARNING: WHATSAPP_APP_SECRET is not set, so WhatsApp webhook POSTs are refused")

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
    if not WA_SECRET:                                   # unsigned, anyone could pose as any number and message real owners
        raise HTTPException(403, "WHATSAPP_APP_SECRET is not set")
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
    if not TG_READY or not hmac.compare_digest(path.encode(), TG_PATH.encode()):
        raise HTTPException(404)
    got = request.headers.get("x-telegram-bot-api-secret-token") or ""
    # every webhook we ever set carries this secret, so a real update always has it; a path seen in a log is not enough
    if not hmac.compare_digest(got.encode(), TG_SECRET.encode()):
        raise HTTPException(403)
    u = await request.json()
    if first_time("tg:" + str(u.get("update_id"))):
        bg.add_task(handle_telegram, u)
    return {"ok": True}

ONLY_TEXT = "Abhi sirf text aur voice note samajh pata hoon 🙏 Type kijiye ya bol ke bhejiye."
OOPS = "Maaf kijiye, mere taraf se kuch gadbad hui 🙏 Thodi der baad wahi message dobara bhejiye."
TOO_LONG = "🎤 Voice note 2 minute se chhota bhejiye 🙏 Ya load type kar dijiye."
SLOW_DOWN = "Bahut saare messages ek saath aa gaye 🙏 Thodi der ruk ke bhejiye."

# Every message can cost model calls on prepaid credit: one chat, or a crowd of them, can't run the desk dry
RATE = {"perMin": 12, "perHour": 120, "allPerHour": 900}
HITS, ALL_HITS, WARNED, RATE_LOCK = {}, collections.deque(), {}, threading.Lock()

def allowed(who):
    """(ok, tell_them): a sliding window per sender and for the whole desk; a refused sender hears it once in 10 min."""
    t = time.time()
    with RATE_LOCK:
        while ALL_HITS and t - ALL_HITS[0] > 3600:
            ALL_HITS.popleft()
        q = HITS.setdefault(who, collections.deque())
        while q and t - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE["perHour"] or sum(t - x < 60 for x in q) >= RATE["perMin"] or len(ALL_HITS) >= RATE["allPerHour"]:
            tell = t - WARNED.get(who, 0) > 600
            if tell:
                WARNED[who] = t
            return False, tell
        q.append(t)
        ALL_HITS.append(t)
        if len(HITS) > 5000:                               # forget senders quiet for an hour
            for k in [k for k, d in HITS.items() if not d or t - d[-1] > 3600]:
                HITS.pop(k, None)
                WARNED.pop(k, None)
        return True, False

def gate(who):
    ok, tell = allowed(who)
    if not ok:
        print("rate limited:", str(who)[-4:])
        if tell:
            send(who, SLOW_DOWN)
    return ok

def handle_telegram(u):
    if "callback_query" in u:
        q = u["callback_query"]
        chat = (q.get("message") or {}).get("chat") or {}
        # a button counts only from the person whose private chat it is in: in a group anyone could tap a broker's Approve
        if chat.get("type", "private") != "private" or (q.get("from") and q["from"].get("id") != chat.get("id")):
            return
        if not gate(f"tg:{chat.get('id')}"):
            return
        try:
            requests.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": q["id"]}, timeout=10)
        except Exception:
            pass                                         # only stops the button's spinner; the action still runs
        return on_button(f"tg:{q['message']['chat']['id']}", q.get("data", ""))
    m = u.get("message") or {}
    if not m.get("chat"):
        return
    if m["chat"].get("type", "private") != "private":
        return                                           # a group would make all its members one broker or one owner
    key = f"tg:{m['chat']['id']}"
    if not gate(key):
        return
    with LOCKS.setdefault(key, threading.Lock()):
        try:
            media = m.get("voice") or m.get("audio") or m.get("video_note")
            if media:
                if (media.get("duration") or 0) > MAX_VOICE_SEC or (media.get("file_size") or 0) > MAX_VOICE_BYTES:
                    return send(key, TOO_LONG)
                f = requests.get(f"{TG_API}/getFile", params={"file_id": media["file_id"]}, timeout=15).json()["result"]
                audio = fetch_capped(f"https://api.telegram.org/file/bot{TG_TOKEN}/{f['file_path']}")
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
            print("telegram message failed:", safe(e))
            send(key, OOPS)

def handle_message(msg):
    wa = msg["from"]
    if not gate(wa):
        return
    with LOCKS.setdefault(wa, threading.Lock()):
        try:
            kind = msg.get("type")
            if kind == "interactive":
                return on_button(wa, msg["interactive"]["button_reply"]["id"])
            if kind == "button":
                return on_button(wa, msg["button"].get("payload", ""))
            if kind == "audio":
                got = wa_media(msg["audio"]["id"])
                if not got:
                    return send(wa, TOO_LONG)
                audio, mime = got
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

FORGET = re.compile(r"^\s*(delete my data|delete data|forget me|mera data (hatao|delete karo|mitao))\s*[.!]*\s*$", re.I)
PRIVACY = ("🔒 Aapka naam, number aur gaadi ki jaankari sirf load dene ke liye rakhte hain. "
           "Sab hatana ho to *delete my data* likhiye.")
GREETING = re.compile(r"^\s*(hi+|hello|helo|hey|new|reset|namaste|namaskar|start|menu)\s*[!.]*\s*$", re.I)
ROLE_BUTTONS = [("role:owner", "🚚 Truck owner"), ("role:broker", "📦 Broker")]

def role_menu(wa):
    o = desk.owners.get(wa)
    now_as = f"Abhi aap *{o['name']}* (truck owner) ke roop mein jude hain.\n" if o and not o.get("sandbox") else ""
    send(wa, "Namaste 🙏 Bhaada freight desk.\n" + now_as + "Aap kaun hain? Neeche button dabaiye:\n"
             "🚚 *Truck owner*: gaadi khaali batao, load aur rate yahin milenge\n"
             "📦 *Broker*: load bhejo (text ya voice), hum truck dhoondh ke quote denge\n" + PRIVACY, ROLE_BUTTONS)

def join_as_owner(wa, name, city):
    o = desk.register_owner(wa, name, city)
    desk.brokers.pop(wa, None)
    if desk.settings["mode"] != "live":
        desk.settings["mode"] = "live"
        desk.post("save")
    send(wa, f"Namaste {o['name']} 🙏 Aap fleet owner ke roop mein jud gaye" + (f" ({o['baseCity']})" if o.get("baseCity") else "")
             + ".\nGaadi khaali ho to aise bhejiye: *32 ft MXL, Bhiwandi, aaj, ₹85/km*\n(Role badalna ho to *hi* likhiye.)\n"
             + PRIVACY)

def on_text(wa, text):
    text = text[:MAX_TEXT]
    if FORGET.match(text):                                 # before remember(): nothing of theirs is kept
        desk.post("forget", key=wa)
        CONVO.pop(wa, None)
        return send(wa, "🗑️ Aapka naam, number, gaadi aur chat desk se hata diye gaye. Dobara judna ho to *hi* likhiye.")
    remember(wa, "Them", text)
    if GREETING.match(text):
        return role_menu(wa)
    waiting = desk.brokers.get(wa) or {}
    if waiting.get("await") == "owner" and wa not in desk.owners:     # they tapped "Truck owner": this is their name
        parts = [p.strip() for p in re.split(r",|\bfrom\b|\bse\b|\n", re.sub(r"^\s*owner\b[\s:,-]*", "", text, flags=re.I)) if p.strip()]
        if not parts or len(parts[0]) > 40 or re.search(r"\d", parts[0]):
            return send(wa, "Apna naam ya company aur shehar bhejiye, e.g. *Sharma Transport, Bhiwandi*")
        return join_as_owner(wa, parts[0], parts[1] if len(parts) > 1 else None)
    m = REGISTER.match(text)
    # "owner ne cancel kiya?" or "transporter chahiye Surat se 14 ton" is a broker talking, not a registration
    if m and not NOT_A_NAME.search(m.group(2)) and ("," in m.group(2) or not desk.broker_loads(wa)):
        parts = [p.strip() for p in re.split(r",|\bfrom\b|\bse\b", m.group(2)) if p.strip()]
        if not parts:
            return send(wa, "Aise likhiye: *owner Sharma Transport, Bhiwandi*")
        return join_as_owner(wa, parts[0], parts[1] if len(parts) > 1 else None)
    if AS_BROKER.match(text):
        was = desk.remove_owner(wa) if wa in desk.owners and not desk.owners[wa].get("sandbox") else None
        desk.brokers[wa] = {"chat": [], "load": {}}
        return send(wa, ("Owner list se hata diya. " if was else "") + "Ab aap broker hain 👍 Load bhejiye (text ya voice note).\n"
                        "e.g. _Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL_")
    if wa in desk.owners and not desk.owners[wa].get("sandbox"):
        return desk.post("owner_message", key=wa, text=text)
    # anyone else is a broker
    s = desk.brokers.setdefault(wa, {"chat": [], "load": {}})
    if broker_command(wa, text):
        return
    if not E.llm_ready():
        return send(wa, "Desk abhi load padh nahi sakta (language model band hai) 🙏 Thodi der baad bhejiye.")
    intent, said = ("load", None) if plainly_load(text) else broker_brain(wa, text, s)
    if intent == "reply":
        if TONS.search(text) or E.material_class(text) != "general":   # load details in a question: keep them for "haan, dhoondo"
            s["chat"] += [{"role": "user", "text": text}, {"role": "bot", "text": said}]
            desk.post("save")
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
    del s["chat"][:-16]                                     # saved with the desk state: a broker who never finishes can't grow it
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

TONS = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ton|tonne|tan|t|mt)\b", re.I)

def plainly_load(text):
    """Load details nobody would mistake for small talk ("Bhiwandi se Surat 14 ton 32 MXL"): skip the routing call,
    which saves a model call per message against Groq's per-minute cap."""
    if "?" in text or len(text) > 300 or re.search(r"\b(kaunsi|konsi|kaun si|which|suggest|salah)\b", text, re.I):
        return False
    low = text.lower()
    cities = sum(bool(re.search(r"\b" + re.escape(c.lower()) + r"\b", low)) for c in E.KNOWN_CITIES)
    return (min(cities, 2) + bool(TONS.search(text)) + bool(E.norm_type(text))) >= 2

def broker_brain(wa, text, s):
    """What does the broker mean? ("load", None): load details or a truck request, for intake.
    ("reply", text): thanks, small talk or a question, answered from their own loads. Falls back to intake."""
    loads = broker_facts(wa)
    asked = next((m["text"] for m in reversed(s["chat"]) if m["role"] == "bot"), None)
    recent = list(CONVO.get(wa, []))[:-1][-8:]          # the chat so far, without the message being answered
    try:
        out = E.parse_json((E.chat([{"role": "user", "content":
            "You are Bhaada, an AI freight desk chatting with a BROKER on Telegram/WhatsApp.\n"
            "How the desk works (explain only if asked): the broker sends a load by text or voice note; the desk messages "
            "nearby truck owners at once, negotiates their rate inside the broker's margin band, and sends a quote with "
            "Approve/Reject buttons; on approval the owner holds the truck; if an owner backs out the desk re-books a "
            "backup at the same shipper price. *status* shows their loads; *reject BH-xxxx* cancels a quote.\n"
            "Recent chat (oldest first):\n" + ("\n".join(recent) or "(new chat)") + "\n"
            "Truck guide (advise from this when they ask which truck suits a load; the cheapest that fits weight, volume "
            "and the goods' body rule; light bulky goods like cloth or cartons fill space before weight):\n" + E.truck_catalogue()
            + "\nGoods rules:\n" + E.material_guide() + "\n"
            + (f"The desk's computed pick for this message (recommend exactly this, explain it simply): {E.advise(text)}\n"
               if E.advise(text) else "")
            + "Their recent loads (newest last):\n" + ("\n".join("- " + x for x in loads) or "- none") + "\n"
            + (f"A new load is being collected: known so far {json.dumps(s['load'], ensure_ascii=False)}; "
               f"your last question was: \"{asked}\"\n" if s["chat"] else "")
            + f'Broker\'s message: "{text}"\n'
            "Decide:\n"
            "- intent=\"load\" if the message gives or changes load details (cities, weight, goods, truck, pickup day, budget), "
            "asks you to find or book a truck, or answers your last question. Leave reply empty.\n"
            "- If they ASK which truck suits their goods (kaunsi gaadi, which truck, suggest), intent=\"reply\": recommend one "
            "truck from the guide with a one-line reason (weight, volume, body), mention one alternative if useful, and ask "
            "whether to find that truck now (e.g. \"Ye gaadi dhoondh dun?\").\n"
            "- intent=\"reply\" for everything else: thanks, greetings, ok, small talk, questions about their loads, owners, "
            "prices, which truck to use, or how the desk works. Answer in 1-3 short lines using ONLY the facts above; never invent a load, "
            "truck, owner or price. Thanks/ok gets a short warm reply (e.g. \"Shukriya ji 🙏 Aur koi load ho to bhejiye.\") "
            "and never a question about tons or cities. Mention their loads only when the message is about them; don't "
            "repeat a load status they already got. Market or rate trends: you have no market feed, say so briefly and "
            "offer to quote a lane. If a load is being collected, you may gently remind what is still needed. To cancel a quote they write *reject BH-xxxx*; a booked trip is changed by calling the owner.\n"
            "Reply in Hinglish (Hindi words in Roman script), never plain English.\n"
            'Return ONLY JSON {"intent":"load|reply","reply":""}'}],
            json_mode=True, timeout=12, retries=2, max_wait=5, tier="smart").get("content") or ""))   # on any trouble intake reads it
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
    if action == "role":
        is_owner = wa in desk.owners and not desk.owners[wa].get("sandbox")
        if lid == "owner":
            if is_owner:
                return send(wa, f"Aap pehle se truck owner hain ({desk.owners[wa]['name']}) 👍 Gaadi khaali ho to bhejiye: "
                                "*32 ft MXL, Bhiwandi, aaj, ₹85/km*")
            desk.brokers[wa] = {"chat": [], "load": {}, "await": "owner"}
            return send(wa, "Badhiya 🙏 Apna naam ya company aur shehar bhejiye, e.g. *Sharma Transport, Bhiwandi*")
        if is_owner:
            desk.remove_owner(wa)
        desk.brokers[wa] = {"chat": [], "load": {}}
        return send(wa, ("Owner list se hata diya. " if is_owner else "") + "Aap broker hain 👍 Load text ya voice note mein bhejiye.\n"
                        "e.g. _Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL_")
    load = desk._find(lid) if lid else None
    if action in ("call_yes", "call_no") and load and load.get("brokerPhone") == wa:
        if load["status"] != "needs_call":
            return send(wa, "Ye sawaal ab khula nahi hai. *status* likh ke haal dekhiye.")
        desk.post("broker_call", lid=lid, yes=action == "call_yes")
        return send(wa, "👍 Theek hai, kar raha hoon…" if action == "call_yes" else "Theek hai, load aapke paas hai.")
    if action in ("approve", "reject") and load and load.get("brokerPhone") == wa:
        if load["status"] != "needs_approval":
            text, buttons = desk.broker_status(load)
            return send(wa, "Ye quote ab khula nahi hai. " + text, buttons)
        desk.post(action, lid=lid)
        return send(wa, "✅ Approve ho gaya. Owner se gaadi hold karwa raha hoon…" if action == "approve" else "Reject kar diya. Kuch book nahi hua.")
    send(wa, "Ye button ab kaam ka nahi hai. Load ka haal jaanne ke liye *status* likhiye, ya naya load seedha bhejiye.")
