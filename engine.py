"""
Bhaada engine: data, deterministic pricing, truck matching, simulated owner replies,
and the agent orchestrator (LLM tool-calling loop with a deterministic fallback planner).

No Streamlit in here, so everything can be tested offline:  python test_engine.py
Prices are computed only in code (trip_cost / price_quote). The LLM decides which
tool to call next; it never produces a number that reaches the shipper.
"""
import json, math, os, re, time, hashlib, difflib, inspect
from pathlib import Path
import requests

DATA = Path(__file__).parent / "data"

def _load(name):
    with open(DATA / name, encoding="utf-8") as f:
        return json.load(f)

owners, trucks, lanes, rates = _load("owners.json"), _load("trucks.json"), _load("lanes.json"), _load("rates.json")
sample_inq, CITY = _load("sample_inquiries.json"), _load("cities.json")
MARGIN, SRC = rates["marginBand"], rates["sourcing"]
KNOWN_CITIES = sorted({l["origin"] for l in lanes} | {l["destination"] for l in lanes})
KNOWN_TYPES = list(rates["mileageKmPerLitre"].keys())
TRUCKS_BY_ID = {t["truckId"]: t for t in trucks}
OWNERS_BY_ID = {o["ownerId"]: o for o in owners}
REQUIRED = ["origin", "destination", "weight_ton", "truck_type"]

# =============================== LLM (Groq, OpenAI-compatible) ===============================
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO = "https://api.groq.com/openai/v1/audio/transcriptions"
LLM = {"key": os.getenv("GROQ_API_KEY", ""),
       "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
       "whisper": os.getenv("GROQ_WHISPER", "whisper-large-v3")}   # turbo garbles Hinglish city names

def local_secrets():
    """Read .streamlit/secrets.toml (simple KEY = "value" lines) for scripts that run outside Streamlit."""
    p = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if not p.exists():
        return {}
    return dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"', p.read_text(encoding="utf-8"), flags=re.MULTILINE))

def configure(key=None, model=None, whisper=None):
    """Set credentials from the app (st.secrets) instead of hardcoding them."""
    if key: LLM["key"] = key
    if model: LLM["model"] = model
    if whisper: LLM["whisper"] = whisper

def llm_ready():
    return bool(LLM["key"])

def _wait_secs(r):
    """Seconds Groq asks us to wait after a 429 (header or 'try again in 7.5s' in the message)."""
    try:
        return float(r.headers.get("retry-after"))
    except (TypeError, ValueError):
        m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", r.text)
        if not m:
            return 5.0
        secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
        return secs + 60 * int(m.group(1) or 0)

def chat(messages, tools=None, json_mode=False, timeout=25, retries=3, max_wait=30):
    """One chat completion with a timeout and retries, so a slow call cannot freeze the demo.
    On a rate limit (free tier: 8,000 tokens/min) it waits as long as Groq asks, up to max_wait seconds."""
    if not LLM["key"]:
        raise RuntimeError("GROQ_API_KEY is not set")
    body = {"model": LLM["model"], "temperature": 0, "messages": messages, "max_completion_tokens": 700}
    if "gpt-oss" in LLM["model"]:
        body["reasoning_effort"] = "low"          # fewer hidden reasoning tokens against the per-minute cap
    if tools:
        body["tools"], body["tool_choice"] = tools, "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    last = None
    for i in range(retries):
        try:
            r = requests.post(GROQ_CHAT, headers={"Authorization": f"Bearer {LLM['key']}"},
                              json=body, timeout=timeout)
            if r.status_code == 429:
                wait = _wait_secs(r)
                last = RuntimeError(f"Groq rate limit (wait {wait:.0f}s)")
                if wait > max_wait:
                    break
                time.sleep(wait + 0.5)
                continue
            if not r.ok:
                raise RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last

def gen(prompt, json_mode=False):
    return chat([{"role": "user", "content": prompt}], json_mode=json_mode).get("content") or ""

