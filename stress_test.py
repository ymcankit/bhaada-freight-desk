"""Stress test of the live desk: many brokers and owners on fake Telegram, through a real local uvicorn server.

Nothing leaves this machine: every outside host (Telegram, OpenAI, Groq, Upstash, Nominatim, OSRM) is answered by a
fake inside this process, so faults can be injected (429 storms, 5xx, timeouts, Upstash down). Only `--real` lets
OpenAI/Groq through, capped at 40 calls, for a small "real model under load" check.

Run:  .venv/bin/python stress_test.py                 throughput, long run, faults (rules only, free)
      .venv/bin/python stress_test.py long --loads 2000
      .venv/bin/python stress_test.py real            5 brokers on the real model chain (<= 40 calls)
Phases: throughput, long, faults, real (any subset; default: throughput long faults).
"""
import os, sys, re, json, time, random, threading, queue, heapq, itertools, collections, tempfile, socket, resource, tracemalloc
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

ARGS = [a for a in sys.argv[1:] if a in ("throughput", "long", "faults", "real")]
PHASES = ARGS or ["throughput", "long", "faults"]
N_LONG = int(sys.argv[sys.argv.index("--loads") + 1]) if "--loads" in sys.argv else 1000
for k in ("PUBLIC_URL", "RENDER_EXTERNAL_URL", "RENDER", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
          "WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "CONSOLE_PASSWORD", "OPENAI_API_KEY"):
    os.environ.pop(k, None)
os.environ["GROQ_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:" + "s" * 35          # fake, but shaped like a real token

# ---------------- the network: a fake for every outside host, so nothing real is reached ----------------
import requests
from requests.structures import CaseInsensitiveDict
_real_request = requests.Session.request
NET = collections.Counter()
REAL = {"allow": False, "budget": int(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 40,
        "used": 0, "log": [], "lock": threading.Lock()}
TG = {"mode": "ok", "p": 1.0, "hang": 3.0, "attempts": 0, "delivered": 0, "lost": 0, "retry_after": 1}
LLMF = {"mode": "down", "retry_after": 5, "hang": 12.0}
GEO = {"mode": "down", "hang": 10.0}
SENT, SENT_Q = [], queue.Queue()

def _resp(url, status=200, body=None, headers=None):
    r = requests.models.Response()
    r.status_code, r.url, r.reason, r.encoding = status, url, "", "utf-8"
    r._content = json.dumps(body if body is not None else {}).encode()
    r.headers = CaseInsensitiveDict(headers or {"content-type": "application/json"})
    return r

def _hang(kw, secs):
    time.sleep(min(float(kw.get("timeout") or secs), secs))
    raise requests.ReadTimeout("stress test: read timed out")

def _telegram(method, url, kw):
    path = urlparse(url).path
    if path.endswith("/getMe"):
        return _resp(url, body={"ok": True, "result": {"username": "stress_bot"}})
    if not path.endswith("/sendMessage"):
        return _resp(url, body={"ok": True, "result": True})
    TG["attempts"] += 1
    if TG["mode"] != "ok" and random.random() < TG["p"]:
        TG["lost"] += 1
        if TG["mode"] == "429":
            return _resp(url, 429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                                    "parameters": {"retry_after": TG["retry_after"]}})
        if TG["mode"] == "500":
            return _resp(url, 502, {"ok": False, "description": "Bad Gateway"})
        if TG["mode"] == "timeout":
            _hang(kw, TG["hang"])
        raise requests.ConnectionError("stress test: telegram down")
    TG["delivered"] += 1
    b = kw.get("json") or {}
    kb = (b.get("reply_markup") or {}).get("inline_keyboard") or [[]]
    item = ("tg:" + str(b.get("chat_id")), b.get("text", ""), [(x["callback_data"], x["text"]) for x in kb[0]] or None, time.time())
    SENT.append(item)
    SENT_Q.put(item)
    return _resp(url, body={"ok": True, "result": {"message_id": TG["delivered"]}})

def _llm(method, url, kw):
    if LLMF["mode"] == "429":
        return _resp(url, 429, {"error": {"message": f"Rate limit reached. Please try again in {LLMF['retry_after']}s."}},
                     {"retry-after": str(LLMF["retry_after"])})
    if LLMF["mode"] == "500":
        return _resp(url, 503, {"error": {"message": "overloaded"}})
    if LLMF["mode"] == "timeout":
        _hang(kw, LLMF["hang"])
    raise requests.ConnectionError("stress test: model provider unreachable")

def _geo(method, url, kw):
    if GEO["mode"] == "timeout":
        _hang(kw, GEO["hang"])
    raise requests.ConnectionError("stress test: geocoder/router down")

FAKES = {"api.telegram.org": _telegram, "api.openai.com": _llm, "api.groq.com": _llm,
         "nominatim.openstreetmap.org": _geo, "photon.komoot.io": _geo, "router.project-osrm.org": _geo}

def _router(self, method, url, **kw):
    host = urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost"):
        return _real_request(self, method, url, **kw)
    NET[host] += 1
    if REAL["allow"] and host in ("api.openai.com", "api.groq.com") and host not in REAL.get("fake", ()):
        with REAL["lock"]:
            if REAL["used"] >= REAL["budget"]:
                raise requests.ConnectionError("stress test: real-call budget used up")
            REAL["used"] += 1
        t0 = time.time()
        r = _real_request(self, method, url, **kw)
        try:
            u = r.json().get("usage") or {}
        except Exception:
            u = {}
        REAL["log"].append({"host": host, "model": (kw.get("json") or {}).get("model"), "status": r.status_code,
                            "secs": round(time.time() - t0, 2), "in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
                            "limits": {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit-limit")}})
        return r
    if host in FAKES:
        return FAKES[host](method, url, kw)
    raise requests.ConnectionError(f"stress test: no network to {host}")

requests.Session.request = _router

# ---------------- the desk, with its store and its worker instrumented ----------------
import engine as E
import live

class CountingFile:
    """live.STATE_FILE: counts local writes and their size."""
    def __init__(self, p):
        self.p, self.writes, self.size = Path(p), 0, 0
    def write_text(self, t, encoding=None):
        self.writes, self.size = self.writes + 1, len(t.encode())
        self.p.write_text(t, encoding="utf-8")
    def read_text(self, encoding=None):
        return self.p.read_text(encoding="utf-8")
    def unlink(self, missing_ok=False):
        self.p.unlink(missing_ok=missing_ok)

live.STATE_FILE = CountingFile(Path(tempfile.gettempdir()) / "bhaada_stress_state.json")
live.STATE_FILE.unlink(missing_ok=True)
STORE = {"up": True, "data": {}, "archive": {}, "sets": 0, "bytes": 0, "max": 0, "hsets": 0, "times": [], "fails": 0}

def fake_redis(cmd):
    """Upstash, in memory: counts commands and payload sizes; can be taken down."""
    if not STORE["up"]:
        STORE["fails"] += 1
        raise requests.ConnectionError("stress test: upstash down")
    op = cmd[0]
    if op == "GET":
        return STORE["data"].get(cmd[1])
    if op == "SET":
        STORE["data"][cmd[1]] = cmd[2]
        n = len(cmd[2].encode())
        STORE["sets"] += 1
        STORE["bytes"] += n
        STORE["max"] = max(STORE["max"], n)
        STORE["times"].append(time.time())
    if op == "HSET":
        STORE["hsets"] += 1
        for k, v in zip(cmd[2::2], cmd[3::2]):
            STORE["archive"][k] = v
    return "OK"

live._store = lambda: ("https://fake-upstash.invalid", "t")
live._redis = fake_redis
EVENTS = collections.deque(maxlen=200000)                  # (kind, seconds) for every desk event

def _worker(self):
    while True:
        kind, data = self.q.get()
        self.busy_since = t0 = time.time()
        try:
            getattr(self, "_on_" + kind)(**data)
        except Exception as e:
            import traceback
            print("desk event failed:", kind, repr(e), traceback.format_exc(limit=-2).strip().splitlines()[-3:])
        EVENTS.append((kind, time.time() - t0))
        self.busy_since, self.beat = None, time.time()

live.Desk._worker = _worker

class Tee:
    """stdout to a log file, counting the lines that mean something went wrong."""
    PATTERNS = ("desk event failed", "message failed", "telegram message failed", "send failed", "Telegram send failed",
                "Telegram send error", "Upstash save failed", "Exception in ASGI", "Traceback", "save skipped",
                "archive failed", "owner chat failed", "negotiation LLM failed", "intake failed")
    def __init__(self, path):
        self.f, self.counts, self.lock, self.out = open(path, "w", encoding="utf-8"), collections.Counter(), threading.Lock(), sys.__stdout__
    def write(self, s):
        with self.lock:
            self.f.write(s)
            for p in self.PATTERNS:
                if p in s:
                    self.counts[p] += 1
    def flush(self):
        self.f.flush()
    def isatty(self):
        return False
    def say(self, *a):
        print(*a, file=self.out, flush=True)

LOG = Tee(Path(tempfile.gettempdir()) / "bhaada_stress.log")
sys.stdout = sys.stderr = LOG
sys.excepthook = lambda *a: __import__("traceback").print_exception(*a, file=sys.__stderr__)
say = LOG.say

import server
E.LLM["key"] = E.LLM["openai_key"] = ""                    # rules only, unless a phase turns a (fake or real) model on
_real_llm_ready = E.llm_ready
E.llm_ready = lambda: True
_real_intake = E.intake_turn
server.RATE.update(perMin=10**6, perHour=10**6, allPerHour=10**7)   # the abuse limits would throttle the simulated crowd
D = server.desk
D.settings.update(mode="live", aiAgents=False, maxDeadheadKm=80)
D.settings["replyTimeoutSec"]["live"] = 6
D.settings["holdTimeoutSec"]["live"] = 8
D.settings["approvalExpireSec"]["live"] = 30
if not D.send_wa:
    D.send_wa = server.desk_send

CITY_NAMES = sorted(set(E.KNOWN_CITIES) | {"Thane", "Vapi", "Vadodara", "Zirakpurgarh"}, key=len, reverse=True)

def intake_rules(hist, load, msg):
    """The intake without a model: cities in the order written, tons, truck type."""
    low = msg.lower()
    found = sorted((low.find(c.lower()), c) for c in CITY_NAMES if c.lower() in low)
    cities = []
    for _, c in found:
        if c not in cities:
            cities.append(c)
    w = re.search(r"(\d+(?:\.\d+)?)\s*(?:ton|t\b)", low)
    raw = {**(load or {}), "material": "general", "pickup_when": "kal"}
    if len(cities) >= 2:
        raw.update(origin=cities[0], destination=cities[1])
    if w:
        raw["weight_ton"] = float(w.group(1))
    if E.norm_type(msg):
        raw["truck_type"] = E.norm_type(msg)
    merged = E.normalise_load(raw)
    merged["material_class"] = "general"
    miss = E.missing_fields(merged)
    return merged, ("Kahan se kahan tak, kitna ton?" if miss else "ok"), miss

E.intake_turn = intake_rules

# ---------------- a real HTTP server, and simulated people answering through it ----------------
import uvicorn, httpx

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

PORT = free_port()
_uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off"))
_uv.install_signal_handlers = lambda: None
threading.Thread(target=_uv.run, daemon=True).start()
while not _uv.started:
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{PORT}"
URL = f"{BASE}/telegram/{server.TG_PATH}"
HTTP = httpx.Client(timeout=30, limits=httpx.Limits(max_connections=200, max_keepalive_connections=200),
                    headers={"X-Telegram-Bot-Api-Secret-Token": server.TG_SECRET})   # as Telegram sends it
POOL = ThreadPoolExecutor(64)
UPD = itertools.count(1)
LAT = collections.defaultdict(list)                        # what -> webhook latencies (s)
HTTP_ERR = collections.Counter()
DUPS = {"p": 0.0, "sent": 0}

def post(update, what="msg", dup_ok=True):
    t0 = time.time()
    try:
        r = HTTP.post(URL, json=update)
        LAT[what].append(time.time() - t0)
        if r.status_code != 200:
            HTTP_ERR[r.status_code] += 1
    except Exception as e:
        HTTP_ERR[type(e).__name__] += 1
    if dup_ok and random.random() < DUPS["p"]:                # Telegram re-delivers the same update
        DUPS["sent"] += 1
        POOL.submit(post, update, what + "-dup", False)

def text(chat, body, what="msg"):
    post({"update_id": next(UPD), "message": {"chat": {"id": chat}, "text": body}}, what)

def tap(chat, data, what="tap"):
    n = next(UPD)
    post({"update_id": n, "callback_query": {"id": f"q{n}", "data": data, "message": {"chat": {"id": chat}}}}, what)

class Sched:
    """Replies after a human-ish delay, without a thread per reply."""
    def __init__(self):
        self.h, self.cv, self.n = [], threading.Condition(), itertools.count()
        threading.Thread(target=self._run, daemon=True).start()
    def later(self, secs, fn, *a):
        with self.cv:
            heapq.heappush(self.h, (time.time() + secs, next(self.n), fn, a))
            self.cv.notify()
    def _run(self):
        while True:
            with self.cv:
                while not self.h or self.h[0][0] > time.time():
                    self.cv.wait(max(0.01, self.h[0][0] - time.time()) if self.h else 1)
                _, _, fn, a = heapq.heappop(self.h)
            POOL.submit(fn, *a)
    def size(self):
        return len(self.h)

SCHED = Sched()
BHID = re.compile(r"\bBH-(\d+)\b")
OWNER_CITIES = ["Bhiwandi", "Mumbai", "Thane", "Pune", "Nashik", "Surat", "Vapi", "Ahmedabad", "Vadodara", "Indore"]
TYPES = [("32 ft MXL", 6), ("32 ft SXL", 2), ("22 ft", 2)]
WEIGHT = {"32 ft MXL": (12, 18), "32 ft SXL": (6, 8), "22 ft": (5, 8)}

class Owner:
    def __init__(self, i, rnd):
        self.chat, self.key = 60000 + i, f"tg:{60000 + i}"
        self.city = OWNER_CITIES[i % len(OWNER_CITIES)]
        self.type = rnd.choices([t for t, _ in TYPES], [w for _, w in TYPES])[0]
        self.busy_until, self.last_lid, self.last_bare, self.which = 0, None, None, 0

class Broker:
    def __init__(self, i):
        self.chat, self.key = 50000 + i, f"tg:{50000 + i}"
        self.pending = collections.deque()                 # send times waiting for their 🔍 BH ack

ACT = {"owners": {}, "brokers": {}, "approve_p": 0.9, "call_yes_p": 0.5, "owner_yes_p": 0.85, "rng": random.Random(7)}
TRACK = {"ack": {}, "sentAt": {}, "quote": collections.Counter(), "quoteAt": {}, "outcome": collections.defaultdict(list),
         "noted": {}, "checkinAt": {}, "which": 0, "replyLag": []}

def target_of(lid, key):
    l = D._find(lid)
    th = (l or {}).get("threads", {}).get(key)
    try:
        return D._econ(l, th)["target"] if th else None
    except Exception:
        return None

def owner_reply(o, body, bare=None):
    o.last_bare = bare or body
    text(o.chat, body, "owner")

def on_owner(o, msg, buttons, t):
    rnd = ACT["rng"]
    m = BHID.search(msg)
    lid = f"BH-{m.group(1)}" if m else None
    d = rnd.uniform(0.3, 2.0)
    if "Noted ✅" in msg:
        TRACK["noted"].setdefault(o.key, t)
    if "Load:" in msg and lid:
        o.last_lid = lid
        tgt = target_of(lid, o.key)
        if o.type in msg and time.time() > o.busy_until and tgt and rnd.random() < 0.9:
            ask = round(tgt * rnd.uniform(0.92, 1.15), 1)
            SCHED.later(d, owner_reply, o, f"{lid} {ask}/km lagega", f"{ask}/km lagega")
        else:
            SCHED.later(d, owner_reply, o, f"{lid} nahi bhai, gaadi free nahi", "nahi bhai, gaadi free nahi")
    elif "kaunse ke liye" in msg:
        o.which += 1
        TRACK["which"] += 1
        if o.which % 4 != 0:                               # a person guesses once or twice, then gives up
            SCHED.later(d, owner_reply, o, f"{o.last_lid} {o.last_bare}")
    elif re.search(r"hold karne ke liye HAAN|hold karni hai\?", msg):
        o.busy_until = time.time() + 40
        SCHED.later(d, owner_reply, o, (f"{lid} " if lid else "") + "haan", "haan")
    elif re.search(r"de sakte hain|badha diya|Aakhri offer|aakhri koshish|chalega 👍", msg) and "Load:" not in msg:
        if lid:
            o.last_lid = lid
        yes = rnd.random() < ACT["owner_yes_p"]
        SCHED.later(d, owner_reply, o, (f"{lid} " if lid else "") + ("haan theek hai" if yes else "nahi bhai isse kam nahi"),
                    "haan theek hai" if yes else "nahi bhai isse kam nahi")
    elif "wala rate chalega" in msg:
        SCHED.later(d, owner_reply, o, (f"{lid} " if lid else "") + "haan chalega", "haan chalega")

def on_broker(b, msg, buttons, t):
    rnd = ACT["rng"]
    m = BHID.search(msg)
    lid = f"BH-{m.group(1)}" if m else None
    if msg.startswith("🔍") and lid:
        TRACK["ack"][lid] = t
        TRACK["sentAt"][lid] = b.pending.popleft() if b.pending else t
        return
    if not lid:
        return
    if buttons and buttons[0][0].startswith("approve:") and not msg.startswith("⏰") and "ab khula nahi" not in msg:
        TRACK["quote"][lid] += 1
        TRACK["quoteAt"].setdefault(lid, t)
        act = "approve" if rnd.random() < ACT["approve_p"] else "reject"
        TRACK["outcome"][lid].append(act)
        SCHED.later(rnd.uniform(0.3, 1.5), tap, b.chat, f"{act}:{lid}")
    elif buttons and buttons[0][0].startswith("call_yes:"):
        TRACK["outcome"][lid].append("call")
        SCHED.later(rnd.uniform(0.3, 1.5), tap, b.chat, buttons[0][0] if rnd.random() < ACT["call_yes_p"] else buttons[1][0])
    for word, tag in (("booking pakki", "booked"), ("dobara book", "recovered"), ("book. Margin", "recovered"),
                      ("❌", "escalated"), ("⌛", "expired"), ("⏳", "waiting"), ("🚨", "escalated")):
        if word in msg:
            TRACK["outcome"][lid].append(tag)

def dispatch():
    while True:
        to, msg, buttons, t = SENT_Q.get()
        try:
            if to in ACT["owners"]:
                on_owner(ACT["owners"][to], msg, buttons, t)
            elif to in ACT["brokers"]:
                on_broker(ACT["brokers"][to], msg, buttons, t)
        except Exception as e:
            say("sim error:", repr(e))

threading.Thread(target=dispatch, daemon=True).start()

HEALTH = []

def sample_health(stop, every=0.5):
    while not stop.is_set():
        try:
            t0 = time.time()
            h = HTTP.get(BASE + "/healthz").json()
            h["lat"], h["t"] = time.time() - t0, t0
            HEALTH.append(h)
        except Exception as e:
            HEALTH.append({"err": repr(e), "t": time.time()})
        stop.wait(every)

# ---------------- helpers for the report ----------------
def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")

def ms(x):
    return f"{x * 1000:.0f} ms" if x == x else "-"

def table(title, head, rows):
    say(f"\n### {title}\n| " + " | ".join(head) + " |\n|" + "---|" * len(head))
    for r in rows:
        say("| " + " | ".join(str(x) for x in r) + " |")

OKS = []

def check(name, cond, detail=""):
    OKS.append(bool(cond))
    say(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

def all_loads():
    """Every load: the ones still on the desk and the ones archived to (fake) Upstash."""
    out = {l["id"]: l for l in list(D.loads)}
    for k, v in list(STORE["archive"].items()):
        out.setdefault(k, json.loads(v))
    return out

def stuck(loads, slack=10):
    t, ttl = live.now(), D.settings["approvalExpireSec"]["live"]
    bad = []
    for l in loads:
        s = l["status"]
        late = (s == "routing" and t - l["createdAt"] > slack) or (s in ("sourcing", "holding") and t > l.get("deadline", t) + slack) \
            or (s in ("needs_call", "needs_approval") and t - l.get("quoteAt", t) > ttl + slack)
        if late:
            bad.append((l["id"], s))
    return bad

def settle(ids, secs=90):
    """Wait until these loads are closed or parked (waiting for trucks)."""
    end = time.time() + secs
    while time.time() < end:
        L = all_loads()
        open_ = [i for i in ids if i in L and L[i]["status"] not in live.Desk.CLOSED + ("waiting_trucks",)]
        if not open_ and SCHED.size() == 0 and D.q.qsize() == 0:
            return []
        time.sleep(0.5)
    return open_

def event_table(title, since=0):
    by = collections.defaultdict(list)
    for k, s in list(EVENTS)[since:]:
        by[k].append(s)
    rows = [(k, len(v), ms(sum(v) / len(v)), ms(pct(v, 95)), ms(max(v))) for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))]
    table(title, ["event", "count", "mean", "p95", "max"], rows)
    return by

def health_summary(since):
    hs = [h for h in HEALTH if h["t"] >= since and "err" not in h]
    return {"samples": len(hs), "queueMax": max((h["queue"] for h in hs), default=0), "busyMax": max((h["busySec"] for h in hs), default=0),
            "outboxMax": max((h["outbox"] for h in hs), default=0), "unsavedMax": max((h["unsavedSec"] for h in hs), default=0),
            "healthP95": pct([h["lat"] for h in hs], 95), "not200": sum(not h.get("ok") for h in hs),
            "errors": sum("err" in h for h in HEALTH if h["t"] >= since)}

def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3

# ---------------- people ----------------
def make_owners(n, rnd):
    owners = [Owner(i, rnd) for i in range(n)]
    for o in owners:
        ACT["owners"][o.key] = o
    return owners

def register(owners):
    t0 = time.time()
    list(POOL.map(lambda o: text(o.chat, f"owner Stress Owner {o.chat}, {o.city}", "register"), owners))
    return time.time() - t0

def checkin_burst(owners, within=3.0, rnd=None):
    rnd = rnd or ACT["rng"]
    t0 = time.time()
    for o in owners:
        TRACK["checkinAt"][o.key] = t0
        TRACK["noted"].pop(o.key, None)
    futs = []
    for o in owners:
        rate = round(rnd.uniform(30, 70))
        SCHED.later(rnd.uniform(0, within), lambda o=o, rate=rate: text(o.chat, f"{o.type}, {o.city}, aaj, ₹{rate}/km", "checkin"))
    end = time.time() + within + 30
    while time.time() < end and len([k for k in TRACK["noted"] if k in TRACK["checkinAt"] and TRACK["noted"][k] >= t0]) < len(owners):
        time.sleep(0.2)
    lag = [TRACK["noted"][o.key] - t0 for o in owners if o.key in TRACK["noted"]]
    return lag

LANES = [(a, b) for a in OWNER_CITIES for b in ["Surat", "Pune", "Ahmedabad", "Nashik", "Indore", "Mumbai", "Vadodara", "Jaipur", "Delhi"] if a != b]

def broker_send(b, rnd):
    o, d = rnd.choice(LANES)
    tt = rnd.choices([t for t, _ in TYPES], [w for _, w in TYPES])[0]
    w = rnd.randint(*WEIGHT[tt])
    b.pending.append(time.time())
    text(b.chat, f"Kal {o} se {d} {w} ton {tt}", "load")

def run_brokers(brokers, per_broker, gap, rnd):
    """Each broker sends per_broker loads, one every ~gap seconds (jittered)."""
    def one(b):
        for _ in range(per_broker):
            broker_send(b, rnd)
            time.sleep(rnd.uniform(0.5, 1.5) * gap)
    list(POOL.map(one, brokers)) if len(brokers) <= 60 else None

def outcome_check(lids, label):
    L = all_loads()
    got = [L[i] for i in lids if i in L]
    quotes = [TRACK["quote"][i] for i in lids]
    ids = [l["id"] for l in got]
    clear = [i for i in lids if TRACK["quote"][i] == 1 or any(x in TRACK["outcome"][i] for x in ("escalated", "expired", "waiting", "recovered"))]
    status = collections.Counter(l["status"] for l in got)
    q2a = [TRACK["quoteAt"][i] - TRACK["sentAt"][i] for i in lids if i in TRACK["quoteAt"] and i in TRACK["sentAt"]]
    ack = [TRACK["ack"][i] - TRACK["sentAt"][i] for i in lids if i in TRACK["ack"]]
    check(f"{label}: every broker load got a BH id, none lost", len(got) == len(lids), f"{len(got)}/{len(lids)}")
    check(f"{label}: ids unique", len(set(ids)) == len(ids))
    check(f"{label}: no load has two quotes", max(quotes or [0]) <= 1, str(collections.Counter(quotes)))
    check(f"{label}: every load got a quote or a clear outcome", len(clear) == len(lids),
          str([(i, L.get(i, {}).get("status"), TRACK["outcome"][i]) for i in lids if i not in clear][:5]))
    st = stuck(got)
    check(f"{label}: nothing stuck past its timers", not st, str(st[:5]))
    return status, q2a, ack

# ---------------- phase 1: throughput ----------------
def phase_throughput():
    say("\n## Phase 1: throughput (rules only, local uvicorn, fake Telegram)")
    rnd = ACT["rng"]
    owners = make_owners(100, rnd)
    brokers = [Broker(i) for i in range(20)]
    for b in brokers:
        ACT["brokers"][b.key] = b
    stop = threading.Event()
    threading.Thread(target=sample_health, args=(stop,), daemon=True).start()
    rows = []
    for ai in (False, True):
        D.settings["aiAgents"] = ai
        if ai:
            D.sandbox_on()
        DUPS["p"] = 0.05
        t0, e0, s0 = time.time(), len(EVENTS), STORE["sets"]
        if not ai:
            reg = register(owners)
            say(f"registered 100 owners in {reg:.1f}s")
        lag = checkin_burst(owners, 3.0, rnd)
        before = set(TRACK["ack"])
        for b in brokers:
            b.pending.clear()
        tb = time.time()
        run_brokers(brokers, 10, 1.5, rnd)
        send_secs = time.time() - tb
        end = time.time() + 20
        while time.time() < end and len(set(TRACK["ack"]) - before) < len(brokers) * 10:
            time.sleep(0.2)
        lids = sorted(set(TRACK["ack"]) - before)
        left = settle(lids, 120)
        if ai:                                           # the AI broker's demo loads, 20 s after each check-in
            time.sleep(1)
        status, q2a, ack = outcome_check(lids, f"AI owners {'on' if ai else 'off'}")
        check(f"AI owners {'on' if ai else 'off'}: all {len(brokers) * 10} broker messages became loads",
              len(lids) == len(brokers) * 10, len(lids))
        demo = [l for l in all_loads().values() if l.get("aiBroker") and l["createdAt"] >= t0]
        hs = health_summary(t0)
        dur = time.time() - t0
        rows.append(["on" if ai else "off", len(lids), f"{send_secs:.0f}s", f"{len(lids) / send_secs * 60:.0f}/min",
                     ms(pct(LAT['load'], 50)), ms(pct(LAT['load'], 95)), ms(pct(LAT['owner'], 95)),
                     f"{pct(ack, 50):.2f}s", f"{pct(q2a, 50):.1f}s / {pct(q2a, 95):.1f}s", hs["queueMax"], hs["busyMax"], hs["outboxMax"],
                     dict(status), len(demo), STORE["sets"] - s0, f"{(STORE['sets'] - s0) / dur * 60:.1f}/min"])
        table(f"check-in burst, AI {'on' if ai else 'off'}", ["owners", "noted", "p50", "p95", "max", "webhook p95"],
              [[len(owners), len(lag), f"{pct(lag, 50):.2f}s", f"{pct(lag, 95):.2f}s", f"{max(lag or [0]):.2f}s", ms(pct(LAT['checkin'], 95))]])
        event_table(f"desk worker events, AI {'on' if ai else 'off'}", e0)
        for k in ("load", "owner", "checkin", "tap", "register"):
            LAT[k + "_all"] = LAT.get(k + "_all", []) + LAT.pop(k, [])
        if left:
            say("still open after settle:", left[:10])
    DUPS["p"] = 0.0
    table("20 brokers x 10 loads, 100 owners", ["AI", "loads", "sent in", "rate", "webhook p50", "webhook p95", "owner wh p95",
          "ack p50", "load→quote p50/p95", "queue max", "busySec max", "outbox max", "final status", "demo loads", "Upstash SETs", "SET rate"], rows)
    say(f"duplicate deliveries sent: {DUPS['sent']}, owners asked 'which load?': {TRACK['which']}, HTTP errors: {dict(HTTP_ERR)}")
    stop.set()

# ---------------- phase 2: long run, memory ----------------
def phase_long():
    say(f"\n## Phase 2: long run ({N_LONG} loads, short timers, tracemalloc)")
    rnd = ACT["rng"]
    if not ACT["owners"]:
        owners = make_owners(100, rnd)
        register(owners)
        checkin_burst(owners, 3.0, rnd)
    owners = list(ACT["owners"].values())
    brokers = [Broker(100 + i) for i in range(20)]
    for b in brokers:
        ACT["brokers"][b.key] = b
    D.settings["aiAgents"] = False
    stop = threading.Event()
    threading.Thread(target=sample_health, args=(stop, 1.0), daemon=True).start()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    t0, s0, b0, w0 = time.time(), STORE["sets"], STORE["bytes"], live.STATE_FILE.writes
    before = set(TRACK["ack"])
    mem = []
    sent = 0
    per = max(1, N_LONG // 20)
    rate = 6.0                                           # loads a second offered to the desk
    nxt = time.time()
    while sent < N_LONG:
        b = brokers[sent % 20]
        POOL.submit(broker_send, b, rnd)
        sent += 1
        if sent % 4 == 0:                                # owners re-check in, as through a day
            o = rnd.choice(owners)
            POOL.submit(text, o.chat, f"{o.type}, {o.city}, aaj, ₹{rnd.randint(30, 70)}/km", "checkin")
        if sent % 200 == 0:
            cur, peak = tracemalloc.get_traced_memory()
            mem.append([sent, f"{(cur - base) / 1e6:.1f} MB", f"{peak / 1e6:.1f} MB", f"{rss_mb():.0f} MB", len(D.loads),
                        len(STORE["archive"]), len(server.CONVO), len(server.LOCKS), len(server.SEEN), f"{live.STATE_FILE.size / 1e3:.0f} KB",
                        D.q.qsize(), server.OUTBOX.qsize()])
        nxt += 1 / rate
        time.sleep(max(0, nxt - time.time()))
    end = time.time() + 30
    while time.time() < end and len(set(TRACK["ack"]) - before) < N_LONG:
        time.sleep(0.3)
    lids = sorted(set(TRACK["ack"]) - before)
    left = settle(lids, 180)
    dur = time.time() - t0
    import gc
    gc.collect()
    cur, peak = tracemalloc.get_traced_memory()
    mem.append(["end", f"{(cur - base) / 1e6:.1f} MB", f"{peak / 1e6:.1f} MB", f"{rss_mb():.0f} MB", len(D.loads), len(STORE["archive"]),
                len(server.CONVO), len(server.LOCKS), len(server.SEEN), f"{live.STATE_FILE.size / 1e3:.0f} KB", D.q.qsize(), server.OUTBOX.qsize()])
    snap = tracemalloc.take_snapshot()
    top = snap.statistics("lineno")[:6]
    tracemalloc.stop()
    stop.set()
    table("memory over the long run", ["loads sent", "traced (net)", "traced peak", "RSS max", "hot loads", "archived", "CONVO keys",
          "LOCKS", "SEEN", "state blob", "queue", "outbox"], mem)
    say("top allocations at the end:\n" + "\n".join("  " + str(s) for s in top))
    status, q2a, ack = outcome_check(lids, "long run")
    hs = health_summary(t0)
    sets = STORE["sets"] - s0
    table("long run", ["loads", "duration", "loads/min", "load→quote p50/p95", "queue max", "busySec max", "outbox max", "Upstash SETs",
                       "SETs/min", "avg SET size", "max SET size", "file writes", "status"],
          [[len(lids), f"{dur:.0f}s", f"{len(lids) / dur * 60:.0f}", f"{pct(q2a, 50):.1f}s / {pct(q2a, 95):.1f}s", hs["queueMax"],
            hs["busyMax"], hs["outboxMax"], sets, f"{sets / dur * 60:.1f}", f"{(STORE['bytes'] - b0) / max(1, sets) / 1e3:.0f} KB",
            f"{STORE['max'] / 1e3:.0f} KB", live.STATE_FILE.writes - w0, dict(status)]])
    by = event_table("desk worker events, long run")
    busy = sum(sum(v) for v in by.values())
    say(f"worker busy {busy:.0f}s of {dur:.0f}s ({busy / dur * 100:.0f}%); open after settle: {left[:5]}")

# ---------------- phase 3: failure injection ----------------
def wait_for(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()

def last_to(chat, since=0):
    return next((t for to, t, b, ts in reversed(SENT) if to == f"tg:{chat}" and ts >= since), "")

def fresh_load(broker, lane=("Bhiwandi", "Surat"), w=14, tt="32 ft MXL", goods=""):
    b = ACT["brokers"].get(f"tg:{broker}") or Broker(broker - 50000)
    ACT["brokers"][b.key] = b
    n0 = set(TRACK["ack"])
    b.pending.append(time.time())
    text(broker, f"Kal {lane[0]} se {lane[1]} {w} ton {goods + ' ' if goods else ''}{tt}", "load")
    wait_for(lambda: set(TRACK["ack"]) - n0, 60)        # after the throughput phase the queue may still be draining
    lid = next(iter(set(TRACK["ack"]) - n0), None)
    return D._find(lid) if lid else None

def phase_faults():
    say("\n## Phase 3: failure injection")
    rnd = ACT["rng"]
    if not ACT["owners"]:
        owners = make_owners(30, rnd)
        register(owners)
        checkin_burst(owners, 2.0, rnd)
    D.settings["aiAgents"] = False
    stop = threading.Event()
    threading.Thread(target=sample_health, args=(stop, 0.5), daemon=True).start()
    rows = []

    # 1. model provider errors: a fake key so every call goes to the (failing) provider
    for mode in ("429", "500", "timeout"):
        E.LLM["key"], E.LLM["openai_key"] = "fake-groq", "fake-openai"
        E.LLM["coolUntil"], E.LLM["cools"], E.LLM["down"] = 0, {}, {}
        LLMF["mode"] = mode
        t0, e0, n0 = time.time(), len(EVENTS), NET["api.openai.com"] + NET["api.groq.com"]
        L = fresh_load(50900)
        ok = L and wait_for(lambda: L["status"] in live.Desk.CLOSED + ("needs_approval", "waiting_trucks"), 150)
        t_close = time.time() - t0
        neg = [s for k, s in list(EVENTS)[e0:] if k == "owner_message"]
        hs = health_summary(t0)
        h = HTTP.get(BASE + "/healthz")
        rows.append([f"LLM {mode}", (L or {}).get("status"), f"{t_close:.0f}s", ms(pct(neg, 50)), ms(max(neg or [0])), hs["busyMax"],
                     hs["not200"], NET["api.openai.com"] + NET["api.groq.com"] - n0, h.status_code])
        check(f"LLM {mode}: the load still closes on the rules", ok, (L or {}).get("status"))
        check(f"LLM {mode}: worker never looks stalled (busySec < 30)", hs["busyMax"] < 30, f"busySec max {hs['busyMax']}")
        E.LLM["key"] = E.LLM["openai_key"] = ""
        settle([L["id"]] if L else [], 30)
    LLMF["mode"] = "down"
    table("model provider failures (one load, ~5 owners negotiating; every model call fails)",
          ["fault", "load status", "to quote/close", "owner msg p50", "owner msg max", "busySec max", "healthz 503s", "model requests", "healthz now"], rows)

    # 2. Upstash down, then up
    STORE["up"] = False
    f0, s0 = STORE["fails"], STORE["sets"]
    L = fresh_load(50901)
    wait_for(lambda: L["status"] in live.Desk.CLOSED + ("needs_approval",) and STORE["fails"] > f0, 40)
    time.sleep(2)
    h = HTTP.get(BASE + "/healthz").json()
    check("Upstash down: desk keeps working", L["status"] in live.Desk.CLOSED + ("needs_approval", "holding"), L["status"])
    check("Upstash down: health shows the store error and unsaved seconds", h["storeError"] and h["unsavedSec"] > 0, str(h))
    tries = STORE["fails"] - f0
    STORE["up"] = True
    wait_for(lambda: STORE["sets"] > s0 and not D._dirty, 40)
    saved = json.loads(getattr(live, "_unpack", str)(STORE["data"].get(live.STATE_KEY) or "{}"))
    check("Upstash back: the state is written again, with the load", L["id"] in [l["id"] for l in saved.get("loads", [])] or L["id"] in STORE["archive"],
          f"failed writes while down: {tries}")

    # 3. geocoder down: an unknown city
    import geo
    for mode in ("down", "timeout"):
        GEO["mode"] = mode
        geo._down.update(geocode=0, route=0)
        geo._missed.clear()
        t0, e0 = time.time(), len(EVENTS)
        D.register_owner("tg:69999", "Geo Test", f"Zirakpurgarh{mode}")      # an owner's unknown base city: pays once
        first = time.time() - t0
        t1 = time.time()
        D.register_owner("tg:69998", "Geo Test 2", f"Qwertypur{mode}")      # the next unknown city, while it is still down
        then = time.time() - t1
        t2 = time.time()
        text(50905, "Kal Bhiwandi se Zirakpurgarh 10 ton 32 ft MXL", "load")  # a load to a city nobody knows: intake asks again
        asked = wait_for(lambda: last_to(50905, t2), 15)
        t_unknown = time.time() - t2
        Lr = fresh_load(50906, ("Pune", "Jaipur"))                          # an uncached road route: OSRM is down too
        wait_for(lambda: Lr and Lr["status"] != "routing", 20)
        Lr2 = fresh_load(50907, ("Nashik", "Delhi"))                        # and the next uncached route right after
        wait_for(lambda: Lr2 and Lr2["status"] != "routing", 20)
        slow = [s for k, s in list(EVENTS)[e0:] if k in ("new_load", "route") and s > 2]
        route = max(slow or [0])
        check(f"geocoder {mode}: after one failed lookup the next ones don't wait on it", then < 2 and t_unknown < 5 and len(slow) <= 1,
              f"first lookup {first:.1f}s, next owner {then:.1f}s, unknown-city load answered in {t_unknown:.1f}s "
              f"({last_to(50905, t2)[:40]!r}), slowest routing event on the worker {route:.1f}s")
        for k in ("tg:69999", "tg:69998"):
            D.owners.pop(k, None)
    GEO["mode"] = "down"
    geo._down.update(geocode=0, route=0)

    # 4. Telegram send failures
    for mode, p in (("429", 0.5), ("500", 0.3), ("timeout", 0.3)):
        TG.update(mode=mode, p=p, attempts=0, delivered=0, lost=0, hang=3.0)
        t0 = time.time()
        L = fresh_load(50902)
        ok = L and wait_for(lambda: L["status"] in live.Desk.CLOSED + ("needs_approval",), 60)
        hs = health_summary(t0)
        TG["mode"] = "ok"
        wait_for(lambda: server.OUTBOX.qsize() == 0, 60)
        check(f"Telegram {mode} ({int(p * 100)}%): desk keeps going", True, f"load {(L or {}).get('status')}, attempts {TG['attempts']}, "
              f"failed {TG['lost']}, delivered {TG['delivered']}, outbox max {hs['outboxMax']}")
        if L:
            settle([L["id"]], 40)

    # 5. bad webhook bodies
    r = [HTTP.post(URL, content=b"{not json", headers={"content-type": "application/json"}).status_code,
         HTTP.post(URL, json=[1, 2, 3]).status_code,
         HTTP.post(URL, json={"update_id": next(UPD), "message": {"chat": {}, "text": "hi"}}).status_code,
         HTTP.post(URL, json={"update_id": next(UPD), "callback_query": {"data": "approve:BH-1"}}).status_code,
         HTTP.post(URL, json={"update_id": next(UPD), "message": {"chat": {"id": 1}, "text": None}}).status_code]
    check("malformed webhook bodies get a 2xx/4xx, never a 500 (Telegram retries 5xx)", all(c < 500 for c in r), str(r))
    t0 = time.time()
    big = HTTP.post(URL, json={"update_id": next(UPD), "message": {"chat": {"id": 59001}, "text": "Bhiwandi se Surat " + "क" * 2_000_000}})
    check("a 6 MB message is answered without hanging", big.status_code < 500 and time.time() - t0 < 10, f"{big.status_code} in {time.time() - t0:.1f}s")
    time.sleep(1)
    chats = D.brokers.get("tg:59001", {}).get("chat", [])
    check("a huge message is not kept whole in the broker's chat", max([len(m["text"]) for m in chats] or [0]) < 5000,
          f"{max([len(m['text']) for m in chats] or [0])} chars")
    for body in ("🚚🚚🚚 नमस्ते भाई 🙏", "कल भिवंडी से सूरत 14 टन 32 MXL", "‮\u0000﻿ zero-width​", "Ｂｈｉｗａｎｄｉ से Surat 14 ton 32 ft MXL"):
        text(59002, body, "unicode")
    time.sleep(1)
    check("emoji/Devanagari/control characters: no handler crashed", LOG.counts["message failed"] + LOG.counts["telegram message failed"] == 0,
          dict(LOG.counts))

    # 6. rapid duplicate taps, approve+reject together, two people tapping
    L = fresh_load(50903)
    ACT["approve_p"] = -1                               # the simulated broker does not tap; this test does
    wait_for(lambda: L["status"] == "needs_approval", 40)
    ACT["approve_p"] = 0.9
    if "--no-tap-guard" in sys.argv:                     # shows the bug the guard fixes: every tap hears its own success
        class Forget(dict):
            def __setitem__(self, k, v):
                pass
        server.DECIDED = Forget()
    if L["status"] == "needs_approval":
        D._on_stall = lambda secs: time.sleep(secs)       # the worker busy on a model call, as when owners negotiate
        D.post("stall", secs=2)
        t0 = time.time()
        acks = []
        list(POOL.map(lambda d: tap(*d), [(50903, f"approve:{L['id']}")] * 5 + [(50903, f"reject:{L['id']}")] + [(50999, f"approve:{L['id']}")] * 3))
        time.sleep(3.5)
        said = [t for to, t, b, ts in SENT if to == "tg:50903" and ts >= t0]
        check("5 approves + 1 reject at once: one outcome", L["status"] in ("holding", "booked", "rejected"), L["status"])
        contradict = any("Reject kar diya" in t for t in said) and L["status"] != "rejected" or \
            any("Approve ho gaya" in t for t in said) and L["status"] == "rejected"
        check("the broker is never told the opposite of what happened", not contradict,
              f"status {L['status']}; broker heard: {[t[:30] for t in said]}")
        other = [t for to, t, b, ts in SENT if to == "tg:50999" and ts >= t0]
        check("someone else's taps change nothing", all("kaam ka nahi" in t for t in other), str([t[:40] for t in other]))
    # owner replies after the round closed
    L = fresh_load(50904)
    wait_for(lambda: L["status"] not in ("routing", "sourcing"), 40)
    late = next((k for k, th in L["threads"].items() if th["status"] in ("silent", "declined")), None)
    if late:
        t0 = time.time()
        text(int(late[3:]), "35/km chalega", "owner")
        wait_for(lambda: last_to(late[3:], t0), 5)
        check("an owner answering a closed round gets a sensible reply", bool(last_to(late[3:], t0)) and L["threads"][late]["status"] in ("silent", "declined"),
              last_to(late[3:], t0)[:60])

    # 7. restart mid-flow: a new desk from the saved state carries on
    ACT["approve_p"] = -1
    open_loads = [fresh_load(50910 + i, lane) for i, lane in enumerate([("Bhiwandi", "Surat"), ("Pune", "Surat"), ("Surat", "Pune")])]
    time.sleep(2)
    D.flush_now()
    old = server.desk
    old.post = lambda *a, **k: None                      # the old process is gone: no more events, ticks or writes
    old._on_tick = old.flush = old.save = lambda *a, **k: None
    new = live.Desk(sender=server.desk_send)
    new.convo = server.CONVO
    new.settings.update(old.settings)
    server.desk = new
    globals()["D"] = new
    ACT["approve_p"] = 0.9
    states = {l["id"]: l["status"] for l in open_loads if l}
    for lid, st in states.items():
        if st == "needs_approval":
            tap(50910 + list(states).index(lid), f"approve:{lid}")
    left = settle(list(states), 60)
    after = {i: (new._find(i) or json.loads(STORE["archive"].get(i, "{}"))).get("status") for i in states}
    check("restart: loads saved mid-flow finish on the new desk", not left, f"{states} -> {after}")
    check("restart: no seq reuse", len({l["id"] for l in new.loads}) == len(new.loads) and new._seq >= max(int(i[3:]) for i in states))
    stop.set()

# ---------------- phase 4: the real model chain, at most 40 calls ----------------
def phase_real():
    say("\n## Phase 4: real model chain (5 brokers at once; <= 40 calls)")
    sec = E.local_secrets()
    if not (sec.get("OPENAI_API_KEY") or sec.get("GROQ_API_KEY")):
        return say("no keys in .streamlit/secrets.toml: skipped")
    E.configure(key=sec.get("GROQ_API_KEY"), model=sec.get("GROQ_MODEL"), openai_key=sec.get("OPENAI_API_KEY"))
    E.LLM["coolUntil"], E.LLM["cools"] = 0, {}
    E.llm_ready = _real_llm_ready
    E.intake_turn = _real_intake
    REAL["allow"] = True
    D.settings["aiAgents"] = False
    for k in [k for k, o in D.owners.items() if not o.get("sandbox")]:
        D.owners.pop(k, None)                            # no owners: no negotiation calls, the load waits for trucks
    D.pool = []
    brokers = [Broker(300 + i) for i in range(5)]
    msgs = ["Kal Bhiwandi se Surat 14 ton kapda", "Pune se Ahmedabad 10 ton steel saria parso", "Surat to Delhi 18 ton cartons tomorrow",
            "कल नासिक से इंदौर 12 टन प्याज", "Mumbai se Jaipur 9 ton machinery"]
    timing = collections.defaultdict(list)

    def converse(b, first):
        ACT["brokers"][b.key] = b
        for what, body in (("intake", first), ("thanks", "thank you bhai"), ("status", "status?")):
            t0 = time.time()
            n0 = len([1 for to, *_ in SENT if to == b.key])
            text(b.chat, body, what)
            wait_for(lambda: len([1 for to, *_ in SENT if to == b.key]) > n0, 60)
            timing[what].append(time.time() - t0)
            if what == "intake" and not b.pending and "🔍" not in last_to(b.chat):
                wait_for(lambda: "🔍" in last_to(b.chat) or "?" in last_to(b.chat), 5)
    t0 = time.time()
    if "--neg-only" in sys.argv:                          # the brokers' part was already measured: spend the calls on this
        brokers, msgs = [], []
    else:
        list(ThreadPoolExecutor(5).map(converse, brokers, msgs))
        # OpenAI down (fake 503) while Groq is real: the chain should fall back cleanly
        REAL["fake"], LLMF["mode"] = {"api.openai.com"}, "500"
        fb = Broker(310)
        ACT["brokers"][fb.key] = fb
        for what, body in (("fallback intake", "Kal Pune se Surat 10 ton steel saria"), ("fallback thanks", "shukriya bhai")):
            t1 = time.time()
            n0 = len([1 for to, *_ in SENT if to == fb.key])
            text(fb.chat, body, what)
            wait_for(lambda: len([1 for to, *_ in SENT if to == fb.key]) > n0, 60)
            timing[what].append(time.time() - t1)
        REAL["fake"], LLMF["mode"] = set(), "down"
        brokers.append(fb)
        msgs.append("(OpenAI down) Kal Pune se Surat 10 ton steel saria / shukriya bhai")
    # one real negotiation: 2 owners check in (the model reads it), a load, owners quote and counter on the worker
    owners = []
    for i in range(2):
        o = Owner(900 + i, ACT["rng"])
        o.city, o.type = "Bhiwandi", "32 ft MXL"
        ACT["owners"][o.key] = o
        owners.append(o)
        text(o.chat, f"owner Real Owner {i + 1}, Bhiwandi", "register")
    time.sleep(0.5)
    for o in owners:                                      # the check-in straight to the desk: the calls go to negotiation
        line = f"32 ft MXL, Bhiwandi, aaj, ₹{35 + owners.index(o) * 3}/km"
        D.post("checkin_apply", key=o.key, text=line, trucks=D._checkin_rules(D.owners[o.key], line))
    wait_for(lambda: len([o for o in owners if any(t["ownerKey"] == o.key for t in D.pool)]) == len(owners), 10)
    e0 = len(EVENTS)
    nb = Broker(320)
    ACT["brokers"][nb.key] = nb
    t1 = time.time()
    L = fresh_load(nb.chat, ("Bhiwandi", "Surat"), 14, "32 ft MXL", "kapda")
    wait_for(lambda: L and L["status"] in live.Desk.CLOSED + ("needs_approval", "holding", "waiting_trucks"), 90)
    timing["load → quote (2 owners, model negotiating)"].append(time.time() - t1)
    neg = [s for k, s in list(EVENTS)[e0:] if k == "owner_message"]
    timing["worker time per owner reply (model)"] += neg
    say(f"real negotiation: load {(L or {}).get('status')}, {len(neg)} owner replies on the worker, "
        f"worker busy {sum(s for k, s in list(EVENTS)[e0:]):.1f}s")
    REAL["allow"] = False
    E.LLM["key"] = E.LLM["openai_key"] = ""
    table("5 brokers on the real chain", ["step", "n", "p50", "max"],
          [[k, len(v), f"{pct(v, 50):.1f}s", f"{max(v):.1f}s"] for k, v in timing.items() if v])
    say(f"real calls used {REAL['used']}, hosts {dict(NET)}")
    by = collections.defaultdict(list)
    for c in REAL["log"]:
        by[(c["host"], c["model"], c["status"])].append(c)
    table("real calls", ["host", "model", "status", "calls", "p50 s", "max s", "tokens in (sum)", "tokens out (sum)"],
          [[h, m, s, len(v), f"{pct([c['secs'] for c in v], 50):.1f}", f"{max(c['secs'] for c in v):.1f}",
            sum(c["in"] or 0 for c in v), sum(c["out"] or 0 for c in v)] for (h, m, s), v in by.items()])
    lim = {k: v for c in REAL["log"] for k, v in c["limits"].items()}
    say(f"real calls used: {REAL['used']}/40 in {time.time() - t0:.0f}s; rate-limit headers seen: {lim}")
    for b, m in zip(brokers, msgs):
        say(f"  {m[:40]!r}: " + " | ".join(t[:70].replace("\n", " ") for to, t, *_ in SENT if to == b.key))

T_START = time.time()
for ph in PHASES:
    {"throughput": phase_throughput, "long": phase_long, "faults": phase_faults, "real": phase_real}[ph]()
say(f"\nlog lines that mean trouble: {dict(LOG.counts)}")
say(f"outside hosts the desk tried (all faked unless 'real'): {dict(NET)}")
say(f"total {time.time() - T_START:.0f}s; log at {LOG.f.name}")
check("no 'desk event failed' in the worker", LOG.counts["desk event failed"] == 0, str(LOG.counts["desk event failed"]))
say("\nALL PASS" if all(OKS) else f"\nSOME FAILED ({OKS.count(False)})")
sys.__stdout__.flush()
os._exit(0 if all(OKS) else 1)
