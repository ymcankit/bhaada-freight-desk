"""
Bhaada - Agentic Freight Desk (Streamlit MVP, Abhikalp 2026 Case 9)
Run:  pip install -r requirements.txt  ->  streamlit run app.py
Keys: put GROQ_API_KEY in .streamlit/secrets.toml (local) or the Streamlit Cloud secrets box.
Without a key the desk still runs, on the deterministic planner (chat intake needs the key).
"""
import os, json, time
from datetime import datetime
import streamlit as st
import engine as E

st.set_page_config(page_title="Bhaada Freight Desk", page_icon="🚚", layout="wide")

def _secret(k, default=""):
    try:
        return st.secrets.get(k, os.getenv(k, default))
    except Exception:                       # no secrets.toml at all
        return os.getenv(k, default)

E.configure(key=_secret("GROQ_API_KEY"), model=_secret("GROQ_MODEL", "openai/gpt-oss-20b"),
            whisper=_secret("GROQ_WHISPER", "whisper-large-v3-turbo"))
MARGIN, KNOWN_CITIES, KNOWN_TYPES = E.MARGIN, E.KNOWN_CITIES, E.KNOWN_TYPES

# ---------------- conversational intake (LLM, with memory) ----------------
def _hist(h):
    who = {"user": "Broker", "owner": "Owner", "bot": "Bhaada", "agent": "Bhaada"}
    return "\n".join(f'{who.get(m["role"], m["role"])}: {m["text"]}' for m in h) or "(start of chat)"

def broker_turn(hist, load, user_msg):
    """Broker <-> Bhaada, remembering the whole conversation and merging the load across turns."""
    prompt = f"""You are Bhaada, an AI freight desk assistant chatting with a BROKER on WhatsApp.
Collect ONE complete load over the conversation. REMEMBER everything already said - never re-ask a detail you were given.
Known cities (map to the closest one): {", ".join(KNOWN_CITIES)}
Truck types (use these exact names): {", ".join(KNOWN_TYPES)}
Load known so far (may be partial): {json.dumps(load)}
Conversation so far:
{_hist(hist)}
Latest broker message: "{user_msg}"
Merge new details into the load; keep existing values unless the broker corrects them.
Required fields: origin, destination, weight_ton, truck_type. Optional: pickup_when, budget_rs (shipper's budget in rupees, number).
Return ONLY JSON:
{{"reply":"<short friendly reply, Hinglish ok>","load":{{"origin":null,"destination":null,"weight_ton":null,"truck_type":null,"pickup_when":null,"budget_rs":null}},"missing":["..."],"complete":false}}
Fill load with everything known so far. If all required fields are known set complete=true and reply that you are finding the best truck now."""
    return E.parse_json(E.gen(prompt, json_mode=True))

ASK = {"origin": "Kahan se load uthana hai?", "destination": "Kahan tak jaana hai?",
       "weight_ton": "Kitna ton hai?",
       "truck_type": "Kaunsi gaadi chahiye? (" + ", ".join(KNOWN_TYPES[:6]) + " …)"}

def fmt_mmss(s):
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"