def parse_json(t):
    t = re.sub(r"^```[a-zA-Z]*|```$", "", (t or "").strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    return json.loads(m.group(0) if m else t)

def voice_hint():
    """Domain vocabulary that stops Whisper guessing at Indian freight words."""
    return ("Indian road-freight load request, Hinglish. "
            f"Cities: {', '.join(KNOWN_CITIES)}. Truck types: {', '.join(KNOWN_TYPES)}. "
            "Words: gaadi, kapda, bhada, ton, chahiye, kal subah, parso, se, tak, MXL, SXL, Taurus, 10 tyre.")

HINDI_HINT = ("भिवंडी से सूरत, कल सुबह, 14 टन कपड़ा, 32 फुट MXL गाड़ी चाहिए। मुंबई, पुणे, दिल्ली, अहमदाबाद, जयपुर, इंदौर, "
              "नागपुर। भाड़ा, रेट कितना है, 40 रुपये प्रति किलोमीटर, हाँ।")   # longer hints dilute it: Bhiwandi came out धेवंदी

AUDIO_EXT = {"ogg": ".ogg", "opus": ".ogg", "mpeg": ".mp3", "mp3": ".mp3", "mp4": ".m4a", "m4a": ".m4a",
             "aac": ".m4a", "wav": ".wav", "webm": ".webm", "amr": ".amr"}

def transcribe(audio_bytes, mime="audio/mpeg", timeout=60, retries=2, name=None):
    """name: the sender's file name, if any; its extension wins over the mime type."""
    if not LLM["key"]:
        raise RuntimeError("GROQ_API_KEY is not set")
    tail = (name or "").rsplit(".", 1)[-1].lower() if name and "." in name else ""
    ext = AUDIO_EXT.get(tail) or next((v for k, v in AUDIO_EXT.items() if k in (mime or "")), ".ogg")
    last = None
    for i in range(retries):
        try:
            # Hindi script, as the speaker said it; the Devanagari freight hint keeps city names right
            r = requests.post(GROQ_AUDIO, headers={"Authorization": f"Bearer {LLM['key']}"},
                              files={"file": ("note" + ext, audio_bytes, mime)},
                              data={"model": LLM["whisper"], "response_format": "verbose_json",
                                    "prompt": HINDI_HINT, "language": "hi"}, timeout=timeout)
            r.raise_for_status()
            out = r.json()
            break
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    else:
        raise last
    text = (out.get("text") or "").strip()
    segs = out.get("segments") or []
    tokens = lambda x: re.findall(r"[^\s.,।!?;:'\"()\-]+", x.lower())
    words = tokens(text)
    echo = words and " ".join(words) in " ".join(tokens(HINDI_HINT))
    silent = segs and all(sg.get("no_speech_prob", 0) > 0.5 for sg in segs)
    # on a silent or very quiet note Whisper returns "." or repeats the tail of the vocabulary prompt
    return "" if (len(words) == 0 or echo or silent) else text

# =============================== normalising what the LLM extracted ===============================
CITY_ALIASES = {"bombay": "Mumbai", "bangalore": "Bengaluru", "bengaluru": "Bengaluru",
                "new delhi": "Delhi", "ncr": "Delhi", "calcutta": "Kolkata", "madras": "Chennai"}

CITY_RESOLVER = None      # set by the live desk to geo.resolve_city: any Indian city, not only the seeded list

def norm_city(s):
    if not s:
        return None
    t = str(s).strip().lower()
    for c in KNOWN_CITIES:
        if t == c.lower():
            return c
    # "Bhiwandi (Mumbai)" -> Bhiwandi: the first known name that appears in the text wins
    hits = sorted((t.find(c.lower()), c) for c in KNOWN_CITIES if c.lower() in t)
    if hits:
        return hits[0][1]
    for a, c in CITY_ALIASES.items():
        if a in t:
            return c
    m = difflib.get_close_matches(str(s).strip().title(), KNOWN_CITIES, 1, 0.8)
    if m:
        return m[0]
    if CITY_RESOLVER:
        hit = CITY_RESOLVER(s)
        return hit["name"] if hit else None
    return None

def norm_type(s):
    """Map '32 MXL', '10 tyre', '22ft' etc. to a known type. Ambiguous ('32 ft') returns None so we ask."""
    if not s:
        return None
    if s in KNOWN_TYPES:
        return s
    t = str(s).lower()
    if "taurus" in t or re.search(r"10[\s-]*(tyre|tire|wheel)", t):
        return "Taurus (10-tyre)"
    if "container" in t:
        return "20 ft container"
    if re.search(r"\b407\b", t):                          # Tata 407: the common 14 ft body
        return "14 ft"
    m = re.search(r"(\d{2})", t)
    if m:
        cands = [k for k in KNOWN_TYPES if k.startswith(m.group(1) + " ft") and "container" not in k]
        if len(cands) == 1:
            return cands[0]
        if "mxl" in t or "multi" in t:
            return next((k for k in cands if "MXL" in k), None)
        if "sxl" in t or "single" in t:
            return next((k for k in cands if "SXL" in k), None)
        return None
    c = difflib.get_close_matches(str(s), KNOWN_TYPES, 1, 0.6)
    return c[0] if c else None

def norm_weight(x):
    if x in (None, ""):
        return None
    m = re.search(r"\d+(\.\d+)?", str(x))
    return float(m.group(0)) if m else None

def norm_tons(x):
    """'18000 kg' or a bare 18000 is kilograms, '150 quintal' is 15 T: no truck carries 100 T."""
    v = norm_weight(str(x).replace(",", "") if x not in (None, "") else x)
    if v is None:
        return None
    s = str(x).lower()
    if "quintal" in s:
        v /= 10
    elif "kg" in s or "kilo" in s or v >= 100:
        v /= 1000
    return round(v, 2) or None

MONEY_MULT = [(r"crore|\bcr\b", 1e7), (r"lakh|lac|\bl\b", 1e5), (r"\d\s*k\b|hazaa?r|thousand", 1e3)]

def norm_money(x):
    """'25k', '1.2 lakh', '35 hazaar', '1,20,000' -> rupees. A per-km figure stays small; the desk converts it."""
    if isinstance(x, (int, float)):
        return float(x)
    v = norm_weight(str(x).replace(",", "") if x not in (None, "") else x)
    if v is None:
        return None
    s = str(x).lower()
    if not re.search(r"/\s*km|per\s*km|prati", s):
        for pat, mult in MONEY_MULT:
            if re.search(pat, s):
                return v * mult
    return v

def normalise_load(load):
    """Validate every LLM-extracted field against the known lists; anything unmatched becomes missing."""
    out = dict(load or {})
    out["origin"] = norm_city(out.get("origin"))
    out["destination"] = norm_city(out.get("destination"))
    out["truck_type"] = norm_type(out.get("truck_type"))
    out["weight_ton"] = norm_tons(out.get("weight_ton"))
    out["budget_rs"] = norm_money(out.get("budget_rs"))
    return out

def missing_fields(load):
    return [k for k in REQUIRED if not load.get(k)]

# =============================== conversational intake (shared by the web desk and WhatsApp) ===============================
def _hist(h):
    who = {"user": "Broker", "owner": "Owner", "bot": "Bhaada", "agent": "Bhaada"}
    return "\n".join(f'{who.get(m["role"], m["role"])}: {m["text"]}' for m in h) or "(start of chat)"

ASK = {"origin": "Kahan se load uthana hai?", "destination": "Kahan tak jaana hai?",
       "weight_ton": "Kitna ton hai?",
       "truck_type": "Kaunsi gaadi chahiye? (" + ", ".join(KNOWN_TYPES[:6]) + " …)"}

def intake_turn(hist, load, user_msg):
    """One broker message -> (merged load, reply, missing fields). The LLM collects the load across turns;
    code validates every field and keeps the broker's own words for the pickup day.
    `hist` must already include the latest user message."""
    prompt = f"""You are Bhaada, an AI freight desk assistant chatting with a BROKER on WhatsApp.
Collect ONE complete load over the conversation. REMEMBER everything already said - never re-ask a detail you were given.
Common cities: {", ".join(KNOWN_CITIES)} (any other Indian city is fine: write its proper name).
Truck types (use these exact names): {", ".join(KNOWN_TYPES)}
Load known so far (may be partial): {json.dumps(load)}
Conversation so far:
{_hist(hist)}
Latest broker message: "{user_msg}"
Merge new details into the load; keep existing values unless the broker corrects them.
Required fields: origin, destination, weight_ton, truck_type. Optional: pickup_when, budget_rs (shipper's budget in rupees, number).
Return ONLY JSON:
{{"reply":"<one or two short lines>","load":{{"origin":null,"destination":null,"weight_ton":null,"truck_type":null,"pickup_when":null,"budget_rs":null}},"missing":["..."],"complete":false}}
Fill load with everything known so far. If all required fields are known set complete=true and reply that you are finding the best truck now.
If anything required is missing, the reply must ASK for exactly those details (e.g. "Kahan se kahan tak?") and must not say you are finding a truck.
Reply language: always Hinglish - Hindi words in Roman script, like "Theek hai, kitne ton maal hai?" - even if the broker wrote in English or in Devanagari. Never reply in plain English."""
    out = parse_json(gen(prompt, json_mode=True))
    raw = {**(load or {}), **{k: v for k, v in (out.get("load") or {}).items() if v not in (None, "")}}
    merged = normalise_load(raw)                      # anything that doesn't match our lists becomes missing
    said = pickup_phrase([m["text"] for m in hist if m["role"] == "user"])
    if said and pickup_day(said) != pickup_day(merged.get("pickup_when")):
        merged["pickup_when"] = said                  # the broker's words win over the model's
    miss = missing_fields(merged)
    reply = out.get("reply", "")
    if miss and (out.get("complete") or "?" not in reply):    # a reply that doesn't ask can't move the load forward
        reply = "Ek cheez confirm karni hai — " + ASK[miss[0]]
    if not miss:      # read back what will be booked, from the validated fields rather than the model's wording
        reply = (f"Theek hai ✅ {merged['origin']} → {merged['destination']}, {merged['weight_ton']:g} T, {merged['truck_type']}, "
                 f"{merged.get('pickup_when') or 'kal'}" + (f", budget ₹{merged['budget_rs']:,.0f}" if merged.get("budget_rs") else "")
                 + ". Owners ko message kar raha hoon.")
    return merged, reply, miss

# =============================== geography and dates ===============================
def find_lane(o, d):
    return next((l for l in lanes if l["origin"] == o and l["destination"] == d), None)

def road_km(a, b):
    """Road km between two cities: lane table if we have it, else straight line x road factor."""
    if a == b:
        return 0
    lane = find_lane(a, b) or find_lane(b, a)
    if lane:
        return lane["distanceKm"]
    pa, pb = CITY["cities"].get(a), CITY["cities"].get(b)
    if not pa or not pb:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (*pa, *pb))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)) * CITY["roadFactor"])

