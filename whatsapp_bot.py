"""
Bhaada on real WhatsApp (Meta WhatsApp Cloud API) - same engine as the web desk.

The broker chats with a WhatsApp number: text or a voice note goes in, the orchestrator sources and prices,
and the quote comes back as a message with Approve / Reject buttons. After approval a demo button lets you
trigger the owner back-out and watch the agent re-book on its own.

Run locally:   uvicorn whatsapp_bot:app --port 8000      (then expose it, e.g. cloudflared tunnel --url http://localhost:8000)
Config (env vars, or .streamlit/secrets.toml locally):
    GROQ_API_KEY, WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_VERIFY_TOKEN
Owner replies are still simulated personas; only the broker side is real WhatsApp.
"""
import os, threading
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import engine as E

_sec = E.local_secrets()
def _cfg(k, default=""):
    return os.getenv(k) or _sec.get(k, default)

E.configure(key=_cfg("GROQ_API_KEY"), model=_cfg("GROQ_MODEL"), whisper=_cfg("GROQ_WHISPER"))
WA_TOKEN, WA_PHONE_ID = _cfg("WHATSAPP_TOKEN"), _cfg("WHATSAPP_PHONE_ID")
WA_VERIFY = _cfg("WHATSAPP_VERIFY_TOKEN", "bhaada_verify_123")
GRAPH = "https://graph.facebook.com/v21.0"
M = E.MARGIN

app = FastAPI(title="Bhaada WhatsApp desk")
SESSIONS = {}            # wa_id -> {"chat", "load", "result", "stage", "replan"}
MEMORY = {}              # learning loop shared across chats (owner reliability)
SEEN = set()             # Meta can deliver a message twice; handle each id once
LOCKS = {}

def session(wa_id):
    return SESSIONS.setdefault(wa_id, {"chat": [], "load": {}, "result": None, "stage": "collect", "replan": None})

def reset(wa_id):
    SESSIONS.pop(wa_id, None)

# ---------------- sending ----------------
def _post(payload):
    r = requests.post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"},
                      json={"messaging_product": "whatsapp", **payload}, timeout=20)
    if not r.ok:
        print("WhatsApp send failed:", r.status_code, r.text[:300])
    return r

def send_text(to, text):
    return _post({"to": to, "type": "text", "text": {"body": text[:4000]}})