CSS = """
<style>
.block-container{padding:0.8rem 1.2rem 2rem;max-width:1500px}
header[data-testid="stHeader"]{background:transparent}
.bh{background:#0E3B2E;color:#fff;border-radius:14px;padding:12px 18px;display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.bh .lg{width:36px;height:36px;border-radius:9px;background:#E8732A;display:flex;align-items:center;justify-content:center;font-size:20px}
.bh .nm{font-size:20px;font-weight:800;line-height:1;color:#fff}
.bh .sb{font-size:10px;color:#B9D3C7;font-weight:700;letter-spacing:1px}
.bh .sp{margin-left:auto;display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.bh .cp{background:rgba(255,255,255,.13);padding:5px 10px;border-radius:7px;font-size:11.5px;font-weight:700;color:#eaf3ee}
.bh .clk{background:#E8732A;color:#fff;padding:6px 13px;border-radius:999px;font-size:13px;font-weight:800;font-variant-numeric:tabular-nums}
.bh .clk.idle{background:rgba(255,255,255,.13);color:#cfe0d8}

/* ---- whatsapp panel (self-contained, no streamlit selectors) ---- */
.wa{background-color:#ECE3DA;
 background-image:
  radial-gradient(circle at 12% 18%, rgba(11,60,45,.055) 0 6px, transparent 7px),
  radial-gradient(circle at 46% 36%, rgba(11,60,45,.04) 0 9px, transparent 10px),
  radial-gradient(circle at 78% 14%, rgba(11,60,45,.05) 0 5px, transparent 6px),
  radial-gradient(circle at 28% 72%, rgba(11,60,45,.045) 0 8px, transparent 9px),
  radial-gradient(circle at 88% 64%, rgba(11,60,45,.04) 0 6px, transparent 7px),
  radial-gradient(circle at 62% 88%, rgba(11,60,45,.05) 0 7px, transparent 8px);
 background-size:170px 170px;
 border:1px solid #cfc6bb;border-radius:14px;overflow:hidden;margin-bottom:10px}
.wa .top{background:#128C7E;padding:9px 14px;display:flex;align-items:center;gap:10px}
.wa .av{width:32px;height:32px;border-radius:50%;background:#0E3B2E;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px}
.wa .av.o{background:#8a6d46}
.wa .nm{font-size:14px;font-weight:700;color:#fff;line-height:1.15}
.wa .st{font-size:11px;color:#d3ece5}
.wa .body{padding:10px 12px 14px;min-height:60px}
.b-in,.b-out{max-width:86%;padding:6px 9px 4px;border-radius:8px;margin:6px 0;font-size:13.5px;line-height:1.45;
 box-shadow:0 1px .5px rgba(0,0,0,.15);width:fit-content;color:#111b21}
.b-in{background:#fff}
.b-out{background:#d9fdd3;margin-left:auto}
.b-who{font-size:10px;font-weight:800;color:#0b7a5b;margin-bottom:2px}
.b-t{font-size:9.5px;color:#667781;float:right;margin:3px 0 -2px 9px}
.b-sys{text-align:center;margin:7px auto;background:#dbe8d6;color:#44563f;font-size:10.5px;font-weight:700;padding:3px 11px;border-radius:7px;width:fit-content}
.b-card{background:#fff;border-radius:8px;padding:10px 12px;margin:6px 0;box-shadow:0 1px .5px rgba(0,0,0,.15);
 max-width:94%;font-size:13px;line-height:1.6;color:#111b21}
.b-card.re{border-left:4px solid #E8732A}
.b-card b{font-weight:800;color:#0b2f22}
.b-alert{background:#fff2f2;border-left:4px solid #DC2626;border-radius:7px;padding:8px 11px;margin:6px 0;
 font-size:12.5px;color:#9b1c1c;font-weight:600;max-width:94%}
.pill{display:inline-block;background:#E4F6E9;color:#0f7a37;font-size:10px;font-weight:800;padding:2px 8px;border-radius:999px}
.pill.w{background:#FDF0DC;color:#92610f}
.pill.r{background:#FCE8E8;color:#a81f1f}
.pill.g{background:#EDEDED;color:#5c5c5c}
.typing{display:inline-block;background:#fff;border-radius:8px;padding:7px 12px;font-size:13px;color:#667781;
 box-shadow:0 1px .5px rgba(0,0,0,.15);margin:6px 0}

/* ---- side cards ---- */
.sh{font-size:10px;font-weight:800;letter-spacing:1.1px;color:#7b8a81;text-transform:uppercase;margin:4px 0 6px}
.cd{background:#fff;border:1px solid #e0dcd2;border-radius:11px;padding:11px 12px;margin-bottom:10px;color:#17251e}
.row{display:flex;justify-content:space-between;font-size:12.5px;padding:2.5px 0;color:#17251e}
.row span{color:#6b7a72}
.row b{color:#17251e;font-weight:700}
.row.tot{border-top:2px solid #e0dcd2;margin-top:4px;padding-top:6px;font-weight:800}
.stp{display:flex;align-items:center;gap:8px;font-size:12.5px;padding:3px 0;color:#17251e}
.dot{width:16px;height:16px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;flex:0 0 16px}
.dot.ok{background:#16A34A}.dot.run{background:#E8732A}.dot.wait{background:#c9d1cb}
.stp .d{color:#7b8a81;font-size:11.5px}
.mt{height:11px;border-radius:6px;background:linear-gradient(90deg,#f5d9d9 0%,#fbeccd 46%,#d8f1e0 74%,#cdefd8 100%);position:relative;margin:9px 3px 22px}
.mf{position:absolute;top:-3px;width:4px;height:17px;background:#13311f;border-radius:3px}
.mk{position:absolute;top:14px;font-size:9px;font-weight:800;color:#7b8a81;transform:translateX(-50%)}
.wa.mini .top{padding:6px 11px}.wa.mini .av{width:26px;height:26px;font-size:12px}.wa.mini .nm{font-size:12.5px}.wa.mini .st{font-size:10px}.wa.mini .body{padding:7px 10px 9px;min-height:0}.wa.mini .b-in,.wa.mini .b-out{font-size:12.5px;padding:5px 8px 3px;margin:4px 0}.wa.mini .typing{font-size:11.5px;padding:5px 10px}
.ohead{display:flex;align-items:center;gap:7px;margin-bottom:3px}
.oname{font-size:12.5px;font-weight:800;color:#17251e}
.osub{font-size:10.5px;color:#7b8a81}
.cmp{display:flex;gap:10px;margin:6px 0 2px;align-items:flex-end}
.bar{flex:1;text-align:center}
.bar .v{font-size:19px;font-weight:800;color:#17251e}
.bar .fillbox{height:76px;display:flex;align-items:flex-end;justify-content:center;margin:4px 0}
.bar .fill{width:46px;border-radius:6px 6px 0 0}
.bar .lb{font-size:10.5px;color:#6b7a72;font-weight:700}
.stButton>button{border-radius:9px;font-weight:700}
.stButton>button[kind="primary"]{background:#17533D;border:none;color:#fff}
.stButton>button[kind="primary"]:hover{background:#0E3B2E;color:#fff}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)
st.markdown("""<style>
.trc{font-size:11.5px;line-height:1.45;color:#17251e;padding:3px 0;border-bottom:1px dashed #e6e1d6}
.trc:last-child{border-bottom:none}
.trc code{background:#EEF4F0;color:#0b5a40;padding:0 4px;border-radius:4px;font-size:11px}
.trc .th{color:#7b8a81;font-style:italic;display:block}
</style>""", unsafe_allow_html=True)

# ---------------- html builders ----------------
MAXLV = 8
NOW = datetime.now().strftime("%I:%M %p").lstrip("0")

def bub(side, text, who=None, t=True):
    cls = "b-out" if side == "out" else "b-in"
    w = f'<div class="b-who">{who}</div>' if who else ""
    tt = f'<div class="b-t">{NOW}{" ✓✓" if side == "out" else ""}</div>' if t else ""
    return f'<div class="{cls}">{w}{text}{tt}</div>'

def typing(label):
    return f'<div class="typing">{label} ●●●</div>'

def wa_panel(title, sub, body, avatar="B", av_cls="", badge="", mini=False):
    bd = f'<div style="margin-left:auto">{badge}</div>' if badge else ""
    return (f'<div class="wa{" mini" if mini else ""}"><div class="top"><div class="av {av_cls}">{avatar}</div>'
            f'<div><div class="nm">{title}</div><div class="st">{sub}</div></div>{bd}</div>'
            f'<div class="body">{body}</div></div>')

def clock_at(level, res, stage):
    """Modelled real-world clock, built from the owners' response delays (the demo itself runs faster)."""
    if not res or level == 0:
        return "--:--"
    tl = res.get("timeline") or {}
    a, s, p = tl.get("intake", 30), tl.get("sourcing", 0), tl.get("pricing", 10)
    if stage in ("booked", "replanned") and res.get("status") == "quote":
        return fmt_mmss(tl.get("total", a + s + p))
    return fmt_mmss({1: 0, 2: 15, 3: a, 4: a, 5: a + s, 6: a + s, 7: a + s + p, 8: a + s + p}[level])

def replan_secs(rp):
    tl = rp.get("timeline") or {}
    return tl.get("sourcing", 0) + tl.get("pricing", 0) + tl.get("hold", 0)

def broker_events(res, msg, was_audio, stage, rp):
    """Every chat message, tagged with the step it appears at. The agents talk as they work."""
    ev = []
    if was_audio:
        ev.append((1, bub("out", "🎤 Voice note")))
        ev.append((1, typing("Intake agent is transcribing")))
        ev.append((2, bub("in", f'Heard you ✅<br><i>“{msg}”</i>', "INTAKE AGENT")))
    else:
        ev.append((1, bub("out", msg)))
        ev.append((1, typing("Intake agent is reading")))

    if res["status"] == "need_info":
        ev.append((3, bub("in", ASK.get((res.get("missing") or ["weight_ton"])[0]), "INTAKE AGENT")))
        return ev
    if res["status"] == "unknown_lane":
        ev.append((3, bub("in", "This lane isn’t in my network yet — passing it to you.", "INTAKE AGENT")))
        return ev

    ld = res["load"]
    budget = f' · budget ₹{ld["budget_rs"]:,.0f}' if ld.get("budget_rs") else ""
    ev.append((3, bub("in", f'Parsed ✅ <b>{ld["origin"]} → {ld["destination"]}</b> · '
                            f'{ld["weight_ton"]:g}T · {ld.get("truck_type","")} · {ld.get("pickup_when") or "kal"}{budget}',
                      "INTAKE AGENT")))
    stype = res.get("search_type")
    if stype and stype != ld["truck_type"]:
        cap = E.rates["capacityTonByType"][ld["truck_type"]]
        ev.append((4, bub("in", f'A {ld["truck_type"]} carries max {cap} T — {ld["weight_ton"]:g} T needs a '
                                f'<b>{stype}</b>. Sourcing that instead.', "ORCHESTRATOR")))
    th = res["threads"]
    if not th:
        ev.append((5, bub("in", f'I have <b>not</b> quoted — over to you. {res.get("reason","")}', "ORCHESTRATOR")))
        return ev
    rounds = len(res["sess"].rounds)
    ev.append((4, bub("in", f'Messaging <b>{len(th)} owners</b> near {ld["origin"]} — all at once'
                            + (f', in {rounds} rounds' if rounds > 1 else '') + '.', "SOURCING AGENT")))
    ev.append((4, typing("waiting for owner replies")))
    nd = sum(1 for t in th if t["status"] == "deal")
    nx = sum(1 for t in th if t["status"] == "declined")
    nsil = sum(1 for t in th if t["status"] == "silent")
    ev.append((5, bub("in", f'Replies in — <b>{nd} agreed</b>, {nx} declined, {nsil} no reply.', "SOURCING AGENT")))

    if res["status"] == "escalate":
        ev.append((6, bub("in", f'I have <b>not</b> quoted — over to you. {res.get("reason","")}', "ORCHESTRATOR")))
        return ev

    win = res["win"]
    neg = next((t for t in th if t["status"] == "deal" and t["deal"]["rounds"] >= 2), None)
    if neg:
        nt = (f'Countered {neg["owner"]["ownerName"]}: ₹{neg["ask"]}/km → '
              f'<b>₹{neg["final"]}/km</b>. Stayed inside your band.')
    else:
        nt = "All agreed quotes already sat inside your band — no counter needed."
    ev.append((6, bub("in", nt, "NEGOTIATION AGENT")))
    ev.append((6, bub("in", f'Picked <b>{win["owner"]["ownerName"]}</b> at ₹{win["final"]}/km. '
                            f'<i>{res.get("reason","")}</i>', "ORCHESTRATOR")))

    q = res["quote"]
    note = f' <i>({q["note"]})</i>' if q.get("note") else ""
    ev.append((7, bub("in", f'Costed it: buy ₹{q["buy_total"]:,} → sell ₹{q["sell_total"]:,}. '
                            f'Margin <b>{q["margin_pct"]}%</b> vs your {MARGIN["floorPct"]}% floor.{note}', "PRICING AGENT")))

    o = res["owner"]
    ev.append((8, f'<div class="b-card"><b>Quote ready for approval</b> <span class="pill">WITHIN MARGIN ✓</span><br>'
                  f'🚛 {o["ownerName"]} · {o["type"]} ({o["reliability"]}★) · in {o["currentCity"]}<br>'
                  f'📍 {ld["origin"]} → {ld["destination"]} · {res["lane"]["distanceKm"]} km<br>'
                  f'💰 ₹{q["rate_per_km"]}/km · <b>Total ₹{q["sell_total"]:,}</b><br>'
                  f'📊 Margin <b>{q["margin_pct"]}%</b> — floor {MARGIN["floorPct"]}%'
                  f'<div class="b-t">{NOW}</div></div>'))

    if stage in ("booked", "replanned"):
        tot = res["timeline"]["total"]
        ev.append((MAXLV, '<div class="b-sys">✅ You approved · owner confirmed the hold</div>'))
        ev.append((MAXLV, bub("in", f'Quote sent to the shipper; truck from {o["ownerName"]} held. Booking-ready in '
                                    f'<b>{fmt_mmss(tot)}</b> (modelled real-world time).', "BOOKING AGENT")))
    if stage == "replanned" and rp:
        ev.append((MAXLV, f'<div class="b-alert">⚠️ {rp["dropped"]} backed out at 4:02 PM. '
                          f'Reliability {rp["rel_before"]}★ → {rp["rel_after"]}★ (remembered for next time).</div>'))
        ev.append((MAXLV, bub("in", f'Goal unchanged — the shipper’s ₹{q["sell_total"]:,} stays fixed. Re-confirming owners '
                                    f'who agreed earlier, then broadcasting if needed. No action needed from you.', "ORCHESTRATOR")))
        if rp["status"] == "recovered":
            o2, q2 = rp["owner"], rp["quote"]
            ev.append((MAXLV, bub("in", f'New buy ₹{q2["buy_total"]:,} — margin <b>{q2["margin_pct"]}%</b>, '
                                        f'still above the {MARGIN["floorPct"]}% floor, so I re-booked.', "PRICING AGENT")))
            ev.append((MAXLV, f'<div class="b-card re"><b>Re-booked</b> <span class="pill">AUTO-RECOVERED ✓</span><br>'
                              f'🚛 {o2["ownerName"]} · {o2["type"]} ({o2["reliability"]}★) · in {o2["currentCity"]}<br>'
                              f'💰 Shipper still pays <b>₹{q2["sell_total"]:,}</b> · Margin <b>{q2["margin_pct"]}%</b><br>'
                              f'Recovered in {fmt_mmss(replan_secs(rp))} (modelled) with <b>0 action</b> from you.'
                              f'<div class="b-t">{NOW}</div></div>'))
        else:
            ev.append((MAXLV, f'<div class="b-alert">Could not re-book above the {MARGIN["floorPct"]}% floor → '
                              f'escalated to you, nothing booked. {rp.get("reason","")}</div>'))
    return ev

def broker_html(level, res, msg, stage, rp, was_audio=False):
    if not res or level == 0:
        body = bub("in", "Send me a load — type it, or upload a voice note. "
                         "I’ll come back with a priced, booking-ready truck.", "BHAADA")
        return wa_panel("Bhaada AI", "online", body)
    ev = broker_events(res, msg, was_audio, stage, rp)
    out = []
    for lv, h in ev:
        if lv > level:
            continue
        if "typing" in h and any(l2 > lv and l2 <= level for l2, _ in ev):   # drop "typing" once the next step lands
            continue
        out.append(h)
    return wa_panel("Bhaada AI", "typing…" if level < MAXLV else "online", "".join(out))

def owners_html(level, res, stage, rp):
    if not res or res["status"] in ("need_info", "unknown_lane") or level < 4 or not res.get("threads"):
        return ('<div class="cd" style="text-align:center;color:#7b8a81">👥 Owner conversations appear here —<br>'
                'Bhaada messages several owners at the same time.</div>')
    threads = res["threads"]
    head = ""
    if stage == "replanned" and rp and rp.get("threads"):
        threads = rp["threads"]
        head = '<div class="sh">Re-plan · owners contacted after the back-out</div>'
    reveal = 0 if level == 4 else (2 if level == 5 else len(threads))
    out = head
    for i, t in enumerate(threads):
        o = t["owner"]
        if i >= reveal:
            pill = '<span class="pill w">SENT ●●●</span>'
        elif t["status"] == "deal":
            pill = '<span class="pill">AGREED ✓</span>'
        elif t["status"] == "declined":
            pill = '<span class="pill r">DECLINED</span>'
        else:
            pill = '<span class="pill g">NO REPLY</span>'
        lines = "".join(bub(side, m, t=False) for side, m in (t["chat"] if i < reveal else t["chat"][:1]))
        if i >= reveal:
            lines += typing("waiting for reply")
        elif t["status"] == "silent":
            lines += f'<div class="typing">no reply in {E.SRC["ownerReplyTimeoutSec"] // 60} min — dropped</div>'
        dh = f' · {o["deadheadKm"]} km away' if o.get("deadheadKm") else " · at pickup"
        sub = f'{o["type"]} · {o["currentCity"]}{dh} · {o["reliability"]}★'
        out += wa_panel(o["ownerName"], sub, lines, avatar=o["ownerName"][0], av_cls="o", badge=pill, mini=True)
    return out

def mbar(pct):
    scale = MARGIN["ceilingPct"] + 1
    left = max(0, min(100, pct / scale * 100))
    return (f'<div class="mt"><div class="mf" style="left:{left:.1f}%"></div>'
            f'<div class="mk" style="left:{MARGIN["floorPct"] / scale * 100:.1f}%">Floor {MARGIN["floorPct"]}%</div>'
            f'<div class="mk" style="left:{MARGIN["targetPct"] / scale * 100:.1f}%">Target {MARGIN["targetPct"]}%</div></div>')

PLANNER = {"llm": "LLM tool-calling", "fallback": "fallback planner (LLM failed)", "deterministic": "deterministic planner (no key)"}

def trace_html(res, title):
    rows = ""
    for s in res.get("trace", []):
        th = f'<span class="th">{s["thought"]}</span>' if s.get("thought") else ""
        rows += f'<div class="trc">{th}<code>{s["tool"]}</code> {s["summary"]}</div>'
    return (f'<div class="sh">{title}</div><div class="cd"><div class="osub" style="margin-bottom:4px">'
            f'{PLANNER.get(res.get("planner"), "")} · agent run {res.get("compute_s", 0)} s (measured)</div>{rows}</div>')

def side_html(level, res, stage, rp):
    steps = [("Intake", "reads / transcribes the message", 1, 3),
             ("Orchestrator", "plans, picks tools, re-plans", 4, 7),
             ("Sourcing", "messages owners in parallel", 4, 5),
             ("Negotiation", "counters inside guardrails", 6, 6),
             ("Pricing", "deterministic calculator", 7, 7),
             ("Booking", "holds the truck, confirms", 9, 9)]
    rows = ""
    for nm, d, s, e in steps:
        if nm == "Booking" and stage in ("booked", "replanned"):
            cls, ic = "ok", "✓"
        elif level > e:
            cls, ic = "ok", "✓"
        elif s <= level <= e:
            cls, ic = "run", "●"
        else:
            cls, ic = "wait", ""
        rows += f'<div class="stp"><div class="dot {cls}">{ic}</div><b>{nm}</b> <span class="d">· {d}</span></div>'
    html = f'<div class="sh">Agent activity</div><div class="cd">{rows}</div>'
    if res and res.get("trace") and level >= 5:
        html += trace_html(res, "Orchestrator trace")
    if stage == "replanned" and rp and rp.get("trace"):
        html += trace_html(rp, "Re-plan trace")
    html += ('<div class="sh">Guardrails</div><div class="cd">'
             f'<div class="row"><span>Margin floor</span><b>{MARGIN["floorPct"]}%</b></div>'
             f'<div class="row"><span>Target</span><b>{MARGIN["targetPct"]}%</b></div>'
             f'<div class="row"><span>Ceiling</span><b>{MARGIN["ceilingPct"]}%</b></div></div>')
    if res and res["status"] == "quote" and level >= 7:
        q = res["quote"]
        if stage == "replanned" and rp and rp["status"] == "recovered":
            q = rp["quote"]
        html += ('<div class="sh">Pricing (deterministic)</div><div class="cd">'
                 f'<div class="row"><span>Fuel (lane)</span><b>₹{q["fuel"]:,}</b></div>'
                 f'<div class="row"><span>Tolls</span><b>₹{q["toll"]:,}</b></div>'
                 f'<div class="row"><span>Driver</span><b>₹{q["driver"]:,}</b></div>'
                 f'<div class="row"><span>Buy (owner)</span><b>₹{q["buy_total"]:,}</b></div>'
                 f'<div class="row"><span>Sell (shipper)</span><b>₹{q["sell_total"]:,}</b></div>'
                 f'<div class="row tot"><span>Gross margin</span><b>₹{q["margin_rs"]:,} · {q["margin_pct"]}%</b></div></div>')
        html += (f'<div class="sh">Margin vs guardrails</div><div class="cd" style="text-align:center">'
                 f'<div style="font-size:24px;font-weight:800;color:#17533D">{q["margin_pct"]}%</div>{mbar(q["margin_pct"])}</div>')
    return html

# ---------------- state ----------------
ss = st.session_state
ss.setdefault("d_chat", [])
ss.setdefault("d_load", {})
ss.setdefault("d_stage", "collect")      # collect | run
ss.setdefault("result", None)
ss.setdefault("stage", "idle")           # quote | booked | replanned
ss.setdefault("replan", None)
ss.setdefault("msg", "")
ss.setdefault("animate", False)
ss.setdefault("was_audio", False)
ss.setdefault("memory", {})              # learning loop: reliability changes survive across loads
pool = E.build_pool(ss["memory"])
n_trucks, n_owners = len(pool), len({p["ownerId"] for p in pool})

def desk_reset():
    ss.update(d_chat=[], d_load={}, d_stage="collect", result=None,
              stage="idle", replan=None, msg="", animate=False, was_audio=False)

def start_run(load, was_audio=False, msg=None):
    summary = msg or (f"{load['origin']} se {load['destination']} {load.get('pickup_when') or 'kal'}, "
                      f"{load['weight_ton']:g}T, {load['truck_type']}")
    with st.spinner("Orchestrator is working the load…"):
        res = E.run_quote(load, pool, use_llm=True)
    ss.update(result=res, replan=None, msg=summary, was_audio=was_audio, animate=True, d_stage="run",
              stage="quote" if res["status"] == "quote" else res["status"])

def process_broker(text, was_audio=False):
    ss["d_chat"].append({"role": "user", "text": text})
    if not E.llm_ready():
        ss["d_chat"].append({"role": "bot", "text": "Chat intake needs GROQ_API_KEY. Use a sample load from the sidebar."})
        st.rerun()
    try:
        out = broker_turn(ss["d_chat"], ss["d_load"], text)
    except Exception as e:
        ss["d_chat"].append({"role": "bot", "text": f"(model error: {str(e)[:120]}) — try again or use a sample load."})
        st.rerun()
    raw = {**ss["d_load"], **{k: v for k, v in (out.get("load") or {}).items() if v not in (None, "")}}
    merged = E.normalise_load(raw)                      # anything that doesn't match our lists becomes missing
    said = E.pickup_phrase([m["text"] for m in ss["d_chat"] if m["role"] == "user"])
    if said and E.pickup_day(said) != E.pickup_day(merged.get("pickup_when")):
        merged["pickup_when"] = said                    # the broker's words win over the model's
    ss["d_load"] = {k: v for k, v in merged.items() if v not in (None, "")}
    miss = E.missing_fields(merged)
    reply = out.get("reply", "")
    if miss and (out.get("complete") or not reply):
        reply = "Ek cheez confirm karni hai — " + ASK[miss[0]]
    ss["d_chat"].append({"role": "bot", "text": reply})
    if not miss:
        if not E.find_lane(merged["origin"], merged["destination"]):
            ss["d_chat"].append({"role": "bot", "text": "That lane is not in my pilot network yet."})
        else:
            start_run(merged, was_audio, text if was_audio else None)
    st.rerun()

# ---------------- sidebar ----------------
with st.sidebar:
    if E.llm_ready():
        st.caption(f"Model: **Groq** · {E.LLM['model']} + Whisper")
    else:
        st.warning("No GROQ_API_KEY set: chat and voice are off; sample loads run on the deterministic planner.")
    st.markdown('<div class="sh">Live truck pool</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="cd">🟢 <b>Pool active</b> · owners who answered today’s 07:00 check-in'
                f'<div style="font-size:22px;font-weight:800;margin-top:4px;color:#17251e">{n_trucks}'
                f'<span style="font-size:12px;color:#6b7a72"> trucks</span> · {n_owners}'
                f'<span style="font-size:12px;color:#6b7a72"> owners</span></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="sh">Sample loads</div>', unsafe_allow_html=True)
    for q in [x for x in E.sample_inq if x.get("load")]:
        if st.button(f'{q["id"]} · {q["scenario"]}', use_container_width=True, key="s_" + q["id"]):
            desk_reset()
            start_run(E.normalise_load(q["load"]), q["channel"] == "voice", q.get("transcript") or q.get("text"))
            st.rerun()
    if ss["memory"].get("reliability"):
        st.caption("Learned today: " + ", ".join(f'{E.OWNERS_BY_ID[k]["name"]} {v:+}★'
                                                 for k, v in ss["memory"]["reliability"].items()))
    if st.button("↺  New conversation", use_container_width=True):
        desk_reset(); st.rerun()

# ---------------- tabs ----------------
tab_desk, tab_day, tab_real = st.tabs(["🖥️  Live desk", "📈  Day simulator", "🌍  In the real world"])

with tab_desk:
    head_ph = st.empty()
    cL, cM, cR = st.columns([1.15, 1.25, 0.95])
    with cL:
        st.markdown('<div class="sh">Broker ↔ Bhaada (WhatsApp)</div>', unsafe_allow_html=True)
        left_ph = st.empty(); left_ctrl = st.container()
    with cM:
        st.markdown('<div class="sh">Owner side — parallel chats</div>', unsafe_allow_html=True)
        mid_ph = st.empty()
    with cR:
        right_ph = st.empty()

    def header(level, res=None, stage="idle"):
        clk = clock_at(level, res, stage)
        extra = "" if level == 0 else '<div class="cp">manual today: 3-4 hrs</div>'
        head_ph.markdown(
            f'<div class="bh"><div class="lg">🚚</div>'
            f'<div><div class="nm">Bhaada</div><div class="sb">AGENTIC FREIGHT DESK</div></div>'
            f'<div class="sp">{extra}<div class="cp">Floor {MARGIN["floorPct"]}%</div>'
            f'<div class="cp">Target {MARGIN["targetPct"]}%</div>'
            f'<div class="clk{" idle" if level == 0 else ""}" title="Modelled real-world time from owner reply delays; '
            f'the demo runs faster">⏱ {clk} <span style="font-weight:600;font-size:10px">est. real-world</span></div>'
            f'</div></div>', unsafe_allow_html=True)

    if ss["d_stage"] == "collect":
        header(0)
        body = bub("in", "Namaste 🙏 send me a load — you can give details across messages, I remember everything.", "BHAADA")
        for mm in ss["d_chat"]:
            body += bub("out" if mm["role"] == "user" else "in", mm["text"], None if mm["role"] == "user" else "BHAADA")
        left_ph.markdown(wa_panel("Bhaada AI", "online", body), unsafe_allow_html=True)
        mid_ph.markdown(owners_html(0, None, "idle", None), unsafe_allow_html=True)
        right_ph.markdown(side_html(0, None, "idle", None), unsafe_allow_html=True)
        with left_ctrl:
            with st.form("chat_form", clear_on_submit=True):
                msg = st.text_input("m", label_visibility="collapsed",
                                    placeholder="e.g. 'Bhiwandi se Surat kal' … then '18 ton, 32 MXL'")
                sent = st.form_submit_button("Send", type="primary", use_container_width=True)
            if sent and msg.strip():
                process_broker(msg.strip())
            with st.expander("🎤 or send a voice note"):
                v = st.file_uploader("v", type=["m4a", "mp3", "wav", "ogg", "aac", "opus"], label_visibility="collapsed")
                if st.button("Send voice note", use_container_width=True) and v is not None:
                    try:
                        text = E.transcribe(v.getvalue(), getattr(v, "type", None) or "audio/mpeg")
                    except Exception as e:
                        st.error(f"Voice failed: {e}")
                    else:
                        process_broker(text, was_audio=True)
    else:
        def paint(level):
            header(level, ss["result"], ss["stage"])
            left_ph.markdown(broker_html(level, ss["result"], ss["msg"], ss["stage"], ss["replan"], ss["was_audio"]), unsafe_allow_html=True)
            mid_ph.markdown(owners_html(level, ss["result"], ss["stage"], ss["replan"]), unsafe_allow_html=True)
            right_ph.markdown(side_html(level, ss["result"], ss["stage"], ss["replan"]), unsafe_allow_html=True)

        PACE = {1: 0.7, 2: 0.9, 3: 0.8, 4: 0.9, 5: 1.2, 6: 1.0, 7: 0.9, 8: 0.5}
        if ss["animate"]:
            for lv in range(1, MAXLV + 1):
                paint(lv); time.sleep(PACE[lv])
            ss["animate"] = False
        else:
            paint(MAXLV)

        r = ss["result"]
        with left_ctrl:
            if r and r["status"] == "quote":
                if ss["stage"] == "quote":
                    a, b = st.columns(2)
                    if a.button("✅  Approve & hold truck", type="primary", use_container_width=True):
                        ss["stage"] = "booked"; st.rerun()
                    if b.button("✏️  Reject", use_container_width=True):
                        desk_reset(); st.rerun()
                elif ss["stage"] == "booked":
                    if st.button("⚠️  Owner backs out (4:02 PM)", use_container_width=True):
                        oid = r["owner"]["ownerId"]
                        before = r["owner"]["reliability"]
                        E.penalise(ss["memory"], oid)
                        new_pool = E.build_pool(ss["memory"])
                        after = next((p["reliability"] for p in new_pool if p["ownerId"] == oid), before)
                        with st.spinner("Orchestrator is re-planning…"):
                            rp = E.run_replan(r, new_pool)
                        rp.update(rel_before=before, rel_after=after)
                        ss["replan"] = rp; ss["stage"] = "replanned"; st.rerun()
                elif ss["stage"] == "replanned":
                    if st.button("↺  New inquiry", use_container_width=True):
                        desk_reset(); st.rerun()
            elif r and r["status"] != "quote":
                if st.button("↺  New inquiry", use_container_width=True):
                    desk_reset(); st.rerun()

# ---------------- day simulator ----------------
@st.cache_data
def day(n):
    return E.simulate_day(n)

with tab_day:
    st.markdown('<div class="sh">A full day of random loads, run through the same engine (deterministic planner, no API calls)</div>',
                unsafe_allow_html=True)
    n = st.slider("Inquiries in the day", 20, 60, 40, 5)
    sim = day(n)
    MAN_WORKED = round(n * 12 / 50)            # stated assumption: 12 of 50 properly worked today
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Inquiries received", sim["n"])
    c2.metric("Quoted by Bhaada", sim["quoted"], f"{sim['quoted'] - MAN_WORKED:+} vs manual (assumed {MAN_WORKED})")
    c3.metric("Escalated to broker", sim["escalated"], f"{sim['substituted']} truck-type substitutions", delta_color="off")
    c4.metric("Median booking-ready", f"{sim['median_min']} min", f"{sim['share_under_5']}% under 5 min (modelled)", delta_color="off")

    def bars(title, a_lb, a_v, b_lb, b_v, unit=""):
        mx = max(a_v, b_v) or 1
        ha, hb = int(76 * a_v / mx), int(76 * b_v / mx)
        return (f'<div class="cd"><div class="sh" style="margin-top:0">{title}</div><div class="cmp">'
                f'<div class="bar"><div class="v">{a_v}{unit}</div>'
                f'<div class="fillbox"><div class="fill" style="height:{ha}px;background:#c6cfc9"></div></div>'
                f'<div class="lb">{a_lb}</div></div>'
                f'<div class="bar"><div class="v">{b_v}{unit}</div>'
                f'<div class="fillbox"><div class="fill" style="height:{hb}px;background:#17533D"></div></div>'
                f'<div class="lb">{b_lb}</div></div></div></div>')

    d1, d2 = st.columns(2)
    d1.markdown(bars("Inquiries properly quoted", "Manual (assumed)", MAN_WORKED, "Bhaada", sim["quoted"]),
                unsafe_allow_html=True)
    d2.markdown(bars("Gross margin per quoted load", "Manual (assumed)", 7.0, "Bhaada (median)", sim["margin_med"], "%"),
                unsafe_allow_html=True)
    st.caption(f"Margin range on quoted loads: {sim['margin_min']}% to {sim['margin_max']}%. "
               f"The margin is policy, not a forecast: the guardrails ({MARGIN['floorPct']}% floor, {MARGIN['targetPct']}% target, "
               f"{MARGIN['ceilingPct']}% ceiling) decide it, and loads that can't clear the floor are escalated instead of quoted. "
               "The manual baseline (12 of 50 worked, 7% margin) is our stated Round 1 assumption. Data is synthetic.")

# ---------------- real world tab ----------------
with tab_real:
    st.markdown("""
<div class="cd">
<div class="sh" style="margin-top:0">A day in Rakesh's desk — what this looks like live</div>
<div class="row"><span><b>07:02</b></span><b>Pool build.</b></div>
<div style="font-size:13px;color:#2a3a32;margin:-2px 0 8px">Sourcing agent WhatsApps every opted-in owner: "Aaj gaadi khaali hai?" Replies become today's live pool. Owners install nothing.</div>
<div class="row"><span><b>09:40</b></span><b>Three inquiries land at once.</b></div>
<div style="font-size:13px;color:#2a3a32;margin:-2px 0 8px">A human handles them one by one. Bhaada opens three load sessions in parallel — each one messaging its own set of owners.</div>
<div class="row"><span><b>09:43</b></span><b>One is incomplete.</b></div>
<div style="font-size:13px;color:#2a3a32;margin:-2px 0 8px">Intake agent asks the shipper a Hinglish clarifying question instead of guessing. No wrong quote goes out.</div>
<div class="row"><span><b>09:45</b></span><b>One has no truck near the pickup.</b></div>
<div style="font-size:13px;color:#2a3a32;margin:-2px 0 8px">Agent escalates rather than inventing a rate. The broker keeps control of the edge cases.</div>
<div class="row"><span><b>09:47</b></span><b>One is ready to book.</b></div>
<div style="font-size:13px;color:#2a3a32;margin:-2px 0 8px">Several owners agreed, one declined, one went silent. The best option clearing the floor is quoted; Rakesh taps Approve once.</div>
<div class="row"><span><b>16:02</b></span><b>The owner backs out.</b></div>
<div style="font-size:13px;color:#2a3a32;margin:-2px 0 0">Agent re-confirms owners who agreed earlier, re-checks the floor at the shipper's fixed price and re-books — zero taps from Rakesh. That owner's reliability drops for next time.</div>
</div>
""", unsafe_allow_html=True)

    st.markdown(f"""
<div class="cd">
<div class="sh" style="margin-top:0">What's real here vs what's mocked</div>
<div class="row"><span>Orchestrator (plans, picks tools, re-plans)</span><b>Real — LLM tool-calling ({E.LLM['model']}), deterministic fallback</b></div>
<div class="row"><span>Intake (messy Hinglish → structured load)</span><b>Real — LLM, validated against known cities and types</b></div>
<div class="row"><span>Voice note → text</span><b>Real — Groq Whisper</b></div>
<div class="row"><span>Truck matching (capacity, availability, distance to pickup)</span><b>Real — deterministic code</b></div>
<div class="row"><span>Pricing &amp; margin guardrails</span><b>Real — deterministic code</b></div>
<div class="row"><span>Owner replies and haggling</span><b>Simulated personas (synthetic data)</b></div>
<div class="row"><span>Clock</span><b>Modelled from owner reply delays</b></div>
<div class="row"><span>WhatsApp transport</span><b>Mocked UI — production uses WhatsApp Cloud API</b></div>
</div>
""", unsafe_allow_html=True)

    st.markdown("""
<div class="cd">
<div class="sh" style="margin-top:0">To run this for a real broker</div>
<div class="row"><span>Owner &amp; truck master, bookings, rate history</span><b>Database (free tier at pilot scale)</b></div>
<div class="row"><span>Daily availability</span><b>Live WhatsApp replies each morning</b></div>
<div class="row"><span>Distances</span><b>Maps API, cached</b></div>
<div class="row"><span>Diesel &amp; tolls</span><b>Price feed / toll API</b></div>
<div class="row"><span>Channel</span><b>WhatsApp Business Cloud API (~₹0.12 per utility message)</b></div>
<div class="row tot"><span>Running cost, one broker</span><b>roughly ₹500–3,000 / month</b></div>
</div>
""", unsafe_allow_html=True)
