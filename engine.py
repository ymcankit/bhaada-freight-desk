"""
Bhaada engine: data, deterministic pricing, truck matching, simulated owner replies,
and the agent orchestrator (LLM tool-calling loop with a deterministic fallback planner).

No Streamlit in here, so everything can be tested offline:  python test_engine.py
Prices are computed only in code (trip_cost / price_quote). The LLM decides which
tool to call next; it never produces a number that reaches the shipper.
"""
import json, math, os, re, time, hashlib, difflib, inspect, threading
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
MATERIALS = _load("materials.json")["classes"]
TRUCKS_BY_ID = {t["truckId"]: t for t in trucks}
OWNERS_BY_ID = {o["ownerId"]: o for o in owners}
REQUIRED = ["origin", "destination", "weight_ton", "material"]     # the truck type is advised from these when not given

# =============================== LLM: OpenAI (paid credit) first for what people read, then free Groq ===============================
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_CHAT = "https://api.openai.com/v1/chat/completions"
LLM = {"key": os.getenv("GROQ_API_KEY", ""),
       "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
       "fallback": os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b"),   # its own per-minute quota on Groq
       "openai_key": os.getenv("OPENAI_API_KEY", ""),
       "openai_model": os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
       "whisper": os.getenv("GROQ_WHISPER", "whisper-large-v3")}   # turbo garbles Hinglish city names

def local_secrets():
    """Read .streamlit/secrets.toml (simple KEY = "value" lines) for scripts that run outside Streamlit."""
    p = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if not p.exists():
        return {}
    return {k: (q or bare).strip() for k, q, bare in      # KEY = "value", or KEY = value without quotes
            re.findall(r'^\s*(\w+)\s*=\s*(?:"([^"]*)"|([^"#\s][^#\n]*))', p.read_text(encoding="utf-8"), flags=re.MULTILINE)}

def configure(key=None, model=None, whisper=None, openai_key=None, openai_model=None):
    """Set credentials from the app (env or secrets) instead of hardcoding them."""
    if key: LLM["key"] = key
    if model: LLM["model"] = model
    if whisper: LLM["whisper"] = whisper
    if openai_key: LLM["openai_key"] = openai_key
    if openai_model: LLM["openai_model"] = openai_model

def llm_ready():
    return bool(LLM["key"] or LLM["openai_key"])

def _cool(name, until=None):
    """Seconds-since-epoch until which a provider is rate limited; "groq" keeps the original LLM["coolUntil"]."""
    box, k = (LLM, "coolUntil") if name == "groq" else (LLM.setdefault("cools", {}), name)
    if until is not None:
        box[k] = max(box.get(k, 0), until)
    return box.get(k, 0)

def _providers(tier):
    """Who answers, in order. "smart": words people read (chat, negotiation, strategy) go to OpenAI first.
    "cheap": routine extraction stays on free Groq and uses OpenAI only when Groq is out."""
    groq = [("groq", GROQ_CHAT, LLM["key"], LLM["model"])] if LLM["key"] else []
    if LLM["key"] and LLM.get("fallback") and LLM["fallback"] != LLM["model"]:
        groq.append(("groq-fallback", GROQ_CHAT, LLM["key"], LLM["fallback"]))
    oa = [("openai", OPENAI_CHAT, LLM["openai_key"], LLM["openai_model"])] if LLM["openai_key"] else []
    return oa + groq if tier == "smart" else groq + oa

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

# ---- observability: numbers only (calls, tokens, failures, cost), never message content or keys ----
# USD per million tokens (input, output). gpt-oss-20b is not on our price sheet: half of 120b, an estimate.
PRICES = {"gpt-5.4-mini": (0.75, 4.50), "openai/gpt-oss-120b": (0.15, 0.60), "openai/gpt-oss-20b": (0.075, 0.30)}
_stats_lock = threading.Lock()

def _new_stats():
    return {"since": time.time(), "calls": 0, "rulesFallbacks": 0, "noKey": 0, "providers": {}}
STATS = _new_stats()