HINDI_DAYS = {"परसों": "parso", "परसो": "parso", "आज": "aaj", "कल": "kal", "सुबह": "subah", "शाम": "shaam", "अभी": "abhi"}

def _roman(text):
    """Hindi-script voice notes: the day words the pickup rules look for, in Latin."""
    t = (text or "").lower()
    for k, v in HINDI_DAYS.items():
        t = t.replace(k, v)
    return t

def pickup_day(text):
    """0 = today, 1 = tomorrow, 2 = day after. Defaults to tomorrow."""
    t = _roman(text)
    if re.search(r"parso|day after|2 din", t):
        return 2
    if re.search(r"\baaj\b|today|abhi|tonight|aaj hi", t):
        return 0
    return 1

def pickup_phrase(texts):
    """The broker's own words for the pickup day, latest message first, so a date the LLM dropped isn't lost."""
    for t in reversed(texts):
        m = re.search(r"\b(aaj hi|aaj|today|tonight|abhi|kal subah|kal shaam|kal|tomorrow|parso|day after tomorrow|day after)\b",
                      _roman(t))
        if m:
            return m.group(1)
    return None

AVAIL_DAY = {"today": 0, "tomorrow": 1, "in 2 days": 2}

# =============================== deterministic pricing ===============================
def fair_cost(d, tt, rp):
    """Market cost of the loaded trip for this truck type. This is what the sell price is built on."""
    fuel = d / rates["mileageKmPerLitre"][tt] * rates["dieselPerLitre"]
    toll = d * rates["tollPerKmByAxle"][str(rates["axlesByType"][tt])]
    driver = max(1, math.ceil(d / rates["avgSpeedKmPerDay"])) * rates["driverCostPerDay"]
    cost = fuel + toll + driver + rates["handlingMiscPerTrip"]
    if rp >= 0.6:
        cost *= (1 - rates["returnLoadDiscountPct"] / 100)
    return {"fuel": round(fuel), "toll": round(toll), "driver": round(driver),
            "misc": rates["handlingMiscPerTrip"], "est_cost": round(cost)}