def send_buttons(to, text, buttons):
    """Up to 3 reply buttons; titles max 20 characters (WhatsApp limit)."""
    return _post({"to": to, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": text[:1024]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons]}}})

def download_media(media_id):
    h = {"Authorization": f"Bearer {WA_TOKEN}"}
    meta = requests.get(f"{GRAPH}/{media_id}", headers=h, timeout=20).json()
    data = requests.get(meta["url"], headers=h, timeout=60)
    data.raise_for_status()
    return data.content, meta.get("mime_type", "audio/ogg")

# ---------------- message text (same story as the web desk, in WhatsApp format) ----------------
def fmt_mmss(s):
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"

def quote_text(res):
    ld, q, o, th = res["load"], res["quote"], res["owner"], res["threads"]
    nd = sum(t["status"] == "deal" for t in th)
    nx = sum(t["status"] == "declined" for t in th)
    ns = sum(t["status"] == "silent" for t in th)
    lines = [f"✅ *{ld['origin']} → {ld['destination']}* · {ld['weight_ton']:g}T · {ld['truck_type']} · {ld.get('pickup_when') or 'kal'}"]
    if res["search_type"] != ld["truck_type"]:
        cap = E.rates["capacityTonByType"][ld["truck_type"]]
        lines.append(f"🔄 {ld['truck_type']} carries max {cap} T, so I sourced a *{res['search_type']}*")
    lines.append(f"📲 Messaged {len(th)} owners near {ld['origin']}: {nd} agreed, {nx} declined, {ns} no reply")
    neg = next((t for t in th if t["status"] == "deal" and t["deal"]["rounds"] >= 2), None)
    if neg:
        lines.append(f"🤝 Countered {neg['owner']['ownerName']}: ₹{neg['ask']}/km → ₹{neg['final']}/km")
    lines += ["",
              f"🚛 *{o['ownerName']}* · {o['type']} · {o['reliability']}★ · in {o['currentCity']}",
              f"💰 Quote to shipper: *₹{q['sell_total']:,}* (₹{q['rate_per_km']}/km)",
              f"📊 Buy ₹{q['buy_total']:,} · margin *{q['margin_pct']}%* (floor {M['floorPct']}%)"]
    if q.get("note"):
        lines.append(f"ℹ️ {q['note']}")
    lines += ["", f"_{res.get('reason', '')}_",
              f"⏱ Booking-ready in ~{fmt_mmss(res['timeline']['total'])} real-world (modelled)"]
    return "\n".join(lines)

def replan_text(rp, prev):
    head = (f"⚠️ *{rp['dropped']}* backed out. Reliability {rp['rel_before']}★ → {rp['rel_after']}★.\n"
            f"Shipper price stays *₹{prev['quote']['sell_total']:,}*. Re-confirming owners who agreed earlier…\n\n")
    if rp["status"] == "recovered":
        o, q = rp["owner"], rp["quote"]
        return head + (f"✅ *Re-booked* with {o['ownerName']} · {o['type']} · {o['reliability']}★\n"
                       f"New buy ₹{q['buy_total']:,} · margin *{q['margin_pct']}%* (still above {M['floorPct']}% floor)\n"
                       f"0 action needed from you.")
    return head + f"❌ Nothing clears the {M['floorPct']}% floor, so nothing is booked. Over to you: {rp.get('reason', '')}"

# ---------------- conversation ----------------
def handle_text(wa_id, text):
    s = session(wa_id)
    if text.strip().lower() in ("reset", "new", "new load", "start", "hi", "hello", "namaste"):
        reset(wa_id)
        return send_text(wa_id, "Namaste 🙏 Bhaada here. Send me a load: text or voice note.\n"
                                "e.g. _Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL_")
    if s["stage"] != "collect":
        reset(wa_id)
        s = session(wa_id)
    if not E.llm_ready():
        return send_text(wa_id, "My language model isn't configured yet (GROQ_API_KEY).")
    s["chat"].append({"role": "user", "text": text})
    try:
        merged, reply, miss = E.intake_turn(s["chat"], s["load"], text)
    except Exception as e:
        return send_text(wa_id, f"Sorry, I couldn't read that ({str(e)[:80]}). Please send it again.")
    s["load"] = {k: v for k, v in merged.items() if v not in (None, "")}
    s["chat"].append({"role": "bot", "text": reply})
    if miss:
        return send_text(wa_id, reply)
    if not E.find_lane(merged["origin"], merged["destination"]):
        return send_text(wa_id, f"{merged['origin']} → {merged['destination']} isn't in my pilot network yet. Passing it to you.")
    send_text(wa_id, f"🔍 Got it. Messaging owners near {merged['origin']} now…")
    res = E.run_quote(merged, E.build_pool(MEMORY), use_llm=True)
    s["result"] = res
    if res["status"] != "quote":
        s["stage"] = "done"
        return send_text(wa_id, f"❌ I have *not* quoted this one. {res.get('reason', '')}\nOver to you. Send *new* for another load.")
    s["stage"] = "quote"
    return send_buttons(wa_id, quote_text(res), [("approve", "Approve ✅"), ("reject", "Reject ❌")])

def handle_button(wa_id, bid):
    s = session(wa_id)
    res = s.get("result")
    if bid == "new" or not res:
        reset(wa_id)
        return send_text(wa_id, "Send me the next load 🚚")
    if bid == "reject":
        reset(wa_id)
        return send_text(wa_id, "Rejected. Nothing booked. Send the next load whenever ready.")
    if bid == "approve" and s["stage"] == "quote":
        s["stage"] = "booked"
        o, q = res["owner"], res["quote"]
        return send_buttons(wa_id, f"✅ Approved. {o['ownerName']} confirmed the hold.\n"
                                   f"Quote *₹{q['sell_total']:,}* sent to the shipper; truck is booking-ready.",
                            [("backout", "Owner backs out ⚠️"), ("new", "New load")])
    if bid == "backout" and s["stage"] == "booked":
        oid, before = res["owner"]["ownerId"], res["owner"]["reliability"]
        E.penalise(MEMORY, oid)
        pool = E.build_pool(MEMORY)
        after = next((p["reliability"] for p in pool if p["ownerId"] == oid), before)
        send_text(wa_id, "⚠️ 4:02 PM: the owner just backed out. Re-planning on my own…")
        rp = E.run_replan(res, pool, use_llm=True)
        rp.update(rel_before=before, rel_after=after)
        s["stage"], s["replan"] = "replanned", rp
        return send_buttons(wa_id, replan_text(rp, res), [("new", "New load")])
    return send_text(wa_id, "That button has expired. Send *new* to start again.")

def handle_message(msg):
    wa_id = msg["from"]
    with LOCKS.setdefault(wa_id, threading.Lock()):       # one message at a time per broker
        try:
            kind = msg.get("type")
            if kind == "text":
                handle_text(wa_id, msg["text"]["body"])
            elif kind == "audio":
                audio, mime = download_media(msg["audio"]["id"])
                text = E.transcribe(audio, mime)
                send_text(wa_id, f"🎤 Heard: _{text}_")
                handle_text(wa_id, text)
            elif kind == "interactive":
                handle_button(wa_id, msg["interactive"]["button_reply"]["id"])
            elif kind == "button":                         # quick-reply buttons on template messages
                handle_button(wa_id, msg["button"].get("payload", ""))
            else:
                send_text(wa_id, "I read text and voice notes for now. Please type or record the load.")
        except Exception as e:
            print("handle_message error:", repr(e))
            send_text(wa_id, "Something went wrong on my side. Send *new* to start again.")

# ---------------- webhook ----------------
@app.get("/webhook")
def verify(request: Request):
    """Meta calls this once when you save the webhook URL."""
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == WA_VERIFY:
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("verification failed", status_code=403)

@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    """Acknowledge at once (Meta retries slow webhooks) and work the message in the background."""
    body = await request.json()
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("id") in SEEN:
                    continue
                SEEN.add(msg.get("id"))
                bg.add_task(handle_message, msg)
    return {"ok": True}

@app.get("/")
def health():
    return {"service": "bhaada-whatsapp", "llm": E.llm_ready(), "whatsapp": bool(WA_TOKEN and WA_PHONE_ID)}