def _stat(name, model, ok=False, fail=False, fallback=False, usage=None):
    """Count one provider attempt: a success with its token usage, or a failure (and whether the next one took over)."""
    with _stats_lock:
        p = STATS["providers"].setdefault(name, {"model": model, "ok": 0, "failures": 0, "fallbacks": 0,
                                                 "promptTokens": 0, "completionTokens": 0, "usd": 0.0})
        p["model"] = model
        p["ok"] += ok
        p["failures"] += fail
        p["fallbacks"] += fallback
        if usage:
            pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            pin, pout = PRICES.get(model, (0, 0))
            p["promptTokens"] += pt
            p["completionTokens"] += ct
            p["usd"] = round(p["usd"] + (pt * pin + ct * pout) / 1e6, 6)

def llm_stats():
    """A copy for /healthz: calls, tokens, failures, fallbacks and the estimated cost, per provider and in total."""
    with _stats_lock:
        out = json.loads(json.dumps(STATS))
    ps = out["providers"].values()
    out.update(since=round(out["since"]), usd=round(sum(p["usd"] for p in ps), 4),
               tokens=sum(p["promptTokens"] + p["completionTokens"] for p in ps))
    return out

def reset_stats():
    global STATS
    with _stats_lock:
        STATS = _new_stats()

def chat(messages, tools=None, json_mode=False, timeout=25, retries=3, max_wait=30, tier="cheap"):
    """One chat completion, trying each configured provider in turn, each with a timeout and retries, so a slow
    call or a used-up quota cannot freeze the demo. Raises only when every provider failed."""
    chain = _providers(tier)
    with _stats_lock:
        STATS["calls"] += 1
        if not chain:
            STATS["noKey"] += 1
            STATS["rulesFallbacks"] += 1
    if not chain:
        raise RuntimeError("No language model key is set (GROQ_API_KEY or OPENAI_API_KEY)")
    last = None
    for i, (name, url, key, model) in enumerate(chain):
        if LLM.setdefault("down", {}).get(url, 0) > time.time():   # just timed out or 5xx'd: skip it, don't wait on it again
            last = last or RuntimeError(f"{name} unreachable (skipped for a few seconds)")
            continue
        try:
            return _complete(name, url, key, model, messages, tools, json_mode, timeout, retries, max_wait)
        except Exception as e:
            last = e
            _stat(name, model, fail=True, fallback=i < len(chain) - 1)
            print(f"LLM {name} failed, trying the next one:", str(e)[:160])
    with _stats_lock:
        STATS["rulesFallbacks"] += 1                  # every provider failed: the caller uses its rules
    raise last

def _complete(name, url, key, model, messages, tools, json_mode, timeout, retries, max_wait):
    body = {"model": model, "messages": messages, "max_completion_tokens": 700}
    if name == "openai":
        body["reasoning_effort"] = "low"                # GPT-5-family models take only the default temperature
        body["max_completion_tokens"] = 1500           # reasoning tokens count against this too
    else:
        body["temperature"] = 0
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"          # fewer hidden reasoning tokens against the per-minute cap
    if tools:
        body["tools"], body["tool_choice"] = tools, "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    last = None
    for i in range(retries):
        left = _cool(name) - time.time()               # some thread already hit the limit: don't spend a request on it
        if left > max_wait:
            raise last or RuntimeError(f"{name} rate limit (cooling down {left:.0f}s)")
        if left > 0:
            time.sleep(left)
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout)
            if r.status_code == 429:
                wait = _wait_secs(r)
                if "insufficient_quota" in r.text:     # the prepaid credit is used up: stop asking for a while
                    wait = 3600
                _cool(name, time.time() + wait + 0.5)
                last = RuntimeError(f"{name} rate limit (wait {wait:.0f}s)")
                if wait > max_wait:
                    break
                continue
            if r.status_code in (400, 401, 403, 404, 413):   # a bad key, model or request: retrying won't help
                raise PermissionError(f"{name} {r.status_code}: {r.text[:200]}")
            if not r.ok:
                raise RuntimeError(f"{name} {r.status_code}: {r.text[:200]}")
            j = r.json()
            msg = j["choices"][0]["message"]
            _stat(name, model, ok=True, usage=j.get("usage"))
            return msg
        except PermissionError:
            raise
        except (requests.Timeout, requests.ConnectionError) as e:
            # a hung or unreachable provider: every retry would cost the caller (often the desk's one worker) a full
            # timeout again, so the next provider answers and this one is skipped for 30 s (same host: both Groq models)
            LLM.setdefault("down", {})[url] = time.time() + 30
            raise RuntimeError(f"{name} unreachable: {str(e)[:120]}") from e
        except Exception as e:
            last = e
            if i < retries - 1:                        # no pause after the last try: the next provider is waiting
                time.sleep(1.5 * (i + 1))
    raise last