def deadhead_cost(km, tt):
    """Owner's cost of driving empty to the pickup point: fuel + tolls. Owners price it into their ask."""
    per_km = rates["dieselPerLitre"] / rates["mileageKmPerLitre"][tt] + rates["tollPerKmByAxle"][str(rates["axlesByType"][tt])]
    return round(km * per_km)

def target_sell(lane, tt):
    return fair_cost(lane["distanceKm"], tt, lane["returnLoadProb"])["est_cost"] / (1 - MARGIN["targetPct"] / 100)

def price_quote(lane, tt, buy, fixed_sell=None, budget=None):
    """Sell price = cost-plus at target margin, capped at the ceiling, matched to the shipper's budget
    only if the floor still holds. After booking, the shipper price is fixed (re-plan)."""
    br = fair_cost(lane["distanceKm"], tt, lane["returnLoadProb"])
    note = ""
    if fixed_sell:
        sell = fixed_sell
        note = "shipper price fixed at booking"
    else:
        sell = br["est_cost"] / (1 - MARGIN["targetPct"] / 100)
        cap = buy / (1 - MARGIN["ceilingPct"] / 100)
        if sell > cap:
            sell, note = cap, f"capped at {MARGIN['ceilingPct']}% ceiling"
        if budget and budget < sell:
            if (budget - buy) / budget * 100 >= MARGIN["floorPct"]:
                sell, note = budget, "matched shipper budget"
            else:
                sell = max(budget, buy / (1 - MARGIN["floorPct"] / 100))
                note = f"shipper budget ₹{budget:,.0f} is below the floor; quoting the floor price"
    m = sell - buy
    return {**br, "buy_total": round(buy), "sell_total": round(sell), "margin_rs": round(m),
            "margin_pct": round(m / sell * 100, 1), "rate_per_km": round(sell / lane["distanceKm"], 1),
            "above_floor": (m / sell * 100) >= MARGIN["floorPct"], "note": note}

def type_check(load):
    """Can the requested truck type carry the weight? If not, suggest the next types that can."""
    tt, w = load.get("truck_type"), load.get("weight_ton") or 0
    cap = rates["capacityTonByType"].get(tt, 0)
    ok = cap >= w
    suggest = [] if ok else sorted((k for k, c in rates["capacityTonByType"].items() if c >= w),
                                   key=lambda k: rates["capacityTonByType"][k])
    return {"requested_type": tt, "capacity_ton": cap, "weight_ton": w, "fits": ok, "suggest": suggest}

# =============================== truck pool and matching ===============================
def build_pool(memory=None):
    """Today's pool: trucks of owners who answered the morning check-in. Reliability includes
    what the desk has learned (memory), e.g. a penalty after a back-out."""
    rel = (memory or {}).get("reliability", {})
    pool = []
    for o in owners:
        if not o["behavior"]["respondsToCheckin"]:
            continue
        for tid in o["truckIds"]:
            t = TRUCKS_BY_ID.get(tid)
            if t:
                pool.append({"truckId": tid, "ownerId": o["ownerId"], "ownerName": o["name"], "type": t["type"],
                             "capacityTon": t["capacityTon"], "currentCity": t["currentCity"],
                             "availableFrom": t["availableFrom"],
                             "reliability": round(max(1.0, o["reliability"] + rel.get(o["ownerId"], 0)), 1),
                             "ratePremium": o["behavior"]["ratePremium"], "minRateMult": o["behavior"]["minRateMult"],
                             "responseDelaySec": o["behavior"]["responseDelaySec"],
                             "honoursHoldProb": o["behavior"]["honoursHoldProb"]})
    return pool

def find_candidates(pool, load, truck_type=None, max_deadhead=None, exclude=(), prefer=(), n=8):
    """Trucks that can actually do this load: right type, enough capacity, free by pickup day,
    and close enough to the origin. Ranked: owners already on this load, then nearest, then most reliable."""
    w, day = load.get("weight_ton") or 0, pickup_day(load.get("pickup_when"))
    max_dh = SRC["maxDeadheadKm"] if max_deadhead is None else max_deadhead
    out = []
    for p in pool:
        if p["ownerId"] in exclude or p["capacityTon"] < w:
            continue
        if truck_type and p["type"] != truck_type:
            continue
        if AVAIL_DAY.get(p["availableFrom"], 1) > day:
            continue
        dh = road_km(p["currentCity"], load["origin"])
        if dh is None or dh > max_dh:
            continue
        out.append({**p, "deadheadKm": dh})
    out.sort(key=lambda p: (0 if p["ownerId"] in prefer else 1, p["deadheadKm"], -p["reliability"]))
    seen, res = set(), []
    for p in out:                      # one truck per owner
        if p["ownerId"] in seen:
            continue
        seen.add(p["ownerId"]); res.append(p)
        if len(res) >= n:
            break
    return res

# =============================== simulated owner replies ===============================
# Stand-in for real WhatsApp replies. Deterministic per owner and load (so demos repeat),
# but different owners go silent or hold firm on different loads.
def _h(*parts):
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest(), 16)