def gen(prompt, json_mode=False, tier="cheap"):
    return chat([{"role": "user", "content": prompt}], json_mode=json_mode, tier=tier).get("content") or ""

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

def read_image(data, mime="image/jpeg", timeout=40):
    """A photo or screenshot of a load post -> the load as plain text ("" if it shows no load). OpenAI only: vision."""
    if not LLM["openai_key"]:
        raise RuntimeError("OPENAI_API_KEY is not set")
    import base64
    url = f"data:{mime or 'image/jpeg'};base64," + base64.b64encode(data).decode()
    prompt = ("This is a photo or screenshot a freight broker in India forwarded (a WhatsApp group post, a load tender, "
              "a note). Copy out the load request it shows: from city, to city, weight, goods, truck type, pickup day, "
              "budget/rate if given, in short plain lines as written (Hinglish is fine). Leave out phone numbers and "
              "people's names. If it shows no load request, return an empty string. "
              'Return ONLY JSON {"text":""}')
    body = {"model": LLM["openai_model"], "reasoning_effort": "low", "max_completion_tokens": 1500,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                      {"type": "image_url", "image_url": {"url": url}}]}]}
    r = requests.post(OPENAI_CHAT, headers={"Authorization": f"Bearer {LLM['openai_key']}"}, json=body, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"openai vision {r.status_code}: {r.text[:200]}")
    return str(parse_json(r.json()["choices"][0]["message"].get("content") or "{}").get("text") or "").strip()

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
    hits = sorted((t.find(c.lower()), c) for c in KNOWN_CITIES             # whole words: Suratgarh is not Surat
                  if re.search(r"(?<![a-z])" + re.escape(c.lower()) + r"(?![a-z])", t))
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
        if hit and hit.get("src") != "seed":
            NEW_PLACES[hit["name"]] = hit             # found by a geocoder: the broker confirms it before owners are messaged
        return hit["name"] if hit else None
    return None

NEW_PLACES = {}
YES_TEXT = re.compile(r"^\s*(haan|haa|ha|han|hn|yes|y|ok|okay|sahi|sahi hai|theek|thik|correct|ji|ji haan|bilkul)\b", re.I)

def _km_guess(a, b):
    pts = [CITY["cities"].get(x) or ([NEW_PLACES[x]["lat"], NEW_PLACES[x]["lon"]] if x in NEW_PLACES else None) for x in (a, b)]
    if not all(pts):
        return None
    (la1, lo1), (la2, lo2) = [map(math.radians, p) for p in pts]
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)) * 1.3)