def owner_reply(p, load, lane, round_no, sell, reconfirm=False, prev_buy=None):
    km = lane["distanceKm"]
    loaded = fair_cost(km, p["type"], lane["returnLoadProb"])["est_cost"]
    owner_cost = loaded + deadhead_cost(p["deadheadKm"], p["type"]) * SRC["deadheadChargedShare"]
    floor_buy = sell * (1 - MARGIN["floorPct"] / 100)
    target_buy = sell * (1 - MARGIN["targetPct"] / 100)
    when = load.get("pickup_when") or "kal"
    seed = _h(p["ownerId"], lane["laneId"], load.get("weight_ton"), round_no)
    chat = []
    if reconfirm:
        chat.append(("out", f"Bhai, {load['origin']} → {load['destination']} ka load abhi bhi hai. "
                            f"Aapka ₹{round(prev_buy / km, 1)}/km wala rate — gaadi free hai?"))
        if (seed % 100) / 100 < p["honoursHoldProb"]:
            chat.append(("in", "Haan, gaadi free hai. Bhej do details. 🚛"))
            deal = {"deal": prev_buy <= floor_buy, "buy": round(prev_buy), "owner_quote": round(prev_buy), "rounds": 1}
            return _thread(p, deal, "deal" if deal["deal"] else "declined", prev_buy / km, prev_buy / km, chat, owner_cost)
        chat.append(("in", "Sorry bhai, gaadi doosre load pe nikal gayi."))
        return _thread(p, {"deal": False, "buy": None, "owner_quote": None, "rounds": 1}, "declined", None, None, chat, owner_cost)

    chat.append(("out", f"Namaste 🙏 {load['origin']} → {load['destination']} {when}, "
                        f"{load['weight_ton']:g}T, {p['type']}. Gaadi free hai? Rate?"))
    if p["responseDelaySec"] > SRC["ownerReplyTimeoutSec"] or seed % 10 == 0:
        return _thread(p, {"deal": False, "buy": None, "owner_quote": None, "rounds": 0}, "silent", None, None, chat, owner_cost)
    ask, mn = owner_cost * p["ratePremium"], owner_cost * p["minRateMult"]
    firm = p["ratePremium"] > 1.08
    if ask <= target_buy:
        agreed, rounds = ask, 1
    elif firm:
        agreed, rounds = ask, 2
    else:                               # flexible owners concede ~65% of the gap, never below their minimum
        agreed, rounds = max(mn, target_buy + (ask - target_buy) * 0.35), 2
    ask_km = round(ask / km, 1)
    chat.append(("in", f"₹{ask_km}/km lagega." + (f" Gaadi {p['currentCity']} mein hai, {p['deadheadKm']} km khaali aana padega."
                                                     if p["deadheadKm"] else "")))
    if agreed <= floor_buy:
        final = round(agreed / km, 1)
        if rounds >= 2:
            chat += [("out", f"Thoda kam karo — ₹{final}/km pe pakka? Regular load denge 👍"),
                     ("in", f"Theek hai, ₹{final}/km. 🚛")]
        else:
            chat += [("out", "Perfect 👍 rate theek hai.")]
        return _thread(p, {"deal": True, "buy": round(agreed), "owner_quote": round(ask), "rounds": rounds},
                       "deal", ask_km, final, chat, owner_cost)
    chat += [("out", f"Itne mein margin nahi banta. ₹{round(target_buy / km, 1)}/km ho payega?"),
             ("in", "Nahi bhai, isse kam nahi hoga.")]
    return _thread(p, {"deal": False, "buy": None, "owner_quote": round(ask), "rounds": rounds},
                   "declined", ask_km, None, chat, owner_cost)

def _thread(p, deal, status, ask, final, chat, fair):
    return {"owner": p, "deal": deal, "status": status, "ask": round(ask, 1) if ask else None,
            "final": round(final, 1) if final else None, "delay": p["responseDelaySec"], "chat": chat, "fair": round(fair)}

# =============================== the load session: tools the orchestrator can call ===============================
class LoadSession:
    def __init__(self, load, lane, pool, fixed_sell=None, exclude=(), prefer=None, mode="quote"):
        self.load, self.lane, self.pool, self.mode = load, lane, pool, mode
        self.fixed_sell, self.exclude = fixed_sell, set(exclude)
        self.prefer = dict(prefer or {})            # ownerId -> earlier agreed buy (re-plan)
        self.threads, self.quotes, self.rounds = {}, {}, []
        self.trace, self.final, self.search_type = [], None, load.get("truck_type")

    # ---- tools ----
    def check_load(self):
        tc = type_check(self.load)
        if tc["fits"]:
            self.search_type = self.load["truck_type"]
        out = {"origin": self.load["origin"], "destination": self.load["destination"],
               "distance_km": self.lane["distanceKm"], "pickup_day": pickup_day(self.load.get("pickup_when")),
               **tc, "shipper_budget_rs": self.load.get("budget_rs")}
        if self.fixed_sell:
            out["fixed_shipper_price_rs"] = round(self.fixed_sell)
        return out

    def find_trucks(self, truck_type=None, max_deadhead_km=None):
        tt = norm_type(truck_type) or self.search_type
        if tt and rates["capacityTonByType"].get(tt, 0) < (self.load.get("weight_ton") or 0):
            return {"error": f"{tt} carries max {rates['capacityTonByType'][tt]} T; load is {self.load['weight_ton']} T. "
                             "Pick a type from check_load.suggest."}
        self.search_type = tt
        mdh = min(int(max_deadhead_km or SRC["maxDeadheadKm"]), 2 * SRC["maxDeadheadKm"])
        c = find_candidates(self.pool, self.load, tt, mdh, exclude=self.exclude | set(self.threads),
                            prefer=self.prefer, n=8)
        return {"truck_type": tt, "max_deadhead_km": mdh, "candidates": [
            {"owner_id": p["ownerId"], "km_to_pickup": p["deadheadKm"], "rel": p["reliability"],
             **({"quoted_earlier": True} if p["ownerId"] in self.prefer else {})} for p in c]}

    def message_owners(self, owner_ids):
        if len(self.rounds) >= 3:
            return {"error": "Already ran 3 broadcast rounds on this load. Price what you have or escalate."}
        ids = [i for i in (owner_ids or []) if i not in self.threads and i not in self.exclude][:SRC["ownersPerBroadcast"]]
        tt = self.search_type
        cands = {p["ownerId"]: p for p in find_candidates(self.pool, self.load, tt, 2 * SRC["maxDeadheadKm"],
                                                          exclude=self.exclude | set(self.threads), n=50)}
        ids = [i for i in ids if i in cands]
        if not ids:
            return {"error": "None of those owners is a valid candidate. Call find_trucks first."}
        rnd = len(self.rounds) + 1
        replies = []
        for i in ids:
            p = cands[i]
            sell = self.fixed_sell or target_sell(self.lane, p["type"])
            t = owner_reply(p, self.load, self.lane, rnd, sell,
                            reconfirm=i in self.prefer, prev_buy=self.prefer.get(i))
            t["round"] = rnd
            self.threads[i] = t
            r = {"owner_id": i, "status": t["status"], "rel": p["reliability"]}
            if t["status"] == "deal":
                q = price_quote(self.lane, p["type"], t["deal"]["buy"], self.fixed_sell, self.load.get("budget_rs"))
                self.quotes[i] = q
                r.update(margin_pct=q["margin_pct"], above_floor=q["above_floor"])
            replies.append(r)
        self.rounds.append(ids)
        return {"round": rnd, "replies": replies}

    def price(self, owner_id):
        t = self.threads.get(owner_id)
        if not t or t["status"] != "deal":
            return {"error": f"{owner_id} has not agreed a rate. Price only owners with status 'deal'."}
        q = price_quote(self.lane, t["owner"]["type"], t["deal"]["buy"], self.fixed_sell, self.load.get("budget_rs"))
        self.quotes[owner_id] = q
        return {"owner_id": owner_id, "owner": t["owner"]["ownerName"], "reliability": t["owner"]["reliability"],
                **{k: q[k] for k in ("buy_total", "sell_total", "margin_rs", "margin_pct", "above_floor", "note")},
                "floor_pct": MARGIN["floorPct"]}

    def propose_quote(self, owner_id, reason=""):
        if owner_id not in self.quotes:
            r = self.price(owner_id)
            if "error" in r:
                return r
        if not self.quotes[owner_id]["above_floor"]:
            return {"error": f"Margin {self.quotes[owner_id]['margin_pct']}% is below the {MARGIN['floorPct']}% floor. "
                             "Choose another owner or escalate."}
        self.final = {"action": "propose", "owner_id": owner_id, "reason": reason}
        return {"ok": True}

    def escalate(self, reason=""):
        self.final = {"action": "escalate", "reason": reason}
        return {"ok": True}

    def call(self, name, args, thought=""):
        fn = {"check_load": self.check_load, "find_trucks": self.find_trucks, "message_owners": self.message_owners,
              "price_quote": self.price, "propose_quote": self.propose_quote, "escalate": self.escalate}.get(name)
        args = args if isinstance(args, dict) else {}
        if fn:
            allowed = inspect.signature(fn).parameters
            args = {k: v for k, v in args.items() if k in allowed}
        try:
            out = fn(**args) if fn else {"error": f"unknown tool {name}"}
        except TypeError as e:
            out = {"error": f"bad arguments for {name}: {e}"}
        self.trace.append({"tool": name, "args": args or {}, "thought": thought, "summary": _summarise(name, out)})
        return out

def _summarise(name, out):
    if "error" in out:
        return "⚠ " + out["error"]
    if name == "check_load":
        s = f"{out['origin']} → {out['destination']}, {out['distance_km']} km"
        return s + ("" if out["fits"] else f"; {out['requested_type']} can't carry {out['weight_ton']} T → try {out['suggest'][:1]}")
    if name == "find_trucks":
        return f"{len(out['candidates'])} {out['truck_type'] or 'any-type'} trucks within {out['max_deadhead_km']} km"
    if name == "message_owners":
        st = [r["status"] for r in out["replies"]]
        ok = sum(1 for r in out["replies"] if r.get("above_floor"))
        return (f"round {out['round']}: {st.count('deal')} agreed ({ok} above floor), "
                f"{st.count('declined')} declined, {st.count('silent')} silent")
    if name == "price_quote":
        return f"{out['owner']}: margin {out['margin_pct']}%" + (" ✓" if out["above_floor"] else " ✗ below floor")
    if name in ("propose_quote", "escalate"):
        return "done"
    return ""