def norm_type(s):
    """Map '32 MXL', '10 tyre', '22ft' etc. to a known type. Ambiguous ('32 ft') returns None so we ask."""
    if not s:
        return None
    if s in KNOWN_TYPES:
        return s
    t = str(s).lower()
    wheels = re.search(r"(\d{1,2})\s*[- ]?(?:tyre|tire|wheel|wheeler|chakka|chakke|pahiya)", t)
    if "taurus" in t or (wheels and wheels.group(1) == "10"):
        return "Taurus (10-tyre)"
    if wheels and wheels.group(1) in ("12", "14"):
        return f"{wheels.group(1)}-wheeler"
    if "trailer" in t or (wheels and wheels.group(1) in ("16", "18", "22")):
        return "Trailer 40 ft"
    if wheels:                                          # a 6-wheeler: the adviser picks the length by weight
        return None
    if re.search(r"\b407\b", t):                          # Tata 407 (~2.5 T): the smallest class on the rate card
        return "14 ft"
    if re.search(r"\b(ace|chhota hathi|pickup|pick-up|709|909|1109)\b", t):
        return None                                     # LCV/ICV model names the rate card doesn't carry yet
    m = re.search(r"(\d{2})", t)
    if "container" in t:                                # read the length first: "32 ft container" is an SXL or MXL
        if m and m.group(1) == "20":
            return "20 ft container"
        if m and m.group(1) == "24":
            return "24 ft SXL"
        if m and m.group(1) == "32":
            return "32 ft MXL" if ("mxl" in t or "multi" in t) else "32 ft SXL" if ("sxl" in t or "single" in t) else None
        return None if m else "20 ft container"
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
    if isinstance(x, str):                              # "1500 cft", "32 ft", "800 carton" are not weights
        x = re.sub(r"\d[\d,.]*\s*(?:cft|cu\.?\s*ft|cubic\s*\w+|cbm|ft|feet|foot|km|carton|box|peti|packet)s?\b", " ", x, flags=re.I)
        if not re.search(r"\d", x):
            return None
    v = norm_weight(str(x).replace(",", "") if x not in (None, "") else x)
    if v is None:
        return None
    s = str(x).lower()
    if "quintal" in s:
        v /= 10
    elif "kg" in s or "kilo" in s:
        v /= 1000
    elif re.search(r"\bton|\btonne|\bmt\b|\d\s*t\b|टन", s):
        pass                                            # "100 ton" is 100 tons: too big for one truck, and the adviser says so
    elif v >= 100:                                      # a bare 18000 is kilograms: no truck carries 100 T
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
    out["volume_cft"] = norm_money(out.get("volume_cft"))
    out["invoice_value_rs"] = norm_money(out.get("invoice_value_rs"))
    if out.get("material"):
        out["material"] = str(out["material"]).strip()[:60]
    return out

def missing_fields(load):
    miss = [k for k in REQUIRED if not load.get(k) and not (k == "material" and load.get("truck_type"))]
    # a broker who names the truck has already chosen the body; the goods are then asked only if they matter (chemicals)
    if not miss and MATERIALS.get(load.get("material_class"), {}).get("askHazard") and load.get("hazardous") is None:
        miss.append("hazard")
    return miss

# =============================== conversational intake (shared by the web desk and WhatsApp) ===============================
def _hist(h):
    who = {"user": "Broker", "owner": "Owner", "bot": "Bhaada", "agent": "Bhaada"}
    return "\n".join(f'{who.get(m["role"], m["role"])}: {m["text"]}' for m in h) or "(start of chat)"

ASK = {"origin": "Kahan se load uthana hai?", "destination": "Kahan tak jaana hai?",
       "weight_ton": "Kitna ton hai?",
       "material": "Kya maal hai? (kapda, steel, chemical, dawai, carton, anaaj…) Isse sahi gaadi chunta hoon.",
       "hazard": "Ye chemical khatarnaak hai (acid, solvent, gas, jwalansheel) ya normal? Khatarnaak ho to alag permit wali gaadi lagti hai.",
       "truck_type": "Kaunsi gaadi chahiye? (" + ", ".join(KNOWN_TYPES[:6]) + " …)"}