TOOLS = [
    {"type": "function", "function": {"name": "check_load", "description": "Validate the load: distance, pickup day, whether the requested type carries the weight (suggest = types that can), budget, fixed shipper price.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "find_trucks", "description": "Search the pool for trucks that can do this load. Excludes owners already contacted.", "parameters": {"type": "object", "properties": {"truck_type": {"type": "string"}, "max_deadhead_km": {"type": "integer", "description": f"empty km to pickup, default {SRC['maxDeadheadKm']}, max {2 * SRC['maxDeadheadKm']}"}}}}},
    {"type": "function", "function": {"name": "message_owners", "description": f"WhatsApp up to {SRC['ownersPerBroadcast']} owners and negotiate. Returns status (deal/declined/silent) and, for deals, margin_pct and above_floor.", "parameters": {"type": "object", "properties": {"owner_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["owner_ids"]}}},
    {"type": "function", "function": {"name": "price_quote", "description": "Full price breakdown for an owner who agreed.", "parameters": {"type": "object", "properties": {"owner_id": {"type": "string"}}, "required": ["owner_id"]}}},
    {"type": "function", "function": {"name": "propose_quote", "description": "Finish with this owner (quote to broker, or book in re-plan). Rejected below the floor.", "parameters": {"type": "object", "properties": {"owner_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["owner_id", "reason"]}}},
    {"type": "function", "function": {"name": "escalate", "description": "Finish by handing the load to the human broker.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
]

SYSTEM_QUOTE = f"""You orchestrate Bhaada, an AI freight desk for an Indian truck broker. Goal: a quote where an owner agreed and margin >= {MARGIN['floorPct']}% floor.
Act only through tools; never invent numbers. Tools take only the arguments in their schema.
1. check_load. If fits is false, search suggest[0] and mention the substitution in your reason; if suggest is empty, escalate at once.
2. find_trucks, then message_owners with the top {SRC['ownersPerBroadcast']} owner_ids.
3. If no reply is above_floor: find_trucks again (raise max_deadhead_km up to {2 * SRC['maxDeadheadKm']}) and message new owners. Max 3 rounds.
4. propose_quote(owner_id, reason) on the highest margin_pct above floor; you may prefer rel >= 0.3 higher if the margin gap < 1 point. Otherwise escalate(reason).
Reasons: one short sentence for the broker."""

SYSTEM_REPLAN = f"""You orchestrate Bhaada, an AI freight desk for an Indian truck broker. The booked owner backed out; the shipper price is fixed (check_load shows it).
Goal: re-book with margin >= {MARGIN['floorPct']}% floor, without the broker. Act only through tools; never invent numbers. Tools take only the arguments in their schema.
1. check_load, then find_trucks. Message owners with quoted_earlier first, in one message_owners call with the other top candidates.
2. If no reply is above_floor, find_trucks again and message new owners (you may try a larger truck type from suggest or any type that carries the weight). Max 3 rounds.
3. propose_quote(owner_id, reason) on the best margin above floor, else escalate(reason)."""

# =============================== planners ===============================
def plan_deterministic(sess):
    """Fallback planner: same tools, fixed strategy. Used when no LLM key, the LLM fails, or for the day simulator."""
    chk = sess.call("check_load", {}, "fallback planner: validate the load")
    tt = None if chk["fits"] else (chk["suggest"][0] if chk["suggest"] else None)
    if not chk["fits"] and not tt:
        return sess.call("escalate", {"reason": f"No truck type in the network carries {chk['weight_ton']} T."})
    first = tt or chk["requested_type"]
    types = [first]
    if sess.mode == "replan":            # shipper price is fixed, so a bigger truck costs the shipper nothing
        types += sorted((k for k, c in rates["capacityTonByType"].items() if c >= chk["weight_ton"] and k != first),
                        key=lambda k: fair_cost(sess.lane["distanceKm"], k, sess.lane["returnLoadProb"])["est_cost"])
    searches = [(first, SRC["maxDeadheadKm"]), (first, SRC["maxDeadheadKm"]), (first, 2 * SRC["maxDeadheadKm"])]
    searches += [(k, 2 * SRC["maxDeadheadKm"]) for k in types[1:]]
    for t_type, mdh in searches:
        if len(sess.rounds) >= 3:
            break
        ft = sess.call("find_trucks", {"truck_type": t_type, "max_deadhead_km": mdh})
        ids = [c["owner_id"] for c in ft.get("candidates", [])][:SRC["ownersPerBroadcast"]]
        if not ids:
            continue
        sess.call("message_owners", {"owner_ids": ids})

        ok = [(q["margin_pct"], i) for i, q in sess.quotes.items() if q["above_floor"]]
        if ok:
            best = max(ok)[1]
            used = sess.threads[max(ok)[1]]["owner"]["type"]
            sub = "" if used == chk["requested_type"] else f" Used a {used} instead of {chk['requested_type']}."
            return sess.call("propose_quote", {"owner_id": best, "reason": f"Best margin that clears the floor.{sub}"})
    if not sess.threads:
        return sess.call("escalate", {"reason": "No suitable truck is free near the pickup point by the pickup day."})
    return sess.call("escalate", {"reason": "No owner agreed a rate that clears the margin floor."})

def plan_llm(sess, max_steps=12):
    system = SYSTEM_REPLAN if sess.mode == "replan" else SYSTEM_QUOTE
    user = json.dumps({"load": {k: sess.load.get(k) for k in ("origin", "destination", "weight_ton", "truck_type",
                                                              "pickup_when", "budget_rs")}})
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(max_steps):
        msg = chat(messages, tools=TOOLS)
        calls = msg.get("tool_calls") or []
        thought = (msg.get("content") or msg.get("reasoning") or "").strip()
        if not calls:
            messages += [{"role": "assistant", "content": msg.get("content") or ""},
                         {"role": "user", "content": "Continue with a tool call. Finish with propose_quote or escalate."}]
            continue
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["function"]["name"],
                                                      "arguments": c["function"].get("arguments") or "{}"}}
                                        for c in calls]})
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out = sess.call(c["function"]["name"], args, thought[:220])
            thought = ""
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out, ensure_ascii=False)})
        if sess.final:
            return
    raise RuntimeError("orchestrator did not finish within the step limit")

def _run(make_session, use_llm):
    t0 = time.time()
    planner = "llm"
    sess = make_session()
    if use_llm and llm_ready():
        try:
            plan_llm(sess)
        except Exception as e:
            sess = make_session()           # owner replies are deterministic, so a clean re-run is safe
            sess.trace.append({"tool": "fallback", "args": {}, "thought": "",
                               "summary": f"LLM orchestrator unavailable ({str(e)[:80]}) → deterministic planner"})
            planner = "fallback"
            plan_deterministic(sess)
    else:
        planner = "deterministic"
        plan_deterministic(sess)
    return sess, planner, round(time.time() - t0, 1)

# =============================== modelled real-world clock ===============================
def modelled_timeline(sess, winner_id=None):
    """Seconds each stage would take in real life, from owner response delays (not the demo's speed).
    The agent waits for every reply in a round, or the reply timeout if someone stays silent."""
    tl = {"intake": 30, "sourcing": 0, "pricing": 10, "approval": 30, "hold": 0}
    for ids in sess.rounds:
        ts = [sess.threads[i] for i in ids]
        if any(t["status"] == "silent" for t in ts):
            tl["sourcing"] += SRC["ownerReplyTimeoutSec"]
        else:
            tl["sourcing"] += min(SRC["ownerReplyTimeoutSec"], max(t["delay"] for t in ts))
    if winner_id:
        tl["hold"] = min(SRC["ownerReplyTimeoutSec"], max(20, sess.threads[winner_id]["delay"] // 2))
    tl["total"] = sum(tl.values())
    return tl

# =============================== entry points used by the app ===============================
def run_quote(load, pool, use_llm=True):
    load = normalise_load(load)
    miss = missing_fields(load)
    if miss:
        return {"status": "need_info", "load": load, "missing": miss}
    lane = find_lane(load["origin"], load["destination"])
    if not lane:
        return {"status": "unknown_lane", "load": load}
    sess, planner, secs = _run(lambda: LoadSession(load, lane, pool), use_llm)
    return _result(sess, planner, secs)

def run_replan(prev, pool, use_llm=True):
    """The booked owner backed out: re-book at the fixed shipper price, re-check the floor, no human unless breached."""
    dropped = prev["owner"]["ownerId"]
    earlier = {i: t["deal"]["buy"] for i, t in prev["sess"].threads.items()
               if t["status"] == "deal" and i != dropped}
    said_no = {i for i, t in prev["sess"].threads.items() if t["status"] == "declined"}   # same price, same answer
    fixed = prev["quote"]["sell_total"]
    sess, planner, secs = _run(lambda: LoadSession(prev["load"], prev["lane"], pool, fixed_sell=fixed,
                                                   exclude={dropped} | said_no, prefer=earlier, mode="replan"), use_llm)
    res = _result(sess, planner, secs)
    res["dropped"] = prev["owner"]["ownerName"]
    res["dropped_id"] = dropped
    res["status"] = "recovered" if res["status"] == "quote" else "escalate"
    res["needs_human"] = res["status"] != "recovered"
    return res

def _result(sess, planner, secs):
    threads = sorted(sess.threads.values(), key=lambda t: (t["round"], t["delay"]))
    base = {"load": sess.load, "lane": sess.lane, "threads": threads, "trace": sess.trace, "planner": planner,
            "compute_s": secs, "sess": sess, "search_type": sess.search_type,
            "reason": (sess.final or {}).get("reason", "")}
    if not sess.final or sess.final["action"] != "propose":
        return {"status": "escalate", "timeline": modelled_timeline(sess), **base}
    oid = sess.final["owner_id"]
    win = sess.threads[oid]
    asked = sess.load.get("truck_type")
    if win["owner"]["type"] != asked and "instead" not in base["reason"]:   # the broker must see a substitution
        base["reason"] = (base["reason"].rstrip(".") + f". Used a {win['owner']['type']} instead of {asked}.").lstrip(". ")
    return {"status": "quote", "owner": win["owner"], "deal": win["deal"], "quote": sess.quotes[oid], "win": win,
            "timeline": modelled_timeline(sess, oid), **base}

def penalise(memory, owner_id):
    """Learning loop: an owner who backs out loses reliability, so they rank lower next time."""
    rel = memory.setdefault("reliability", {})
    rel[owner_id] = round(rel.get(owner_id, 0) - SRC["reliabilityPenaltyOnBackout"], 2)
    return memory

# =============================== day simulator (no API calls) ===============================
def simulate_day(n_inquiries=40, seed=7):
    import random
    rnd = random.Random(seed)
    pool = build_pool()
    quoted, margins, escalated, subs, mins = 0, [], 0, 0, []
    for _ in range(n_inquiries):
        lane = rnd.choice(lanes)
        tt = rnd.choice(["32 ft MXL", "22 ft", "Taurus (10-tyre)", "32 ft SXL", "20 ft container"])
        cap = rates["capacityTonByType"][tt]
        w = round(rnd.uniform(0.6, 1.1) * cap)          # some loads are heavier than the type asked for
        res = run_quote({"origin": lane["origin"], "destination": lane["destination"], "truck_type": tt,
                         "weight_ton": w, "pickup_when": rnd.choice(["aaj", "kal", "parso"])}, pool, use_llm=False)
        if res["status"] == "quote":
            quoted += 1
            margins.append(res["quote"]["margin_pct"])
            mins.append(res["timeline"]["total"] / 60)
            subs += res["search_type"] != tt
        else:
            escalated += 1
    margins.sort(); mins.sort()
    med = lambda xs: xs[len(xs) // 2] if xs else 0
    return {"n": n_inquiries, "quoted": quoted, "escalated": escalated, "substituted": subs,
            "margin_min": margins[0] if margins else 0, "margin_med": med(margins),
            "margin_max": margins[-1] if margins else 0,
            "avg_margin": round(sum(margins) / len(margins), 1) if margins else 0,
            "median_min": round(med(mins), 1), "share_under_5": round(100 * sum(m < 5 for m in mins) / len(mins)) if mins else 0}