def intake_rules(hist):
    """A load read without a model: 'X se Y', tons, a truck name, the goods and the day, from the broker's own words."""
    text = " ".join(m["text"] for m in hist if m["role"] == "user")
    low = text.lower()
    out = {}
    pair = re.search(r"([a-z\u0900-\u097f][\w\u0900-\u097f ]{1,30}?)\s+(?:se|from)\s+([a-z\u0900-\u097f][\w\u0900-\u097f]{1,30})", low)
    if pair:
        out["origin"] = pair.group(1).split()[-1]
        out["destination"] = pair.group(2)
    else:
        found = sorted((low.find(c.lower()), c) for c in KNOWN_CITIES if re.search(r"\b" + re.escape(c.lower()) + r"\b", low))
        if len(found) >= 2:
            out["origin"], out["destination"] = found[0][1], found[1][1]
    w = re.search(r"(\d+(?:\.\d+)?)\s*(?:ton|tonne|tan|t\b|mt\b|टन)", low)
    if w:
        out["weight_ton"] = w.group(1)
    truck = re.search(r"\d{2}\s*(?:ft|feet|foot|fit)?\s*(?:mxl|sxl|container)|\d{2}\s*(?:ft|feet|foot|fit)\b|\d{1,2}\s*[- ]?(?:wheeler|"
                      r"tyre|tire|chakka)|taurus|trailer|\b407\b", low)
    if truck and norm_type(truck.group(0)):        # only a phrase that names a truck: "14 ton" is not a 14 ft
        out["truck_type"] = norm_type(truck.group(0))
    if material_class(text) != "general":
        out["material"] = material_class(text)
    said = pickup_phrase([text])
    if said:
        out["pickup_when"] = said
    return out

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
Required fields: origin, destination, weight_ton, material (what the goods are, in the broker's words, e.g. "kapda", "steel saria", "acid drums"; "pata nahi"/"general" is fine if they don't say). Optional: truck_type (only if the broker names one; never guess it - the desk advises the truck), pickup_when, budget_rs (shipper's budget in rupees, number), volume_cft (cubic feet if given), hazardous (true/false only if the broker said whether a chemical is dangerous), invoice_value_rs (value of the goods in rupees, if given: decides the e-way bill).
Return ONLY JSON:
{{"reply":"<one or two short lines>","load":{{"origin":null,"destination":null,"weight_ton":null,"material":null,"truck_type":null,"pickup_when":null,"budget_rs":null,"volume_cft":null,"hazardous":null,"invoice_value_rs":null}},"missing":["..."],"complete":false}}
Fill load with everything known so far. If all required fields are known set complete=true and reply that you are finding the best truck now.
If anything required is missing, the reply must ASK for exactly those details (e.g. "Kahan se kahan tak?") and must not say you are finding a truck.
Reply language: Hinglish - Hindi words in Roman script, like "Theek hai, kitne ton maal hai?" - even if the broker wrote in English or in Devanagari. If they write in Marathi, Gujarati, Punjabi, Bengali, Tamil, Telugu or Kannada, reply in that language in Roman script. Never reply in plain English."""
    try:
        out = parse_json(gen(prompt, json_mode=True, tier="smart"))
    except Exception as e:                              # every model down or a reply that isn't JSON: read it with rules
        print("intake model failed, using rules:", str(e)[:120])
        out = {"load": intake_rules(hist), "reply": ""}
    raw = {**(load or {}), **{k: v for k, v in (out.get("load") or {}).items() if v not in (None, "")}}
    raw.pop("blocked", None)
    merged = normalise_load(raw)                      # anything that doesn't match our lists becomes missing
    said_all = " ".join(m["text"] for m in hist if m["role"] == "user")
    merged["material_class"] = material_class(merged.get("material"), said_all)
    if merged["material_class"] == "chemical" and merged.get("hazardous") is True:
        merged["material_class"] = "hazardous"
    if merged.get("material") is None and re.search(r"pata nahi|general|kuch bhi|mixed", user_msg, re.I):
        merged["material"] = "general"
    said = pickup_phrase([m["text"] for m in hist if m["role"] == "user"])
    if said and pickup_day(said) != pickup_day(merged.get("pickup_when")):
        merged["pickup_when"] = said                  # the broker's words win over the model's
    miss = missing_fields(merged)
    unsure = [c for c in (merged.get("origin"), merged.get("destination")) if c and c in NEW_PLACES]
    if unsure and not miss:
        if (load or {}).get("placesAsked") == unsure and YES_TEXT.search(user_msg):
            merged["placesOk"] = unsure                 # the broker confirmed the places the geocoder found
        elif merged.get("placesOk") != unsure:
            merged["placesAsked"] = unsure
            km = _km_guess(merged["origin"], merged["destination"])
            return merged, (f"Ek baar confirm kar lijiye: *{merged['origin']} → {merged['destination']}*"
                            + (f" (~{km:,} km)" if km else "") + " sahi hai? 🙏 *Haan* likhiye, ya shehar ka sahi naam bhejiye."), ["confirm"]
    reply = out.get("reply", "")
    if miss and (out.get("complete") or "?" not in reply or "hazard" in miss):   # a reply that doesn't ask can't move the load forward
        reply = ("Ek cheez confirm karni hai — " if len(miss) == 1 else "") + " ".join(ASK[k] for k in miss[:2])
    if not miss:      # advise the truck, then read back what will be booked, from the validated fields
        fit = truck_fit(merged)
        m = fit["material"]
        if not fit["type"]:
            merged["blocked"] = fit["why"]
            return merged, f"🙏 {fit['why']}. Load badal ke bhejna ho to nayi details bhejiye, ya *hi* likh ke naya shuru kijiye.", ["blocked"]
        advice = ""
        if fit["type"] != merged.get("truck_type"):
            advice = f" (meri salah: {fit['why']})" if merged.get("truck_type") else f" (sujhaav: {fit['why']})"
            merged["truck_type_asked"], merged["truck_type"] = merged.get("truck_type"), fit["type"]
        reply = (f"Theek hai ✅ {merged['origin']} → {merged['destination']}, {merged['weight_ton']:g} T {m['hi']}, "
                 f"{merged['truck_type']}{advice}, {merged.get('pickup_when') or 'kal'}"
                 + (f", budget ₹{merged['budget_rs']:,.0f}" if merged.get("budget_rs") else "")
                 + (". ⚠️ Khatarnaak maal: sirf Hazchem permit aur trained driver wali gaadi" if m.get("hazmat") else "")
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

WEEKDAYS = {"somvar": 0, "monday": 0, "mangalvar": 1, "tuesday": 1, "budhvar": 2, "wednesday": 2, "guruvar": 3, "veervar": 3,
            "thursday": 3, "shukravar": 4, "friday": 4, "shanivar": 5, "saturday": 5, "ravivar": 6, "itvaar": 6, "sunday": 6}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

def pickup_day(text):
    """Days from today to pickup: aaj 0, kal 1, parso 2, a weekday, or a date ('30 tarikh', '28-Oct'). Defaults to tomorrow."""
    from datetime import date
    t = _roman(text)
    if re.search(r"\bparso\b|day after|2 din", t):
        return 2
    if re.search(r"\baaj\b|\btoday\b|\babhi\b|\btonight\b", t):
        return 0
    today = date.today()
    wd = next((d for w, d in WEEKDAYS.items() if re.search(r"\b" + w + r"\b", t)), None)
    if wd is not None:
        return (wd - today.weekday()) % 7 or 7
    m = re.search(r"\b(\d{1,2})\s*(?:-|/|\s)?\s*(?:tarikh|tareekh|date|th|st|nd|rd)?\s*[-/ ]?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)?", t)
    if m and (m.group(2) or re.search(r"tarikh|tareekh|date", t)):
        try:
            month = MONTHS.get(m.group(2)) or today.month
            d = date(today.year, month, int(m.group(1)))
            if d < today:
                d = date(today.year + (month <= today.month and not m.group(2) and 0 or 1), month, int(m.group(1))) \
                    if m.group(2) else date(today.year + (today.month == 12), today.month % 12 + 1, int(m.group(1)))
            return min(30, (d - today).days)
        except ValueError:
            pass
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
def market_rate(d, tt):
    """What a shipper pays for this truck over d km, in rupees: fixed + per-km, fitted to published lane rates."""
    m = rates["marketRateByType"][tt]
    return round(m["fixedRs"] + m["perKm"] * d)

def fair_cost(d, tt, rp, memory=None):
    """Expected buy price of the loaded trip. Owners get the market's shipper price less a normal broker margin,
    and never less than what running the truck really costs them (fuel, toll, driver and helper,
    EMI/maintenance/insurance, handling). memory: ₹/km actually agreed on this lane and type lately; it pulls
    the estimate halfway towards what owners really took. The sell price is built on this."""
    fuel = d / rates["mileageKmPerLitre"][tt] * rates["dieselPerLitre"]
    toll = d * rates["tollPerKmByAxle"][str(rates["axlesByType"][tt])]
    days = max(1, math.ceil(d / rates["avgSpeedKmPerDay"]))
    driver = days * (rates["driverCostPerDay"] + rates["driverAllowancePerDay"] + rates["helperPerDay"])
    fixed = d * rates["fixedCostPerKmByType"][tt]
    run = fuel + toll + driver + fixed + rates["handlingMiscPerTrip"]
    if rp >= 0.6:                                     # a likely return load lowers what the owner needs from this leg
        run *= (1 - rates["returnLoadDiscountPct"] / 100)
    market = market_rate(d, tt)
    owner_market = market * (1 - MARGIN["targetPct"] / 100)
    if memory:
        owner_market = (owner_market + memory * d) / 2
    est = max(run, owner_market)
    return {"fuel": round(fuel), "toll": round(toll), "driver": round(driver), "fixed": round(fixed),
            "misc": rates["handlingMiscPerTrip"], "owner_cost": round(run), "market": market,
            "owner_market": round(owner_market), "basis": "market" if owner_market >= run else "cost", "est_cost": round(est)}

def deadhead_cost(km, tt):
    """Owner's cost of driving empty to the pickup point: fuel + tolls. Owners price it into their ask."""
    per_km = rates["dieselPerLitre"] / rates["mileageKmPerLitre"][tt] + rates["tollPerKmByAxle"][str(rates["axlesByType"][tt])]
    return round(km * per_km)

def lane_memory(lane, tt):
    return (lane.get("memory") or {}).get(tt)

def season(day=None):
    """The festival or peak window a pickup date falls in: (name, +%) or (None, 0)."""
    from datetime import date
    d = day or date.today()
    for s in rates.get("seasons", []):
        lo, hi = (x if len(x) == 10 else f"{d.year}-{x}" for x in (s["from"], s["to"]))
        if lo <= d.isoformat() <= hi:
            return s["name"], s["pct"]
    return None, 0

def lane_cost(lane, tt):
    """fair_cost for a load's lane, with its goods premium (hazardous, perishable...) and the season on the expected buy price."""
    c = fair_cost(lane["distanceKm"], tt, lane["returnLoadProb"], lane_memory(lane, tt))
    p = (lane.get("premiumPct") or 0) + (lane.get("seasonPct") or 0)
    if p:
        c = {**c, "est_cost": round(c["est_cost"] * (1 + p / 100)), "market": round(c["market"] * (1 + p / 100)), "premiumPct": p}
    return c

def target_sell(lane, tt):
    return lane_cost(lane, tt)["est_cost"] / (1 - MARGIN["targetPct"] / 100)

def price_quote(lane, tt, buy, fixed_sell=None, budget=None):
    """Sell price = the lane's market-anchored price (expected buy at target margin), matched down to the shipper's
    budget only if the floor still holds. A cheaper buy is the broker's gain, not a discount for the shipper.
    After booking, the shipper price is fixed (re-plan)."""
    br = lane_cost(lane, tt)
    note = ""
    if fixed_sell:
        sell = fixed_sell
        note = "shipper price fixed at booking"
    else:
        sell = br["est_cost"] / (1 - MARGIN["targetPct"] / 100)
        if buy < 0.6 * br["owner_cost"]:
            note = f"owner's price is far below the truck's running cost (₹{br['owner_cost']:,}): confirm with the owner"
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

def material_class(*texts):
    """Which rule applies to the goods: matched on the broker's own words (Roman Hinglish or Hindi)."""
    t = " " + " ".join(str(x or "") for x in texts).lower() + " "
    for key, m in MATERIALS.items():
        if any(re.search(r"(?<![\w])" + re.escape(w.lower()) + r"(?:s|es)?(?![\w])", t) for w in m["words"]):
            return key
    return "general"

def body_ok(tt, body):
    """closed: rain/theft-sensitive goods; open: crane- or forklift-loaded goods (steel, machinery) that a box can't take."""
    have = rates["bodyByType"][tt]
    return body == "any" or body in have

def truck_fit(load):
    """Which truck this load needs, from weight, the goods' body rule and their volume.
    Returns the type to source, why, and the material rule; never smaller than what the broker asked for."""
    w = load.get("weight_ton") or 0
    m = MATERIALS[load.get("material_class") or "general"]
    vol = load.get("volume_cft") or (round(w * 1000 / m["kgPerCft"]) if m.get("kgPerCft") and w else 0)
    caps, cft = rates["capacityTonByType"], rates["volumeCftByType"]
    fits = lambda t: caps[t] >= w and cft[t] >= vol and body_ok(t, m["body"])
    asked = load.get("truck_type")
    if m["body"] == "reefer":
        return {"type": None, "why": "reefer chahiye, hamare pool mein abhi reefer nahi", "material": m, "volume": vol}
    if asked and fits(asked):
        return {"type": asked, "why": "", "material": m, "volume": vol}
    options = sorted((t for t in KNOWN_TYPES if fits(t)), key=lambda t: market_rate(500, t))
    if asked:                                       # keep at least the size they asked for
        options = [t for t in options if caps[t] >= caps.get(asked, 0)] or options
    if not options:
        n = max(2, -(-int(w) // max(caps[t] for t in KNOWN_TYPES if body_ok(t, m["body"]) or m["body"] == "reefer")))
        why = (f"{w:g} T" + (f" aur ~{vol:,} cft" if vol else "") + f" ek {m['hi']} wali gaadi mein nahi aayega; "
               f"load kam se kam {n} gaadiyon mein baantna padega. Ek gaadi ka load bhejiye (e.g. {min(w, 16):g} T)")
        return {"type": None, "why": why, "material": m, "volume": vol}
    best = options[0]
    bits = []
    if asked and caps[asked] < w:
        bits.append(f"{asked} mein {w:g} T nahi aayega")
    elif asked and cft[asked] < vol:
        bits.append(f"{asked} mein ~{vol:,} cft {m['hi']} nahi samayega")
    elif asked and not body_ok(asked, m["body"]):
        bits.append(f"{m['hi']} ke liye band (closed) body chahiye")
    bulky = m.get("kgPerCft", 99) <= 10
    why = ", ".join(bits) or f"{w:g} T {m['hi']}" + (f" (~{vol:,} cft)" if vol and bulky else "") + " ke liye sabse sasti sahi gaadi"
    return {"type": best, "why": why, "material": m, "volume": vol}

def advise(text):
    """The desk's own truck pick for goods and tons named in a free-text question, so the chat explains the same
    answer sourcing would use. None if the message doesn't say how many tons."""
    w = norm_tons((re.search(r"(\d+(?:\.\d+)?)\s*(?:ton|tonne|t\b|टन)", str(text), re.I) or [None, None])[1])
    if not w:
        return None
    vol = re.search(r"(\d{3,5})\s*(?:cft|cubic|cu\.? ?ft)", str(text), re.I)
    fit = truck_fit({"weight_ton": w, "material_class": material_class(text), "volume_cft": float(vol.group(1)) if vol else None})
    if not fit["type"]:
        return f"{w:g} T {fit['material']['hi']}: {fit['why']}"
    caps = rates["capacityTonByType"]
    alts = [t for t in KNOWN_TYPES if t != fit["type"] and truck_fit({"weight_ton": w, "material_class": material_class(text),
                                                                       "truck_type": t})["type"] == t][:2]
    return (f"{w:g} T {fit['material']['hi']} → {fit['type']} ({caps[fit['type']]} T, {rates['bodyByType'][fit['type']]} body): "
            f"{fit['why']}" + (f". Also fits: {', '.join(alts)}" if alts else ""))

def truck_catalogue():
    """One line per truck type, for the chat models to advise from."""
    return "\n".join(f"- {t}: up to {rates['capacityTonByType'][t]} T, ~{rates['volumeCftByType'][t]:,} cft, "
                     f"{rates['bodyByType'][t]} body, market ~₹{round(market_rate(500, t) / 500)}/km at 500 km" for t in KNOWN_TYPES)

def material_guide():
    return "\n".join(f"- {m['label']}: body {m['body']}" + (", hazardous" if m.get("hazmat") else "")
                     + (f", +{m['premiumPct']}% rate" if m.get("premiumPct") else "") + (f". {m['note']}" if m.get("note") else "")
                     for k, m in MATERIALS.items() if k not in ("general", "chemical"))

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
    load = normalise_load({"material": "general", **(load or {})})   # the earlier planner names the truck itself
    miss = missing_fields(load) + ([] if load.get("truck_type") else ["truck_type"])
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
