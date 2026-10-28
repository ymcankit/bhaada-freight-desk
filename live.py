"""
The live desk: loads worked against real fleet owners on WhatsApp, in real time.

Everything runs through one worker that takes events in order (a broker message, an owner reply, a button,
the clock), so a load's state never races. What is real and what is not:
  - owners: in "live" mode they are people on WhatsApp who registered and answered the morning check-in;
    in "sandbox" mode they are personas from data/owners.json, labelled as such everywhere;
  - distance: OSRM road distance for any pair of Indian cities (geo.py);
  - costs: diesel, tolls, driver and margin band come from settings with their source and date;
  - negotiation: an LLM agent reads each owner's reply and answers in Hinglish, inside a ceiling the code enforces;
  - price: only engine.price_quote, never the LLM.
"""
import json, os, queue, re, threading, time, hashlib, hmac, traceback
import requests
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import engine as E
import geo

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = Path(__file__).parent / "data" / "live_state.json"
STATE_KEY = "bhaada:live_state"

ARCHIVE_KEY = "bhaada:archive"                 # closed loads, one hash field each, written once and never re-sent
ARCHIVE_FILE = Path(__file__).parent / "data" / "live_archive.jsonl"
_SEC = E.local_secrets()
ALIAS_SALT = os.urandom(16)                    # the console's owner ids: stable for this process, meaningless outside it

def _store():
    """Upstash Redis (free, REST) when configured: free hosts wipe local files on every restart and deploy."""
    sec = _SEC
    url = (os.getenv("UPSTASH_REDIS_REST_URL") or sec.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
    tok = os.getenv("UPSTASH_REDIS_REST_TOKEN") or sec.get("UPSTASH_REDIS_REST_TOKEN") or ""
    return (url, tok) if url.startswith("https://") and tok else None

def _redis(cmd):
    url, tok = _store()
    r = requests.post(url, headers={"Authorization": f"Bearer {tok}"}, json=cmd, timeout=8)
    r.raise_for_status()
    return r.json().get("result")
geo.seed_cities(E.CITY["cities"])
E.CITY_RESOLVER = geo.resolve_city

YES = re.compile(r"\b(haan|haa|ha|han|yes|yep|ok|okay|pakka|confirm|confirmed|done|theek|thik|chalega|sure|book)\b|👍|✅", re.I)
NO = re.compile(r"\b(nahi|nahin|nhi|no|cancel|sorry|nikal|nikal gayi|not|mat)\b|^\W*na+\W*$|❌", re.I)   # "haan na" is a yes
CANCEL = re.compile(r"cancel|back\s*out|nahi\s+(aa|ja|ho)\s+(pa|sak)|nikal\s+ga(yi|ya)|mana\s+kar", re.I)

def now():
    return time.time()

def clock(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S") if ts else ""

DEFAULT_SETTINGS = {
    "mode": "sandbox",                       # "live" once real owners have registered
    "dieselPerLitre": E.rates["dieselPerLitre"],
    "dieselSource": "Retail diesel, Mumbai (IOCL). Update daily.",
    "dieselAsOf": datetime.now(IST).strftime("%Y-%m-%d"),
    "tollNote": "Estimated per km by axle class (NHAI plaza rates vary by stretch).",
    "floorPct": E.MARGIN["floorPct"], "targetPct": E.MARGIN["targetPct"], "ceilingPct": E.MARGIN["ceilingPct"],
    "replyTimeoutSec": {"live": 300, "sandbox": 30},
    "holdTimeoutSec": {"live": 1800, "sandbox": 30},        # an owner on a slow phone gets 30 min to say HAAN
    "approvalExpireSec": {"live": 3600, "sandbox": 900},    # a quote nobody approves stops holding the owner's truck
    "terms": {"advancePct": 80, "balanceDays": 7, "freeHours": 24, "detentionPerDay": 1500, "hamali": "party",
              "gstMode": "RCM5"},                           # the agency's standard terms: owners hear them, quotes print them
    "aiAgents": True,                                       # AI owners bid beside real ones; an AI broker posts demo loads
    "maxRounds": 3,
    "maxDeadheadKm": 80,
    "defaultReturnLoadProb": 0.5,
}

class Desk:
    def __init__(self, sender=None):
        self.send_wa = sender                # callable(to, text, buttons=None) or None
        self.q = queue.Queue()
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.owners = {}                     # key -> owner (live: key is the WhatsApp number)
        self.pool = []                       # trucks offered at today's check-in
        self.loads = []
        self.brokers = {}                    # WhatsApp number -> intake chat in progress
        self.reliability = {}                # learned penalties, ownerKey -> delta
        self.lane_rates = {}                 # "Origin-Destination|type" -> recent agreed buy ₹/km: lane memory
        self.checkin = {"sentAt": None, "asked": 0}
        self._seq = 1000
        self._seq_lock = threading.Lock()
        self.ai_due = {}                     # ownerKey -> when the AI broker posts them a demo load (clock-driven)
        self._dirty, self._saved_at, self._saved_hash, self._remote_retry = False, 0.0, None, 0.0
        self.store_error, self.beat, self.busy_since, self._tick_due = None, time.time(), None, False
        self._load()
        self._apply_settings()
        for l in self.loads:                 # a restart between intake and routing: route it now, not escalate in 3 min
            if l["status"] == "routing":
                self.post("route", lid=l["id"])
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._clock, daemon=True).start()

    # ---------------- persistence ----------------
    def _load(self):
        s = None
        self.remote_ok = True
        if _store():
            for i in range(3):
                try:
                    raw = _redis(["GET", STATE_KEY])
                    s = json.loads(raw) if raw else None
                    print("state loaded from Upstash:", "found" if s else "empty")
                    break
                except Exception as e:
                    print("Upstash load failed:", repr(e))
                    time.sleep(i + 1 if i < 2 else 0)
            else:
                self.remote_ok = False         # never overwrite what Upstash holds with a blank desk; merge once it answers
        if s is None:
            try:
                s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return
        self.settings.update(s.get("settings", {}))
        self.owners = s.get("owners", {})
        self.pool = s.get("pool", [])
        self.loads = s.get("loads", [])
        self.reliability = s.get("reliability", {})
        self.lane_rates = s.get("laneRates", {})
        self.brokers = s.get("brokers", {})
        self.ai_due = s.get("aiDue", {})
        self._seq = s.get("seq", self._seq)

    def _merge_remote(self, s):
        """Upstash was down at boot: fold what it holds into what this process learned since."""
        for k, o in s.get("owners", {}).items():
            self.owners.setdefault(k, o)
        for name in ("reliability", "brokers", "lane_rates"):
            for k, v in s.get(name, {}).items():
                getattr(self, name).setdefault(k, v)
        mine = {t["ownerKey"] for t in self.pool}
        self.pool = self.pool + [t for t in s.get("pool", []) if t["ownerKey"] not in mine]
        ids = {l["id"] for l in self.loads}
        self.loads = sorted([l for l in s.get("loads", []) if l["id"] not in ids] + self.loads, key=lambda l: l["createdAt"])
        self._seq = max(self._seq, s.get("seq", 0))
        print("merged the Upstash state:", len(s.get("owners", {})), "owners")

    CLOSED = ("escalated", "rejected", "booked", "recovered")

    def _on_save(self):
        self.save()

    SAVE_GAP = 5                                       # seconds between writes: Upstash free is 500K commands, 10 GB a month
    HOT_DAYS, HOT_MAX = 3, 150                         # closed loads older than this leave the state blob for the archive

    def save(self):
        """Ask for a write. A burst of changes becomes one write; the clock writes whatever is left (_on_tick)."""
        self._dirty = True
        if now() - self._saved_at >= self.SAVE_GAP:
            self.flush()

    def _archive(self):
        """Move old closed loads out of the blob that is re-sent on every save; each one is written once."""
        old = now() - self.HOT_DAYS * 86400
        last = lambda l: max(l.get("closedAt") or 0, l.get("bookedAt") or 0, l["createdAt"])
        out = [l for l in self.loads if l["status"] in self.CLOSED and last(l) < old]
        spare = len(self.loads) - len(out) - self.HOT_MAX
        if spare > 0:
            ids = {l["id"] for l in out}
            out += [l for l in self.loads if l["status"] in self.CLOSED and l["id"] not in ids][:spare]
        if not out:
            return
        try:
            if _store():
                if not self.remote_ok:
                    return
                _redis(["HSET", ARCHIVE_KEY] + [x for l in out for x in (l["id"], json.dumps(l, ensure_ascii=False))])
            else:
                with ARCHIVE_FILE.open("a", encoding="utf-8") as f:
                    f.writelines(json.dumps(l, ensure_ascii=False) + "\n" for l in out)
        except Exception as e:
            return print("archive failed, loads kept:", repr(e))
        gone = {l["id"] for l in out}
        self.loads = [l for l in self.loads if l["id"] not in gone]

    def flush(self):
        self._dirty, self._saved_at = False, now()
        if _store() and not self.remote_ok and now() >= self._remote_retry:
            try:
                raw = _redis(["GET", STATE_KEY])
                if raw:
                    self._merge_remote(json.loads(raw))
                self.remote_ok = True
            except Exception as e:
                self._remote_retry = now() + 30           # not on every save: each try holds the worker up to the timeout
                self.store_error = repr(e)[:120]
                print("Upstash still unreachable, saving locally only:", repr(e))
        self._archive()
        for attempt in range(5):                       # the server thread may add an owner or a broker chat meanwhile
            try:
                live_owners = {k: v for k, v in list(self.owners.items()) if not v.get("sandbox")}
                data = {"settings": self.settings, "owners": live_owners, "reliability": self.reliability, "seq": self._seq, "laneRates": self.lane_rates,
                        "pool": [t for t in self.pool if t.get("source") == "checkin"], "loads": self.loads,
                        "brokers": self.brokers, "aiDue": self.ai_due}
                text = json.dumps(data, ensure_ascii=False)
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            self._dirty = True
            return print("save skipped: state kept changing")
        digest = hashlib.sha1(text.encode()).hexdigest()
        if digest == self._saved_hash:                 # nothing changed since the last write: no request at all
            return
        try:
            STATE_FILE.write_text(text, encoding="utf-8")
        except Exception:
            pass
        if not _store():
            self._saved_hash = digest
        elif self.remote_ok:
            try:
                _redis(["SET", STATE_KEY, text])
                self._saved_hash, self.store_error = digest, None
            except Exception as e:
                self._dirty, self.store_error = True, repr(e)[:120]
                self._saved_at = now() + 25                # try again in ~30 s, not on every change
                print("Upstash save failed:", repr(e))

    def _on_flush(self, done=None):
        self.flush()
        if done:
            done.set()

    def flush_now(self, wait=10):
        """From another thread (shutdown): the worker writes what it has, then this returns."""
        done = threading.Event()
        self.post("flush", done=done)
        return done.wait(wait)

    def health(self):
        busy = time.time() - self.busy_since if self.busy_since else 0
        return {"worker": "stalled" if busy > 90 else "ok", "busySec": round(busy), "queue": self.q.qsize(),
                "store": ("upstash" if self.remote_ok else "upstash unreachable") if _store() else "local file",
                "storeError": self.store_error, "unsavedSec": round(now() - self._saved_at) if self._dirty else 0}

    def _apply_settings(self):
        E.rates["dieselPerLitre"] = float(self.settings["dieselPerLitre"])
        for k in ("floorPct", "targetPct", "ceilingPct"):
            E.MARGIN[k] = float(self.settings[k])

    def timeout(self):
        return self.settings["replyTimeoutSec"][self.settings["mode"]]

    # ---------------- event plumbing ----------------
    def post(self, kind, **data):
        self.q.put((kind, data))

    def _clock(self):
        while True:
            time.sleep(1)
            if not self._tick_due:                      # a busy worker gets one tick when it is free, not a backlog
                self._tick_due = True
                self.post("tick")

    def _worker(self):
        while True:
            kind, data = self.q.get()
            self.busy_since = time.time()
            try:
                getattr(self, "_on_" + kind)(**data)
            except Exception as e:
                print("desk event failed:", kind, repr(e), traceback.format_exc(limit=-2).strip().splitlines()[-3:])
            self.busy_since, self.beat = None, time.time()

    # ---------------- messaging ----------------
    def _to_owner(self, load, th, text):
        th["messages"].append({"dir": "out", "text": text, "t": now()})
        o = self.owners.get(th["ownerKey"])
        if not o:
            return
        if o.get("sandbox"):
            self._sim_reply(load, th)
        elif self.send_wa:
            self.send_wa(o["phone"], text)

    def _to_broker(self, load, text, buttons=None):
        load["brokerMessages"].append({"text": text, "t": now()})
        if load.get("brokerPhone") and self.send_wa:
            self.send_wa(load["brokerPhone"], text, buttons)

    def _event(self, load, actor, text):
        load["events"].append({"t": now(), "actor": actor, "text": text})

    # ---------------- owners ----------------
    def register_owner(self, phone, name, city=None):
        hit = geo.resolve_city(city) if city else None
        self.owners[phone] = {"key": phone, "phone": phone, "name": name.strip()[:40],
                              "baseCity": hit["name"] if hit else (city or ""), "reliability": 4.5,
                              "registeredAt": now(), "sandbox": False}
        self.post("save")                              # called from the server thread: the worker does the writing
        self.post("retry_waiting")                   # a new owner is asked about loads still waiting for a truck
        return self.owners[phone]

    def sandbox_on(self):
        """Load the persona owners and their trucks as today's pool, marked sandbox. Built aside, then swapped in,
        because the console calls this while the worker may be reading the owners."""
        owners = {k: v for k, v in list(self.owners.items()) if not v.get("sandbox")}
        pool = [t for t in self.pool if t.get("source") != "sandbox"]
        for o in E.owners:
            if not o["behavior"]["respondsToCheckin"]:
                continue
            key = "sb:" + o["ownerId"]
            owners[key] = {"key": key, "phone": None, "name": o["name"], "baseCity": o["baseCity"],
                           "reliability": o["reliability"], "sandbox": True, "behavior": o["behavior"]}
            for tid in o["truckIds"]:
                t = E.TRUCKS_BY_ID.get(tid)
                if t:
                    pool.append({"ownerKey": key, "type": t["type"], "capacityTon": t["capacityTon"],
                                 "city": t["currentCity"], "available": t["availableFrom"], "ratePerKm": None,
                                 "source": "sandbox", "at": now()})
        self.owners, self.pool = owners, pool

    def reliability_of(self, key):
        o = self.owners.get(key, {})
        return round(max(1.0, o.get("reliability", 4.5) + self.reliability.get(key, 0)), 1)

    # ---------------- inbound ----------------
    # ---------------- terms and paperwork ----------------
    def terms_line(self):
        t = {**DEFAULT_SETTINGS["terms"], **(self.settings.get("terms") or {})}
        return (f"Advance {t['advancePct']}% loading pe, baaki POD milne ke {t['balanceDays']} din mein. {t['freeHours']} ghante free, "
                f"phir ₹{t['detentionPerDay']:,}/din detention. Loading/unloading (hamali) {'party' if t['hamali'] == 'party' else 'owner'} ki.")

    def paperwork_lines(self, load):
        """What the shipper and broker must sort before dispatch: e-way bill validity and the GST line."""
        f, km = load["load"], load["lane"]["distanceKm"]
        t = {**DEFAULT_SETTINGS["terms"], **(self.settings.get("terms") or {})}
        days = max(1, -(-km // 200))                    # 1 day per 200 km for normal cargo
        inv = f.get("invoice_value_rs")
        ewb = (f"E-way bill zaroori (maal ₹{inv:,.0f}), Part B mein gaadi number; valid {days} din ({km} km)" if inv and inv >= 50000
               else f"E-way bill: maal ₹50,000+ ho to zaroori; valid {days} din ({km} km)" if not inv
               else "E-way bill: ₹50,000 se kam maal, zaroori nahi (state rule check kar lein)")
        if f.get("material_class") == "agri":
            gst = "GST: anaaj/agri maal par GTA GST chhoot"
        elif t["gstMode"] == "RCM5":
            gst = "GST 5% RCM: party (shipper) seedha bharegi, bilty par likhein"
        else:
            gst = "GST forward charge, bilty par likhein (rate CA se confirm)"
        return [ewb, gst]

    DISPATCH_RE = re.compile(r"\b([A-Z]{2})[ -]?(\d{1,2})[ -]?([A-Z]{1,3})[ -]?(\d{3,4})\b", re.I)
    PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[ -]?)?([6-9]\d{9})(?!\d)")
    STATE_CODES = set("AN AP AR AS BR CH CG DD DL GA GJ HR HP JK JH KA KL LA LD MP MH MN ML MZ NL OD OR PY PB RJ SK TN TS TR UP UK UA WB".split())

    def _ask_dispatch(self, load, th):
        th["needDispatch"] = True
        self._to_owner(load, th, f"{load['id']}: gaadi ka number aur driver ka mobile bhejiye (e.g. *MH04AB1234, 98XXXXXXXX*). "
                                 "Broker ko gate pass aur e-way bill ke liye chahiye.")
        if th.get("sandbox"):
            seq = int(hashlib.md5(th["ownerKey"].encode()).hexdigest(), 16)
            reply = f"MH04 DM {1000 + seq % 9000}, driver 90000{seq % 100000:05d}"
            threading.Timer(3, lambda: self.post("owner_message", key=th["ownerKey"], text=reply, lid=load["id"])).start()

    def _take_dispatch(self, load, th, text):
        """The owner's vehicle and driver for a booked load: checked, stored, and sent to the broker as a dispatch card."""
        v, ph = self.DISPATCH_RE.search(text), self.PHONE_RE.search(text)
        if not v and not ph:
            return False
        th["messages"].append({"dir": "in", "text": text, "t": now()})
        if v:
            th["vehicle"] = "".join(v.groups()).upper()
        if ph:
            th["driverPhone"] = ph.group(1)
        if not (th.get("vehicle") and th.get("driverPhone")):
            self._to_owner(load, th, "Shukriya 🙏 " + ("Driver ka mobile number bhi bhejiye." if th.get("vehicle") else "Gaadi number bhi bhejiye."))
            return True
        th["needDispatch"] = False
        state = th["vehicle"][:2]
        check = ("number ka format sahi, VAHAN check: demo (abhi juda nahi)" if state in self.STATE_CODES
                 else f"⚠️ '{state}' koi state code nahi, number dobara confirm karein")
        demo = " (demo gaadi)" if th.get("sandbox") else ""
        lane, f = load["lane"], load["load"]
        self._event(load, "Booking", f"Dispatch: vehicle {th['vehicle']}{demo}, driver ••••{th['driverPhone'][-4:]}; {check}.")
        self._to_owner(load, th, "Dhanyavaad ✅ Details broker ko bhej di. " + self.terms_line())
        self._to_broker(load, f"🚛 {load['id']} dispatch: gaadi *{th['vehicle']}*{demo} · driver {th['driverPhone']} · {th['name']}\n"
                              f"{lane['origin']} → {lane['destination']}, {f.get('pickup_when') or 'kal'} · {check}\n"
                              + "\n".join("🧾 " + x for x in self.paperwork_lines(load))
                              + ("\n🔁 Gaadi badli hai: e-way bill Part B mein naya number update kijiye." if load.get("phase") == "replan" else ""))
        self.save()
        return True

    def _held_needing_dispatch(self, key, lid=None):
        for load in reversed(self.loads):
            th = load["threads"].get(key)
            if th and th["status"] == "held" and th.get("needDispatch") and (not lid or load["id"] == lid):
                return load, th
        return None

    def _on_owner_message(self, key, text, lid=None):
        held = self._held_needing_dispatch(key, lid)
        if held and self._take_dispatch(*held, text):
            return
        live_states = ("sent", "countered", "hold_requested", "reconfirm")
        open_now = [l for l in self.loads if (l["threads"].get(key) or {}).get("status") in live_states]
        named = re.search(r"\bBH[-\s]?(\d{4,})\b", text, re.I)
        pick = lid or (f"BH-{named.group(1)}" if named else None)
        ids = {l["id"] for l in open_now}
        if lid and lid not in ids:
            return                                      # a persona answering a round that has already closed
        if pick in ids or len(open_now) > 1:
            load = next((l for l in open_now if l["id"] == pick), None)
            if not load:                                # two negotiations at once and the message doesn't say which
                o = self.owners.get(key) or {}
                if not o.get("sandbox"):
                    self._wa(o, "Aapke paas do load khule hain, kaunse ke liye likh rahe hain? 🙏\n" + "\n".join(
                        f"{l['id']}: {l['lane']['origin']} → {l['lane']['destination']}" for l in open_now)
                        + "\nLoad number ke saath likhiye, e.g. *" + open_now[-1]["id"] + " 38/km*")
                return
            th = load["threads"][key]
            th["messages"].append({"dir": "in", "text": text, "t": now()})
            text = re.sub(r"\bBH[-\s]?\d{4,}\b", " ", text, flags=re.I)      # the load number is not a rate
            return self._owner_reply(load, th, text)
        th, load = self._open_thread(key)
        if th and th["status"] == "held":
            # a booked owner writes about many things (payment, timing, tomorrow's truck); only a clear cancel un-books
            if CANCEL.search(text) and load["status"] in ("booked", "recovered") and now() - (load.get("bookedAt") or 0) < 48 * 3600:
                th["messages"].append({"dir": "in", "text": text, "t": now()})
                th["status"] = "backed_out"
                self._event(load, "Booking", f"{th['name']} cancelled: “{text[:60]}”.")
                return self._replan(load, key)
            th = None
        if th:
            th["messages"].append({"dir": "in", "text": text, "t": now()})
            return self._owner_reply(load, th, text)
        o = self.owners.get(key)
        if o and not o.get("sandbox"):
            self._owner_chat(o, text)

    def _owner_story(self, key):
        """What this owner has going on with the desk, for the model to answer from (facts only, newest first)."""
        rows = []
        for load in reversed(self.loads[-30:]):
            th = load["threads"].get(key)
            if not th:
                continue
            lane, f = load.get("lane") or {}, load["load"]
            agreed = f"agreed ₹{th['agreedBuy'] / lane['distanceKm']:.1f}/km (₹{th['agreedBuy']:,} total)" \
                if th.get("agreedBuy") and lane.get("distanceKm") else None
            stage = {"needs_approval": "waiting for the broker's approval", "holding": "broker approved, owner asked to confirm",
                     "booked": "booked", "recovered": "re-booked", "rejected": "broker rejected", "escalated": "closed",
                     "sourcing": "collecting rates", "waiting_trucks": "waiting for trucks"}.get(load["status"], load["status"])
            won = "this owner was chosen" if load.get("winner") == key else ("another owner was chosen" if load.get("winner") else "")
            rows.append(f"- {load['id']}: {lane.get('origin')} → {lane.get('destination')}, {lane.get('distanceKm')} km, "
                        f"{f.get('weight_ton')} T, {th['type']}, pickup {f.get('pickup_when') or 'kal'}. Load stage: {stage}. "
                        f"Owner's thread: {th['status']}" + (f", owner asked ₹{th['ask']}/km" if th.get("ask") else "")
                        + (f", desk countered ₹{th['counter']}/km" if th.get("counter") else "")
                        + (f", {agreed}" if agreed else "") + (f". {won}" if won else ""))
            if len(rows) >= 4:
                break
        trucks = [f"{t['type']} in {t['city']} ({t['available']})" + (f" at ₹{t['ratePerKm']}/km" if t.get("ratePerKm") else "")
                  for t in self.pool if t["ownerKey"] == key]
        return rows, trucks

    def _owner_status_line(self, key):
        """Without the model: where this owner's latest deal stands, in plain words, instead of a canned truck question."""
        for load in reversed(self.loads[-30:]):
            th, lane = load["threads"].get(key), load.get("lane") or {}
            if not th:
                continue
            rate = f" ₹{th['agreedBuy'] / lane['distanceKm']:.1f}/km" if th.get("agreedBuy") and lane.get("distanceKm") else ""
            stage = ("booking pakki hai" + rate if load.get("winner") == key and load["status"] in ("booked", "recovered")
                     else f"rate{rate} tay hua, broker ke approval ka intezaar hai" if th["status"] == "deal" and load["status"] == "needs_approval"
                     else "ye load abhi kisi aur ko gaya" if load.get("winner") and load["winner"] != key
                     else "abhi koi deal pakki nahi hui")
            return (f"{load['id']} ({lane.get('origin')} → {lane.get('destination')}): {stage}. "
                    "Payment, advance aur loading time broker batayenge 🙏")
        return ("Abhi aapke liye koi load nahi hai. Load aate hi yahin message karenge 🙏\n"
                "Gaadi free ho to aise bhejiye: *32 ft MXL, Surat, aaj, ₹85/km*")

    def _owner_chat(self, o, text):
        """An owner message outside a live negotiation: a truck report, a question or small talk. The model call runs
        off the worker so one slow reply never holds up other loads; the facts are read here, on the worker."""
        rows, trucks = self._owner_story(o["key"])
        threading.Thread(target=self._owner_chat_bg, args=(dict(o), text, rows, trucks), daemon=True).start()

    def _owner_chat_bg(self, o, text, rows, trucks):
        try:
            out = self._llm_json(
                "You are Bhaada, a freight desk talking to a TRUCK OWNER on chat. Decide what their message is and answer.\n"
                f"Owner: {o['name']}, base {o.get('baseCity') or 'unknown'}.\n"
                f"Their trucks reported today: {'; '.join(trucks) or 'none yet'}.\n"
                "Their loads with the desk (newest first):\n" + ("\n".join(rows) or "- none yet") + "\n"
                "Recent chat (oldest first):\n" + ("\n".join(list(getattr(self, "convo", {}).get(o["key"], []))[:-1][-8:])
                                                   or "(new chat)") + "\n"
                "Mention a load only when the message is about it; don't repeat what they were just told.\n"
                "If they ask whether their truck suits some goods or weight, advise from this truck guide:\n" + E.truck_catalogue()
                + "\nGoods rules:\n" + E.material_guide() + "\n"
                f'Owner\'s message: "{text}"\n'
                "Rules:\n"
                "- If the message reports a free/empty truck, its type, location or rate, set intent=\"checkin\" and leave reply empty.\n"
                "- If they ask whether there is a load/kaam for them, intent=\"ask_load\" with a short reply from the facts.\n"
                "- Thanks, greetings, ok/theek hai and small talk: intent=\"reply\" with a short, warm, natural answer "
                "(e.g. \"Shukriya ji 🙏 Koi load aaya to turant bataunga.\"). Never ask for truck details in reply to a thank-you.\n"
                "- Otherwise intent=\"reply\": answer their actual question in 1-3 short lines using ONLY the facts above. "
                "Quote agreed or countered prices exactly as given. Never invent a load, a price or a promise. If a deal is "
                "waiting for the broker's approval, say so and that they'll be told. If you don't know, say the desk will update them. "
                "If they have no load yet, say loads come here as soon as one fits, and ask them to send their truck as "
                "'32 ft MXL, Surat, aaj, ₹85/km' only if they haven't reported one.\n"
                f"- Payment terms are fixed: {self.terms_line()} State them when asked. Loading time, documents and anything "
                "else not in the facts: the broker will tell them. Don't guess.\n"
                "- If they seem to cancel a booked trip, ask them to write *cancel* to confirm.\n"
                "- Reply in Hinglish: Hindi words in Roman script, like \"Aapka rate ₹33/km tay hua hai, broker ka approval "
                "aate hi bata denge.\" If they write in Marathi, Gujarati, Punjabi, Bengali, Tamil, Telugu or Kannada, reply in that language in Roman script. Never write plain English sentences.\n"
                'Return ONLY JSON {"intent":"checkin|ask_load|reply","reply":""}')
        except Exception as e:
            print("owner chat failed, using rules:", repr(e))
            if E.norm_type(text) or (NO.search(text) and len(text.split()) <= 4 and "?" not in text):
                return self.post("checkin_apply", key=o["key"], text=text, trucks=self._extract_trucks(o, text))
            return self._wa(o, self._owner_status_line_safe(o["key"]))
        intent, reply = out.get("intent"), (out.get("reply") or "").strip()
        if intent in ("reply", "ask_load") and reply:
            self._wa(o, reply[:600])
            if intent == "ask_load":
                self.post("ai_broker", key=o["key"])
            return
        self.post("checkin_apply", key=o["key"], text=text, trucks=self._extract_trucks(o, text))

    def _owner_status_line_safe(self, key):
        try:
            return self._owner_status_line(key)
        except RuntimeError:                           # the worker changed a load mid-read: a plain answer is fine
            return "Abhi koi naya update nahi hai. Load aate hi yahin message karenge 🙏"

    def _open_thread(self, key):
        """The negotiation this owner is answering, newest first; a booked trip only if nothing is being negotiated."""
        for states in (("sent", "countered", "hold_requested", "reconfirm"), ("held",)):
            for load in reversed(self.loads):
                th = load["threads"].get(key)
                if th and th["status"] in states:
                    return th, load
        return None, None

    def remove_owner(self, key):
        """A registered owner who wants to send loads instead (e.g. one phone testing both roles)."""
        o = self.owners.pop(key, None)
        self.pool = [t for t in self.pool if t["ownerKey"] != key]
        self.post("save")
        return o

    def _on_forget(self, key):
        """'delete my data' (DPDP Act): drop the person's profile, trucks and chats; their loads keep only the numbers."""
        gone = "deleted-" + hmac.new(ALIAS_SALT, str(key).encode(), hashlib.sha256).hexdigest()[:8]
        self.owners.pop(key, None)
        self.pool = [t for t in self.pool if t["ownerKey"] != key]
        for d in (self.brokers, self.reliability, self.ai_due):
            d.pop(key, None)
        for l in self.loads:
            if l.get("brokerPhone") == key:
                l["brokerPhone"], l["raw"], l["brokerMessages"] = None, "", []
            th = l["threads"].pop(key, None)
            if th:
                th.update(ownerKey=gone, name="(deleted)", messages=[])
                l["threads"][gone] = th
                l["contacted"] = [gone if k == key else k for k in l.get("contacted", [])]
                if l.get("winner") == key:
                    l["winner"] = gone
        self.save()

    def send_checkin(self):
        live = [o for o in self.owners.values() if not o.get("sandbox")]
        for o in live:
            if self.send_wa:
                self.send_wa(o["phone"], f"Namaste {o['name']} 🙏 Bhaada desk se. Aaj gaadi khaali hai?\n"
                                         "Aise bhejiye: *32 ft MXL, Bhiwandi, aaj, ₹85/km* (ek line mein ek gaadi).\n"
                                         "Koi gaadi free nahi to *nahi* likh dijiye.")
        self.checkin = {"sentAt": now(), "asked": len(live)}
        return len(live)

    def _parse_checkin(self, o, text):
        self._on_checkin_apply(o["key"], text, self._extract_trucks(o, text))

    def _extract_trucks(self, o, text):
        """The trucks in an owner's message; safe off the worker (reads nothing that changes)."""
        if NO.search(text) and not re.search(r"\d", text):
            return None                                # "nahi": nothing free today
        trucks = []
        try:
            out = self._llm_json(tier="cheap", prompt=
                "An Indian truck owner answered 'is your truck free today?'. Extract every truck they offer.\n"
                f"Truck types (exact names): {', '.join(E.KNOWN_TYPES)}\n"
                'Return ONLY JSON {"trucks":[{"type":null,"city":null,"available":"today|tomorrow|in 2 days","rate_per_km":null}]}\n'
                f'Owner\'s base city: {o.get("baseCity") or "unknown"}. Message: "{text}"')
            trucks = out.get("trucks") or []
        except Exception as e:
            print("check-in parse failed:", repr(e))
        return trucks or self._checkin_rules(o, text)

    def _on_checkin_apply(self, key, text, trucks):
        o = self.owners.get(key)
        if not o:
            return
        if trucks is None:
            self.pool = [t for t in self.pool if t["ownerKey"] != o["key"]]
            return self._wa(o, "Theek hai, dhanyavaad. Kal phir poochenge 🙏")
        added = []
        for t in trucks:
            tt = E.norm_type(t.get("type"))
            city = geo.resolve_city(t.get("city") or o.get("baseCity"))
            if not tt or not city:
                continue
            rate = E.norm_weight(t.get("rate_per_km"))
            added.append({"ownerKey": o["key"], "type": tt, "capacityTon": E.rates["capacityTonByType"][tt],
                          "city": city["name"], "available": t.get("available") if t.get("available") in E.AVAIL_DAY else "today",
                          "ratePerKm": rate if rate and 10 < rate < 200 else None, "source": "checkin", "at": now()})
        if not added:
            return self._wa(o, "Gaadi ka type samajh nahi aaya 🙏 Inme se kaunsi hai: " + ", ".join(E.KNOWN_TYPES[:-1])
                               + "? Aur kahan khadi hai? e.g. *32 ft MXL, Surat, aaj, ₹85/km*")
        self.pool = [t for t in self.pool if t["ownerKey"] != o["key"]] + added
        self.save()
        day = {"today": "aaj", "tomorrow": "kal", "in 2 days": "parso"}
        lines = ", ".join(f"{t['type']}, {t['city']} ({day.get(t['available'], t['available'])})" for t in added)
        self._wa(o, f"Noted ✅ {lines}. Load aate hi message karenge.")
        self._on_retry_waiting()
        if self.settings.get("aiAgents"):               # nothing real for them yet: a demo load in a few seconds
            self.ai_due[o["key"]] = now() + 20

    def _checkin_rules(self, o, text):
        """Fallback when the model is unavailable: one truck per line, '32 ft MXL, Bhiwandi, aaj, ₹85/km'."""
        out = []
        for line in re.split(r"[\n;]+", text):
            tt = E.norm_type(line)
            if not tt:
                continue
            city = next((c for c in E.KNOWN_CITIES if c.lower() in line.lower()), None)
            if not city:
                parts = [x.strip() for x in line.split(",")]
                city = parts[1] if len(parts) > 1 else o.get("baseCity")
            day = "in 2 days" if re.search(r"parso|day after", line, re.I) else "tomorrow" if re.search(r"\bkal\b|tomorrow", line, re.I) else "today"
            rate = re.search(r"(\d{2,3}(?:\.\d)?)\s*(?:/|per)\s*km", line, re.I)
            out.append({"type": tt, "city": city, "available": day, "rate_per_km": float(rate.group(1)) if rate else None})
        return out

    def _wa(self, o, text):
        if self.send_wa and o.get("phone"):
            self.send_wa(o["phone"], text)

    # ---------------- a new load ----------------
    def start_load(self, fields, source="console", broker_phone=None, raw=""):
        with self._seq_lock:                           # two brokers at once must not share a load number
            self._seq += 1
            lid = f"BH-{self._seq}"
        load = {"id": lid, "createdAt": now(), "source": source, "brokerPhone": broker_phone,
                "raw": raw, "load": fields, "status": "routing", "events": [], "threads": {}, "brokerMessages": [],
                "quote": None, "winner": None, "rounds": 0, "mode": self.settings["mode"], "phase": "quote",
                "fixedSell": None, "contacted": [], "reason": "", "widenLeft": 1}
        self.post("new_load", load=load)               # the worker owns self.loads
        return load

    def _on_new_load(self, load):
        self.loads.append(load)
        self._on_route(load["id"])

    def _find(self, lid):
        return next((l for l in self.loads if l["id"] == lid), None)

    def _on_route(self, lid):
        load = self._find(lid)
        if not load or load["status"] != "routing":
            return
        f = load["load"]
        o, d = geo.resolve_city(f["origin"]), geo.resolve_city(f["destination"])
        self._event(load, "Intake", f"{f['origin']} → {f['destination']} · {f['weight_ton']:g} T · {f['truck_type']} · "
                                   f"{f.get('pickup_when') or 'kal'}" + (f" · budget ₹{f['budget_rs']:,.0f}" if f.get("budget_rs") else ""))
        if not o or not d:
            return self._escalate(load, f"Couldn't find {'origin' if not o else 'destination'} on the map.",
                                  f"{f['origin'] if not o else f['destination']} naksha (map) par nahi mila. Shehar ka naam check karke dobara bhejiye.")
        if o["name"] == d["name"]:
            return self._escalate(load, f"Origin and destination are both {o['name']}.",
                                  f"Pickup aur drop dono {o['name']} likha hai. Sahi shehar ke saath dobara bhejiye.")
        r = geo.road_km(o, d)
        known = E.find_lane(o["name"], d["name"])
        load["lane"] = {"laneId": f"{o['name']}-{d['name']}", "origin": o["name"], "destination": d["name"],
                        "distanceKm": max(1, r["km"]), "hours": r.get("hours"), "distanceSrc": r["src"],
                        "returnLoadProb": known["returnLoadProb"] if known else self.settings["defaultReturnLoadProb"],
                        "memory": self._lane_memory(f"{o['name']}-{d['name']}")}
        self._event(load, "Routing", f"{r['km']} km by road ({r['src']})" + (f", about {r['hours']} h driving" if r.get("hours") else ""))
        if f.get("budget_rs") and f["budget_rs"] < 300:     # "40 rupaye per km" lands here as 40: a rate, not a total
            per_km = f["budget_rs"]
            f["budget_rs"] = round(per_km * load["lane"]["distanceKm"])
            self._event(load, "Intake", f"Budget ₹{per_km:g}/km × {load['lane']['distanceKm']} km = ₹{f['budget_rs']:,}")
        f.setdefault("material_class", E.material_class(f.get("material"), load.get("raw")))
        fit = E.truck_fit(f)
        m = fit["material"]
        load["searchType"] = fit["type"]
        load["lane"]["premiumPct"] = m.get("premiumPct", 0)
        pickup = (datetime.now(IST) + timedelta(days=E.pickup_day(f.get("pickup_when")))).date()
        load["lane"]["season"], load["lane"]["seasonPct"] = E.season(pickup)
        if not load["searchType"]:
            return self._escalate(load, f"No truck fits: {fit['why']}.", f"{fit['why']}.")
        self._event(load, "Orchestrator", f"Goods: {m['label']} → {m['body']} body" + (", hazardous handling" if m.get("hazmat") else "")
                    + (f", +{m['premiumPct']}% rate" if m.get("premiumPct") else "")
                    + (f". Sourcing a {load['searchType']} ({fit['why']})." if load["searchType"] != f.get("truck_type") or f.get("truck_type_asked")
                       else f". Sourcing a {load['searchType']}."))
        self._broadcast(load)

    # ---------------- sourcing ----------------
    def _candidates(self, load, radius):
        f, lane = load["load"], load["lane"]
        origin = geo.resolve_city(lane["origin"])
        day = E.pickup_day(f.get("pickup_when"))
        w = f["weight_ton"]
        seen, out = set(load["contacted"]), []
        mode_sandbox = load["mode"] == "sandbox"
        ai_ok = mode_sandbox or (self.settings.get("aiAgents") and not load.get("aiBroker"))   # demo loads are for real owners
        rows = []
        for t in list(self.pool):
            o = self.owners.get(t["ownerKey"])
            if not o or t["ownerKey"] in seen or (o.get("sandbox") and not ai_ok) or (mode_sandbox and not o.get("sandbox")):
                continue
            if t["type"] != load["searchType"] or t["capacityTon"] < w or E.AVAIL_DAY.get(t["available"], 1) > day:
                continue
            if self._stale(t):
                continue
            c = geo.resolve_city(t["city"])
            dh = geo.road_km(c, origin, precise=False)["km"] if c else 9999
            if dh > radius:
                continue
            ai = bool(self.owners[t["ownerKey"]].get("sandbox")) and not mode_sandbox
            rows.append((ai, 0 if t["ownerKey"] in load.get("prefer", {}) else 1, dh, -self.reliability_of(t["ownerKey"]), t, dh))
        real, bots = [], []
        for ai, _, _, _, t, dh in sorted(rows, key=lambda r: r[:4]):
            if t["ownerKey"] in {x["ownerKey"] for x in real + bots}:
                continue
            (bots if ai else real).append({**t, "deadheadKm": dh})
        if not mode_sandbox:
            # broadcast on demand: registered owners who haven't answered today's check-in yet
            for k, o in list(self.owners.items()):    # a snapshot: owners register from the server thread
                if o.get("sandbox") or k in seen or k in {x["ownerKey"] for x in real}:
                    continue
                real.append({"ownerKey": k, "type": load["searchType"], "capacityTon": E.rates["capacityTonByType"][load["searchType"]],
                             "city": o.get("baseCity") or "", "available": "unknown", "ratePerKm": None,
                             "source": "on-demand", "deadheadKm": None})
        n = E.SRC["ownersPerBroadcast"]
        return real[:n] + bots[:max(0, n - len(real[:n]))] if mode_sandbox else real[:n] + bots[:max(2, n - len(real[:n]))]

    def _stale(self, t):
        """A check-in truck whose day has passed ("aaj" said yesterday) is no longer free."""
        if t.get("source") != "checkin" or not t.get("at"):
            return False
        said = datetime.fromtimestamp(t["at"], IST).date()
        return (datetime.now(IST).date() - said).days > E.AVAIL_DAY.get(t.get("available"), 0)

    def _broadcast(self, load):
        radius = self.settings["maxDeadheadKm"] * (1 if load["rounds"] == 0 else 2)
        cands = self._candidates(load, radius)
        if not cands:
            if load["threads"]:
                return self._decide(load, final=True)
            return self._wait_for_trucks(load, radius)
        load["rounds"] += 1
        load["status"] = "sourcing"
        load["deadline"] = now() + self.timeout()
        f, lane = load["load"], load["lane"]
        for c in cands:
            key = c["ownerKey"]
            o = self.owners.get(key)
            if not o:                                   # removed a moment ago (switched to broker)
                continue
            sell = load["fixedSell"] or E.target_sell(lane, c["type"])
            th = {"ownerKey": key, "name": o["name"], "sandbox": bool(o.get("sandbox")), "type": c["type"],
                  "city": c["city"], "deadheadKm": c["deadheadKm"], "checkinRate": c.get("ratePerKm"),
                  "source": c["source"], "reliability": self.reliability_of(key), "status": "sent", "messages": [],
                  "sell": sell, "rounds": 0, "ask": None, "counter": None, "agreedBuy": None, "round": load["rounds"]}
            load["threads"][key] = th
            load["contacted"].append(key)
            when = f.get("pickup_when") or "kal"
            hint = f" Aapne subah ₹{c['ratePerKm']}/km bataya tha." if c.get("ratePerKm") else ""
            demo = "🧪 Demo load (AI broker) · " if load.get("aiBroker") else ""
            m = E.MATERIALS.get(f.get("material_class") or "general", {})
            goods = f" {f['material']}" if f.get("material") and f["material"] != "general" else ""
            need = (" Band (closed) body chahiye." if m.get("body") == "closed" else "") + \
                   (" ⚠️ Khatarnaak maal: Hazchem permit + trained driver zaroori. Nahi ho to mana kar dijiye." if m.get("hazmat") else "")
            self._to_owner(load, th, f"🚚 {demo}{load['id']} Load: {lane['origin']} → {lane['destination']} ({lane['distanceKm']} km), {when}, "
                                     f"{f['weight_ton']:g} T{goods}, {c['type']}.{need}{hint} Advance "
                                     f"{(self.settings.get('terms') or DEFAULT_SETTINGS['terms'])['advancePct']}% loading pe. "
                                     "Gaadi free hai? Rate batayiye (₹/km ya total).")
        self._event(load, "Sourcing", f"Messaged {len(cands)} owners near {lane['origin']} at once"
                                      + (f" (round {load['rounds']}, within {radius} km)" if load["rounds"] > 1 else "")
                                      + f". Waiting up to {self.timeout() // 60 or 1} min for replies.")
        self.save()

    WAIT_HOURS = 6

    def _wait_for_trucks(self, load, radius):
        """Nobody has the truck yet: keep the load open and try again whenever an owner checks in."""
        if load["status"] == "waiting_trucks":
            return
        load["status"], load["watchUntil"] = "waiting_trucks", now() + self.WAIT_HOURS * 3600
        lane = load["lane"]
        self._event(load, "Sourcing", f"No {load['searchType']} within {radius} km of {lane['origin']} yet. Load kept open "
                                      f"for {self.WAIT_HOURS} h: any owner who checks in with one gets it at once.")
        self._to_broker(load, f"⏳ {load['id']}: abhi {lane['origin']} ke paas {load['searchType']} khaali nahi hai.\n"
                              f"Load khula rakha hai. Jaise hi koi owner gaadi batayega, turant usse rate poochke "
                              f"aapko quote bhejunga ({self.WAIT_HOURS} ghante tak).")
        self.save()

    def _on_retry_waiting(self):
        """New supply arrived (a check-in or a new owner): offer it every open load it fits."""
        for load in self.loads:
            if load["status"] != "waiting_trucks":
                continue
            radius = self.settings["maxDeadheadKm"]
            if self._candidates(load, radius):
                load["status"] = "sourcing"
                self._event(load, "Sourcing", "A matching truck just checked in. Sourcing again.")
                self._broadcast(load)

    AI_DESTS = ["Mumbai", "Pune", "Surat", "Ahmedabad", "Vadodara", "Bhiwandi", "Nashik", "Vapi", "Indore", "Nagpur",
                "Jaipur", "Delhi", "Hyderabad", "Bengaluru", "Chennai", "Lucknow", "Ludhiana", "Kolkata"]

    def _on_ai_broker(self, key):
        """The AI broker posts a demo load that fits a real owner's free truck, so an owner can try the desk alone."""
        o = self.owners.get(key)
        if not o or o.get("sandbox") or not self.settings.get("aiAgents") or self.settings["mode"] != "live":
            return
        busy = [l for l in self.loads if key in l["threads"] and l["status"] not in self.CLOSED]
        last = max([l["createdAt"] for l in self.loads if l.get("aiBroker") and l.get("aiFor") == key] or [0])
        trucks = [t for t in self.pool if t["ownerKey"] == key and not self._stale(t)]
        if busy or not trucks or now() - last < 600:
            return
        t = trucks[0]
        here = geo.resolve_city(t["city"])
        if not here:
            return
        rnd = __import__("random").Random(f"{key}{int(now() // 600)}")
        dests = []
        for name in self.AI_DESTS:
            d = geo.resolve_city(name)
            if d and d["name"] != here["name"] and 150 <= geo.road_km(here, d, precise=False)["km"] <= 900:
                dests.append(d["name"])
        if not dests:
            return
        cap = E.rates["capacityTonByType"][t["type"]]
        fields = E.normalise_load({"origin": here["name"], "destination": rnd.choice(dests), "truck_type": t["type"],
                                   "weight_ton": max(1, round(cap * rnd.choice([0.6, 0.75, 0.9]))),
                                   "pickup_when": {"today": "aaj", "tomorrow": "kal", "in 2 days": "parso"}.get(t["available"], "kal")})
        load = self.start_load(fields, "ai-broker", raw="AI broker demo load")
        load["aiBroker"], load["aiFor"] = True, key

    # ---------------- negotiation ----------------
    def _econ(self, load, th):
        km = load["lane"]["distanceKm"]
        sell = th["sell"]
        return {"km": km, "target": round(sell * (1 - E.MARGIN["targetPct"] / 100) / km, 1),
                "ceiling": round(sell * (1 - E.MARGIN["floorPct"] / 100) / km, 1)}

    def _owner_reply(self, load, th, text):
        if th["status"] in ("hold_requested", "held", "reconfirm"):
            return self._hold_reply(load, th, text)
        ec = self._econ(load, th)
        th["rounds"] += 1
        d = self._negotiate(load, th, text, ec)
        if d["action"] == "accept":
            th["status"], th["agreedBuy"] = "deal", round(d["rate"] * ec["km"])
            if d["rate"] <= ec["target"] and load["status"] == "sourcing":   # a good offer is in: give the rest 45 s, not the full window
                load["deadline"] = min(load.get("deadline", now() + 45), now() + 45)
            reply = d["reply"] or f"Done ✅ ₹{d['rate']}/km. Confirm karke batate hain."
            self._event(load, "Negotiation", f"{th['name']} agreed at ₹{d['rate']}/km.")
        elif d["action"] == "counter":
            th["status"], th["counter"] = "countered", d["rate"]
            reply = d["reply"] or f"₹{d['rate']}/km ho jayega? Regular load denge 👍"
            self._event(load, "Negotiation", f"Countered {th['name']}: ₹{th['ask'] or '?'}/km → ₹{d['rate']}/km.")
        elif d["action"] == "decline":
            th["status"] = "declined"
            reply = d["reply"] or "Koi baat nahi, agli baar 🙏"
            self._event(load, "Negotiation", f"{th['name']} is above the ceiling (₹{ec['ceiling']}/km). Walked away.")
        else:
            reply = d["reply"] or "Rate kitna lagega? ₹/km bata dijiye."
        self._to_owner(load, th, reply)
        if self._all_answered(load):
            self._decide(load)

    def _negotiate(self, load, th, text, ec):
        """The model reads what the owner said; the code decides the move (a concession ladder inside the ceiling)
        and explains it with the trip's real costs, so every counter comes with a reason the owner can check."""
        f, lane, km = load["load"], load["lane"], ec["km"]
        read = None
        if E.llm_ready():
            try:
                hist = "\n".join(("Bhaada: " if m["dir"] == "out" else "Owner: ") + m["text"] for m in th["messages"][-6:])
                out = self._llm_json(
                    "Read a truck owner's latest reply in a freight rate negotiation (Indian road freight, Hinglish chat).\n"
                    f"Load: {lane['origin']} → {lane['destination']}, {km} km, {f.get('weight_ton')} T, {th['type']}, "
                    f"pickup {f.get('pickup_when') or 'kal'}. Our last offer: {('₹' + str(th['counter']) + '/km') if th['counter'] else 'none yet'}.\n"
                    f"Terms you may state: the rate includes diesel and toll. {self.terms_line()} Anything else (loading time, "
                    "documents) the broker confirms.\n"
                    f"Conversation (latest last):\n{hist}\n"
                    "intent: quote (they name their rate), accept_offer (they agree to our last offer), refuse (not "
                    "interested / truck not free), question (they ask something), other.\n"
                    "If they ask something, put a 1-line answer from the terms above in answer, in Hinglish (Hindi words in "
                    "Roman script, e.g. \"Rate mein diesel aur toll dono shaamil hain.\"), never English. Else empty.\n"
                    'Return ONLY JSON {"intent":"quote|accept_offer|refuse|question|other","owner_rate_per_km":null,'
                    '"owner_total_rs":null,"answer":""}')
                rate = E.norm_weight(out.get("owner_rate_per_km"))
                total = E.norm_weight(out.get("owner_total_rs"))
                read = {"intent": out.get("intent"), "ask": rate or (total / km if total and total > 300 else None),
                        "answer": (out.get("answer") or "").strip()}
            except Exception as e:
                print("negotiation LLM failed, using rules:", repr(e))
        if not read or read["intent"] not in ("quote", "accept_offer", "refuse", "question", "other"):
            ask = self._owner_rate(text, km)
            read = {"intent": "quote" if ask else "accept_offer" if YES.search(text) and th["counter"]
                    else "refuse" if NO.search(text) else "other", "ask": ask, "answer": ""}
        if read["intent"] == "quote" and not read["ask"]:
            read["intent"] = "other"
        if read["ask"]:
            prev = th["ask"]
            th["ask"] = round(read["ask"], 1)
            th["askDrop"] = round(prev - th["ask"], 1) if prev else 0
        d = self._next_move(load, th, ec, read)
        if read["answer"] and d["action"] in ("counter", "ask"):
            d["reply"] = read["answer"] + "\n" + d["reply"]
        d["ask"] = None                                 # th["ask"] is already set above
        return d

    def _lane_memory(self, lane_id):
        """Median agreed ₹/km per truck type on this lane over the last 30 days (at least 2 deals)."""
        out = {}
        for k, rows in list(self.lane_rates.items()):
            lid, _, tt = k.partition("|")
            recent = sorted(r for r, t in rows if lid == lane_id and now() - t < 30 * 86400)
            if len(recent) >= 2:
                out[tt] = recent[len(recent) // 2]
        return out

    def _remember_rate(self, load, th):
        """A booked trip teaches the desk what owners really take on this lane."""
        lane = load["lane"]
        rows = self.lane_rates.setdefault(f"{lane['laneId']}|{th['type']}", [])
        rows.append((round(th["agreedBuy"] / lane["distanceKm"], 1), now()))
        del rows[:-10]

    def _rationale(self, load, th, km):
        """The owner-facing reason behind a number: the lane's market rate and what this trip really costs them."""
        lane = load["lane"]
        c = E.lane_cost(lane, th["type"])
        why = (f"Hisaab: is route pe {th['type']} ka market bhaada ~₹{round(c['market'] / km)}/km (shipper rate) chalta hai. "
               f"Aapka kharcha: diesel ~₹{c['fuel']:,} (₹{E.rates['dieselPerLitre']:g}/L), toll ~₹{c['toll']:,}, driver+helper "
               f"~₹{c['driver']:,}, EMI/maintenance ~₹{c['fixed']:,} → lagbhag ₹{round(c['owner_cost'] / km)}/km.")
        if E.lane_memory(lane, th["type"]):
            why += f" Is lane pe haal mein owners ne ~₹{E.lane_memory(lane, th['type'])}/km liya hai."
        if lane.get("returnLoadProb", 0) >= 0.6:
            why += f" {lane['destination']} se wapsi ka load aasaani se milta hai."
        if th.get("deadheadKm"):
            why += f" Gaadi pickup se sirf {th['deadheadKm']} km door hai."
        return why

    def _best_rival(self, load, th):
        """The cheapest owner already agreed on this load (a real offer, never a made-up one)."""
        km = load["lane"]["distanceKm"]
        rates = [x["agreedBuy"] / km for k, x in load["threads"].items() if x is not th and x["status"] == "deal" and x.get("agreedBuy")]
        return round(min(rates), 1) if rates else None

    def _next_move(self, load, th, ec, read):
        km, ask, rnd, last = ec["km"], th["ask"], th["rounds"], self.settings["maxRounds"]
        target, ceiling = ec["target"], ec["ceiling"]
        rival = self._best_rival(load, th)
        done = lambda r: {"action": "accept", "rate": round(r, 1),
                          "reply": f"Done ✅ ₹{round(r, 1)}/km (₹{round(r * km):,} total). Broker se confirm karke turant batate hain."}
        if read["intent"] == "refuse":
            return {"action": "decline", "rate": None, "reply": "Koi baat nahi 🙏 Agla load aate hi aapko batayenge."}
        if read["intent"] == "accept_offer" and th["counter"]:
            return done(th["counter"])
        if not ask:
            q = "Is load ka aapka rate kya hoga? ₹/km ya total bata dijiye."
            return {"action": "ask", "rate": None, "reply": q if read["intent"] != "question" else "Rate bhi bata dijiye (₹/km ya total)."}
        if ask <= target or (rival is None and th["counter"] and ask <= min(ceiling, th["counter"] + 0.5)):
            return done(ask)
        cap = min(ceiling, rival) if rival else ceiling   # paying more than an owner who already said yes makes no sense
        if ask <= cap and ask - (th["counter"] or target) <= max(0.6, 0.02 * ask):
            return done(ask)                            # a few paise apart: close it rather than haggle
        if rnd >= last:
            if ask <= cap:
                return done(ask)
            if (th["counter"] or 0) < cap - 0.05 and not th.get("finalOffer"):
                th["finalOffer"] = True                 # one honest last step to the most we can pay, then stop
                return {"action": "counter", "rate": round(cap, 1),
                        "reply": f"Aakhri offer ₹{round(cap, 1)}/km (₹{round(cap * km):,} total), isse upar sach mein nahi ho payega. "
                                 "Chalega to *haan* likhiye."}
            why = f"ek aur owner ₹{rival}/km pe raazi hai" if rival and rival < ceiling else "isse upar humara margin nahi bachta"
            return {"action": "decline", "rate": None,
                    "reply": f"₹{ask}/km nahi ho payega 🙏 ({why}). Agla load aapko pehle denge."}
        # concession ladder: open at the anchor, then close the gap step by step; never move more than they did
        morning = th.get("checkinRate") if th.get("checkinRate") and th["checkinRate"] <= cap else None
        anchor = morning or target                      # their own morning rate, if we can pay it, is the fairest opener
        step = {1: 0.0, 2: 0.5}.get(rnd, 1.0)
        offer = anchor + (min(ask, cap) - anchor) * step
        if th["counter"]:
            give = offer - th["counter"]
            moved = th.get("askDrop") or 0
            offer = th["counter"] + min(give, max(moved, 0.5)) if give > 0 else th["counter"]
        offer = round(min(offer, cap), 1)
        if offer >= ask:
            return done(ask)
        lump = f"₹{round(offer * km / 500) * 500:,} total (₹{offer}/km)"     # owners think in lump sums: "27 hazaar"
        lines = [f"Aapke liye {lump} tak badha diya, isse zyada mushkil hai." if rnd >= last - 1 else
                 f"{lump} de sakte hain." if rnd == 1 else f"Aapke liye {lump} tak badha diya."]
        if rnd == 1:
            lines.append(self._rationale(load, th, km))
        if morning and morning < ask:
            lines.append(f"Aapne subah khud ₹{morning}/km bataya tha.")
        if rival:
            lines.append(f"Ek aur owner ₹{rival}/km pe raazi hai, aap ₹{offer}/km pe karo to aapko denge.")
        lines.append("Theek lage to *haan* likhiye.")
        return {"action": "counter", "rate": offer, "reply": " ".join(lines)}

    @staticmethod
    def _owner_rate(text, km):
        """The owner's ask in ₹/km: '38/km', '38k', '35 hazaar total'; ignores tons, feet, times and toll add-ons."""
        t = text.lower().replace(",", "")
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:rs\.?|₹|rupaye|rupees|rupee)?\s*(?:/|per|prati)\s*(?:km|kilo)", t)
        if m:
            return float(m.group(1))
        nums = []
        for v, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(lakh|lac|k\b|hazaa?r|thousand|tonne|ton|t\b|ft|feet|foot|baje|tyre|km|din|ghante|gaadi|truck)?", t):
            if unit in ("", "lakh", "lac", "k", "hazar", "hazaar", "thousand"):
                nums.append(float(v) * {"": 1, "lakh": 1e5, "lac": 1e5}.get(unit, 1e3))
        if not nums:
            return None
        n = max(nums)
        return n / km if n > 300 else n

    def _llm_json(self, prompt, tier="smart"):
        # timeout 12: negotiation calls this on the worker, so one slow reply must not stall every other load
        msg = E.chat([{"role": "user", "content": prompt}], json_mode=True, timeout=12, retries=2, max_wait=8, tier=tier)
        return E.parse_json(msg.get("content") or "")

    def _all_answered(self, load):
        live = [t for t in load["threads"].values() if t["round"] == load["rounds"]]
        return all(t["status"] in ("deal", "declined", "silent") for t in live)

    # ---------------- deciding ----------------
    def _on_tick(self):
        t = now()
        self._tick_due = False
        for key, due in list(self.ai_due.items()):      # persisted, so a restart doesn't drop the promised demo load
            if t >= due:
                self.ai_due.pop(key, None)
                self._on_ai_broker(key)
        for load in self.loads:
            if load["status"] == "routing" and t - load["createdAt"] > 180:     # the route event failed or never ran
                self._escalate(load, "Routing did not finish (map service or an internal error).",
                               "Route nahi nikal paaya (map service ka jawab nahi aaya). Thodi der baad dobara bhejiye.")
            elif load["status"] in ("sourcing",) and t > load.get("deadline", t + 1):
                for th in load["threads"].values():
                    if th["status"] in ("sent", "countered", "reconfirm") and th["round"] == load["rounds"]:
                        th["status"] = "declined" if th["status"] == "countered" else "silent"
                self._decide(load)
            elif load["status"] == "waiting_trucks" and t > load.get("watchUntil", t + 1):
                load["status"] = "sourcing"                 # so _escalate's broker message reads as the end of the wait
                self._escalate(load, f"{self.WAIT_HOURS} ghante mein koi {load['searchType']} nahi mila.")
            elif load["status"] == "holding" and not load.get("holdNudged") and load.get("deadline", t) - t < \
                    (load["deadline"] - load.get("approvedAt", t)) / 2 and load.get("approvedAt"):
                load["holdNudged"] = True                   # halfway through the hold window: one reminder
                th = load["threads"][load["winner"]]
                mins = max(1, int((load["deadline"] - t) // 60))
                self._to_owner(load, th, f"⏰ {load['id']}: gaadi hold karni hai? {mins} min mein *HAAN* likhiye, warna load kisi aur ko jayega.")
            elif load["status"] == "needs_approval" and t >= load.get("autoApproveAt", t + 1):
                self._on_approve(load["id"])                # the AI broker's own demo load
            elif load["status"] == "needs_call" and t - load.get("quoteAt", t) > \
                    self.settings.get("approvalExpireSec", {}).get(load["mode"], 3600):
                self._escalate(load, "Broker didn't answer the strategist in time.", "Aapka jawab nahi aaya, load aapke paas hai.")
            elif load["status"] == "needs_approval" and load.get("quoteAt"):
                ttl = self.settings.get("approvalExpireSec", {}).get(load["mode"], 3600)
                if t - load["quoteAt"] > ttl:
                    self._expire_quote(load)
                elif t - load["quoteAt"] > ttl / 4 and not load.get("quoteNudged"):
                    load["quoteNudged"] = True
                    self._to_broker(load, f"⏰ {load['id']} ka quote approval ka intezaar kar raha hai. Owner ki gaadi "
                                          f"{int((ttl - (t - load['quoteAt'])) // 60)} min aur rukegi.\n" + self.quote_text(load),
                                    [("approve:" + load["id"], "Approve ✅"), ("reject:" + load["id"], "Reject ❌")])
            elif load["status"] == "holding" and t > load.get("deadline", t + 1):
                th = load["threads"][load["winner"]]
                th["status"] = "backed_out"
                self._event(load, "Booking", f"{th['name']} didn't confirm the hold in time.")
                self._replan(load, load["winner"])
        if (self._dirty and t - self._saved_at >= self.SAVE_GAP) or t - self._saved_at >= 30:
            self.flush()                                # coalesced saves, and changes made without a save() call

    def _decide(self, load, final=False):
        if load["status"] not in ("sourcing",):
            return
        lane, f = load["lane"], load["load"]
        offers = []
        for key, th in load["threads"].items():
            if th["status"] == "deal" and th["agreedBuy"]:
                q = E.price_quote(lane, th["type"], th["agreedBuy"], load["fixedSell"], f.get("budget_rs"))
                th["quote"] = q
                if q["above_floor"]:
                    offers.append((q["margin_pct"], th["reliability"], key))
        if not offers:
            return self._strategize(load, final)
        offers.sort(reverse=True)
        best = offers[0]
        for m, rel, key in offers[1:]:              # a clearly more reliable owner wins a near tie
            if best[0] - m < 1 and rel - best[1] >= 0.3:
                best = (m, rel, key)
        key = best[2]
        th = load["threads"][key]
        load["winner"], load["quote"] = key, th["quote"]
        why = (f"Best margin above the floor from {len(offers)} offer(s)" if best == offers[0]
               else f"{th['name']} is rated {th['reliability']}/5, worth a margin gap under 1 point")
        load["reason"] = why
        for k, other in load["threads"].items():
            if k != key and other["status"] == "deal":
                self._to_owner(load, other, "Dhanyavaad 🙏 Abhi doosri gaadi final hui, aapko backup mein rakha hai.")
        q = th["quote"]
        if load["phase"] == "replan":
            th["status"] = "held"
            load["status"], load["bookedAt"] = "recovered", now()
            self._remember_rate(load, th)
            self._to_owner(load, th, f"Booked ✅ {lane['origin']} → {lane['destination']} at ₹{round(th['agreedBuy'] / lane['distanceKm'], 1)}/km.")
            self._event(load, "Booking", f"Re-booked with {th['name']}: margin {q['margin_pct']}% at the shipper's fixed ₹{q['sell_total']:,}.")
            self._to_broker(load, f"✅ {load['id']} dobara book ho gaya: {th['name']} · margin {q['margin_pct']}% "
                                  f"(floor {E.MARGIN['floorPct']:g}%). Shipper ka rate wahi ₹{q['sell_total']:,}. Gaadi number aate hi bhejta hoon.")
            self._ask_dispatch(load, th)
        else:
            load["status"], load["quoteAt"] = "needs_approval", now()
            if load.get("aiBroker"):                     # the AI broker approves its own demo load after a short think
                load["autoApproveAt"] = now() + 8        # on the clock, so it survives a restart
            self._event(load, "Pricing", f"Buy ₹{q['buy_total']:,} → sell ₹{q['sell_total']:,}, margin {q['margin_pct']}% "
                                         f"(floor {E.MARGIN['floorPct']:g}%)." + (f" {q['note']}." if q.get("note") else ""))
            self._to_broker(load, self.quote_text(load), [("approve:" + load["id"], "Approve ✅"), ("reject:" + load["id"], "Reject ❌")])
        self.save()

    # ---------------- the strategist: an AI decision when the plan is stuck, checked by code ----------------
    def _options(self, load, final):
        """Every recovery move that is possible right now, with the numbers the code has worked out for it."""
        lane, f, km = load["lane"], load["load"], load["lane"]["distanceKm"]
        ths = list(load["threads"].values())
        opts = {}
        if load.get("widenLeft", 0) > 0 and not final:
            opts["widen"] = f"message owners up to {self.settings['maxDeadheadKm'] * 2} km from {lane['origin']} (not asked yet)"
        bigger = [t for t in E.type_check({"truck_type": load["searchType"], "weight_ton": f["weight_ton"]})["suggest"]
                  if t not in load.get("triedTypes", []) and t != load["searchType"]
                  and E.rates["capacityTonByType"][t] > E.rates["capacityTonByType"][load["searchType"]]] \
            if E.rates["capacityTonByType"].get(load["searchType"]) else []
        if not bigger:
            caps = E.rates["capacityTonByType"]
            bigger = sorted((t for t in caps if caps[t] > caps[load["searchType"]] and t not in load.get("triedTypes", [])
                             and t != "20 ft container"), key=lambda t: caps[t])
        if bigger:
            opts["_bigger"] = bigger[0]
            opts["bigger_truck"] = f"look for a {bigger[0]} instead of {load['searchType']} (more capacity, usually a higher rate)"
        sell = load["fixedSell"] or E.target_sell(lane, load["searchType"])
        ceiling = round(sell * (1 - E.MARGIN["floorPct"] / 100) / km, 1)
        near = [t for t in ths if t.get("ask") and t["status"] in ("declined", "countered") and not t.get("finalOffer")
                and t["ask"] <= ceiling * 1.12]
        if near and not load.get("finalSweep"):
            opts["final_offer"] = f"offer ₹{ceiling}/km (the most the floor allows) to {len(near)} owner(s) who asked up to 12% more"
        asks = sorted((t["ask"], t["ownerKey"]) for t in ths if t.get("ask") and t["status"] in ("declined", "countered", "deal"))
        if asks and not load.get("brokerCallAsked"):
            ask, key = asks[0]
            if load["phase"] == "replan":
                m = round((load["fixedSell"] - ask * km) / load["fixedSell"] * 100, 1)
                opts["ask_broker"] = (f"ask the broker to book {load['threads'][key]['name']} at their ₹{ask}/km anyway: "
                                      f"margin {m}% (below the {E.MARGIN['floorPct']:g}% floor) at the shipper's fixed ₹{load['fixedSell']:,}")
            else:
                need = round(ask * km / (1 - E.MARGIN["floorPct"] / 100))
                opts["ask_broker"] = (f"ask the broker to quote the shipper ₹{need:,} (₹{round(need / km, 1)}/km) so "
                                      f"{load['threads'][key]['name']}'s ₹{ask}/km clears the floor")
            opts["_broker_pick"] = (key, ask)
        opts["escalate"] = "hand the load to the broker with what was tried"
        return opts

    def _strategize(self, load, final):
        """Nothing clears the floor. The model picks the recovery from the moves the code says are possible;
        the code checks the choice and carries it out, and falls back to a fixed order if the model is unavailable."""
        load["strategySteps"] = load.get("strategySteps", 0) + 1
        opts = self._options(load, final)
        moves = [k for k in opts if not k.startswith("_")]
        if load["strategySteps"] > 4:
            moves = ["escalate"]
        ths = list(load["threads"].values())
        tally = ", ".join(f"{sum(t['status'] == s for t in ths)} {s}" for s in ("deal", "countered", "declined", "silent") if
                          any(t["status"] == s for t in ths)) or "nobody asked yet"
        asks = sorted(t["ask"] for t in ths if t.get("ask"))
        choice, why, by = None, "", "rules"
        if len(moves) > 1 and E.llm_ready():
            try:
                f, lane = load["load"], load["lane"]
                out = self._llm_json(
                    "You are the recovery strategist of an Indian freight desk. A load is stuck: no owner's rate leaves the "
                    "broker's minimum margin. Pick the single best next move.\n"
                    f"Load {load['id']}: {lane['origin']} → {lane['destination']}, {lane['distanceKm']} km, {f['weight_ton']} T, "
                    f"truck {load['searchType']}, pickup {f.get('pickup_when') or 'kal'}, phase "
                    f"{'RE-PLAN: an owner backed out and the shipper already has the price' if load['phase'] == 'replan' else 'first quote'}.\n"
                    f"Owners: {tally}. Their asks (₹/km): {asks or 'none'}. Moves tried so far: {load.get('tried', []) or 'none'}.\n"
                    "Possible moves:\n" + "\n".join(f"- {k}: {opts[k]}" for k in moves) + "\n"
                    "Guidance: owners who stayed silent are a supply problem: widen or try a bigger truck before anything else. "
                    "An owner whose ask is within ~12% of the limit: final_offer. Asks far above the limit with nothing left to "
                    "try: ask_broker (the broker decides whether to pay more; in a re-plan the shipper already has the price, so "
                    "asking the broker is faster than escalating). escalate only when no other move is listed or the others "
                    "were already tried. Only list facts that are true; nothing has been tried unless it says so above.\n"
                    'Return ONLY JSON {"move":"<one of the moves>","why":"<one short sentence>"}')
                if out.get("move") in moves:
                    choice, why, by = out["move"], (out.get("why") or "").strip()[:200], "AI"
            except Exception as e:
                print("strategist model failed, using the fixed order:", repr(e))
        if choice == "escalate" and len(moves) > 1 and load["strategySteps"] == 1:
            choice, why, by = None, "", "rules"            # handing over at the first dead end wastes the moves still open
        if not choice:
            choice = next(m for m in ("widen", "final_offer", "bigger_truck", "ask_broker", "escalate") if m in moves)
            why = "fixed order: widen, final offer, bigger truck, ask the broker, hand over"
        load.setdefault("tried", []).append(choice)
        self._event(load, "Strategist", f"({by}) {choice.replace('_', ' ')}: {opts[choice]}." + (f" Why: {why}" if why else ""))
        getattr(self, "_move_" + choice)(load, opts)

    def _move_widen(self, load, opts):
        load["widenLeft"] -= 1
        self._broadcast(load)

    def _move_bigger_truck(self, load, opts):
        load.setdefault("triedTypes", []).append(load["searchType"])
        load["searchType"], load["widenLeft"] = opts["_bigger"], max(load.get("widenLeft", 0), 1)
        self._broadcast(load)

    def _move_final_offer(self, load, opts):
        load["finalSweep"] = True
        km = load["lane"]["distanceKm"]
        sell = load["fixedSell"] or E.target_sell(load["lane"], load["searchType"])
        ceiling = round(sell * (1 - E.MARGIN["floorPct"] / 100) / km, 1)
        load["rounds"] += 1
        load["status"], load["deadline"] = "sourcing", now() + self.timeout()
        for t in load["threads"].values():
            if t.get("ask") and t["status"] in ("declined", "countered") and t["ask"] <= ceiling * 1.12:
                t.update(status="countered", counter=ceiling, round=load["rounds"], finalOffer=True, sell=sell)
                self._to_owner(load, t, f"{load['id']}: ek aakhri koshish 🙏 ₹{ceiling}/km (₹{round(ceiling * km):,} total) pe "
                                        "gaadi de sakte ho? Isse upar sach mein nahi ho payega. *haan* likhiye.")

    def _move_ask_broker(self, load, opts):
        key, ask = opts["_broker_pick"]
        load["brokerCallAsked"], load["brokerCall"] = True, {"owner": key, "ask": ask}
        load["status"], load["quoteAt"] = "needs_call", now()
        km, th = load["lane"]["distanceKm"], load["threads"][key]
        if load["phase"] == "replan":
            text = (f"🤔 {load['id']}: backup ke liye sabse sasta {th['name']} ₹{ask}/km maang raha hai. Shipper ka ₹{load['fixedSell']:,} "
                    f"wahi rakhein to margin floor se kam bachega ({opts['ask_broker'].split('margin ')[1].split(' (')[0]}). Book karein?")
            buttons = [("call_yes:" + load["id"], "Haan, book karo"), ("call_no:" + load["id"], "Nahi")]
        else:
            need = round(ask * km / (1 - E.MARGIN["floorPct"] / 100))
            text = (f"🤔 {load['id']}: owners ka sabse kam rate {th['name']} ka ₹{ask}/km hai. Shipper ko ₹{need:,} "
                    f"(₹{round(need / km, 1)}/km) bol sakte ho to {E.MARGIN['floorPct']:g}% margin bachega. Is rate pe quote banaun?")
            buttons = [("call_yes:" + load["id"], f"Haan ₹{need:,}"), ("call_no:" + load["id"], "Nahi")]
        self._to_broker(load, text, buttons)
        self.save()

    def _move_escalate(self, load, opts):
        ths = list(load["threads"].values())
        if any(t["status"] in ("deal", "declined", "countered") for t in ths):
            return self._escalate(load, "No owner agreed a rate that clears the margin floor.",
                                  "Kisi owner ka rate margin floor ke andar nahi baitha.")
        self._escalate(load, "No owner replied.", "Kisi owner ne jawab nahi diya.")

    def _on_own_rate(self, lid, rs):
        load = self._find(lid)
        if load and load.get("quote"):
            load["brokerRate"] = rs
            self._event(load, "Broker", f"Shadow mode: the broker's own quote was ₹{rs:,.0f} (Bhaada ₹{load['quote']['sell_total']:,}).")
            self.save()

    def _on_broker_call(self, lid, yes):
        """The broker's answer to the strategist's question: book the cheapest owner at their ask, or hand over."""
        load = self._find(lid)
        if not load or load["status"] != "needs_call":
            return
        call = load["brokerCall"]
        th, km = load["threads"][call["owner"]], load["lane"]["distanceKm"]
        self._event(load, "Broker", ("Agreed" if yes else "Declined") + " to the strategist's proposal.")
        if not yes:
            return self._escalate(load, "Broker declined the strategist's proposal.", "Aapne mana kiya, load aapke paas hai.")
        th["status"], th["agreedBuy"] = "deal", round(call["ask"] * km)
        self._to_owner(load, th, f"{load['id']}: aapka ₹{call['ask']}/km chalega 👍 Thodi der mein confirm karte hain.")
        if load["phase"] == "replan":
            q = E.price_quote(load["lane"], th["type"], th["agreedBuy"], load["fixedSell"])
            th["quote"], load["quote"], load["winner"] = q, q, call["owner"]
            th["status"] = "held"
            load["status"], load["bookedAt"] = "recovered", now()
            self._remember_rate(load, th)
            self._event(load, "Booking", f"Re-booked with {th['name']} at {q['margin_pct']}% margin, below the floor, on the broker's say-so.")
            self._to_owner(load, th, "Booked ✅")
            self._to_broker(load, f"✅ {load['id']} {th['name']} ke saath book. Margin {q['margin_pct']}%, shipper ka rate wahi.")
            self._ask_dispatch(load, th)
            return self.save()
        load["fixedSell"] = round(call["ask"] * km / (1 - E.MARGIN["floorPct"] / 100))
        load["status"] = "sourcing"
        self._decide(load, final=True)

    def _escalate(self, load, reason, hi=None):
        """reason: English, for the console; hi: the same in Hinglish for the broker's chat."""
        replan = load["phase"] == "replan"
        load["status"], load["reason"] = "escalated", reason
        load["closedAt"] = now()
        self._event(load, "Orchestrator", f"Escalated to you: {reason}")
        if replan:                                     # the shipper already has this price: this one is urgent
            lane = load.get("lane") or {}
            self._to_broker(load, f"🚨 {load['id']} {lane.get('origin')} → {lane.get('destination')}: backup gaadi nahi mili. "
                                  f"{hi or reason}\nShipper ko ₹{load['fixedSell']:,} bola hua hai. Turant khud kisi owner ko call kijiye.")
        else:
            self._to_broker(load, f"❌ {load['id']}: abhi quote nahi ban paaya. {hi or reason}\nAap khud dekh lijiye, ya thodi der baad phir bhejiye.")
        self.save()

    def _expire_quote(self, load):
        load["status"], load["closedAt"], load["reason"] = "escalated", now(), "Quote expired without approval."
        self._event(load, "Orchestrator", "Quote expired: nobody approved it in time, owners released.")
        for th in load["threads"].values():
            if th["status"] == "deal":
                th["status"] = "released"
                self._to_owner(load, th, f"{load['id']}: broker ne time pe confirm nahi kiya, ye load abhi cancel samjhiye. "
                                         "Gaadi free hai to agla load bhejte hain 🙏")
        self._to_broker(load, f"⌛ {load['id']} ka quote expire ho gaya (approve nahi hua), owners ko chhod diya. "
                              "Abhi bhi chahiye to load dobara bhejiye.")
        self.save()

    # ---------------- approval, hold, back-out ----------------
    def _on_approve(self, lid):
        load = self._find(lid)
        if not load or load["status"] != "needs_approval":
            return
        th = load["threads"][load["winner"]]
        hold = self.settings.get("holdTimeoutSec", {}).get(self.settings["mode"], self.timeout())
        load["status"], load["approvedAt"], load["deadline"] = "holding", now(), now() + hold
        load["fixedSell"] = load["quote"]["sell_total"]
        self._event(load, "Broker", "Approved the quote.")
        th["status"] = "hold_requested"                  # before the message, so the reply is read as the hold answer
        self._to_owner(load, th, f"Pakka karte hain? {load['lane']['origin']} → {load['lane']['destination']}, "
                                 f"₹{round(th['agreedBuy'] / load['lane']['distanceKm'], 1)}/km (₹{th['agreedBuy']:,} total). {self.terms_line()} "
                                 "Gaadi hold karne ke liye *HAAN* likhiye.")
        self.save()

    def _on_reject(self, lid):
        load = self._find(lid)
        if not load or load["status"] not in ("needs_approval",):
            return
        load["status"], load["closedAt"] = "rejected", now()
        self._event(load, "Broker", "Rejected the quote.")
        for th in load["threads"].values():
            if th["status"] == "deal":
                self._to_owner(load, th, "Load cancel ho gaya, dhanyavaad 🙏")
        self.save()

    def _hold_reply(self, load, th, text):
        key = th["ownerKey"]
        if th["status"] == "reconfirm":
            if YES.search(text) and not NO.search(text):
                th["status"] = "deal"
                self._event(load, "Sourcing", f"{th['name']} confirmed the earlier rate still holds.")
            else:
                th["status"] = "declined"
                self._event(load, "Sourcing", f"{th['name']} is no longer free.")
            if self._all_answered(load):
                self._decide(load)
            return
        if NO.search(text) and not YES.search(text):
            if re.search(r"\d", text):                     # "35 nahi, 38 chahiye" is haggling, not a back-out
                rate = round(th["agreedBuy"] / load["lane"]["distanceKm"], 1)
                return self._to_owner(load, th, f"Rate ₹{rate}/km tay hua tha. Usi rate pe gaadi hold karni hai? *HAAN* ya *NAHI* likhiye 🙏")
            th["status"] = "backed_out"
            self._event(load, "Booking", f"{th['name']} backed out: “{text[:60]}”.")
            return self._replan(load, key)
        if th["status"] == "hold_requested" and YES.search(text):
            th["status"] = "held"
            load["status"], load["bookedAt"] = "booked", now()
            self._remember_rate(load, th)
            secs = load["bookedAt"] - load["createdAt"]
            self._event(load, "Booking", f"{th['name']} confirmed the hold. Booking-ready in {int(secs // 60)}:{int(secs % 60):02d}.")
            self._to_owner(load, th, "Dhanyavaad ✅ Booking pakki.")
            self._to_broker(load, f"✅ {load['id']} booking pakki: {th['name']} ne gaadi hold kar di. "
                                  f"Shipper ko ₹{load['quote']['sell_total']:,} ka quote bhej dijiye. Gaadi number aate hi bhejta hoon.")
            self._ask_dispatch(load, th)
            self.save()
        elif th["status"] == "hold_requested":
            self._to_owner(load, th, "Gaadi hold karni hai? *HAAN* ya *NAHI* likhiye 🙏")

    def _on_backout(self, lid, note="Owner called to cancel"):
        """Recorded by the coordinator when an owner cancels by phone (or in sandbox, to rehearse it)."""
        load = self._find(lid)
        if not load or load["status"] not in ("booked", "holding", "recovered"):
            return
        key = load["winner"]
        load["threads"][key]["status"] = "backed_out"
        self._event(load, "Broker", f"{note}: {load['threads'][key]['name']}.")
        self._replan(load, key)

    def _replan(self, load, dropped):
        self.reliability[dropped] = round(self.reliability.get(dropped, 0) - E.SRC["reliabilityPenaltyOnBackout"], 2)
        name = load["threads"][dropped]["name"]
        load["phase"], load["status"], load["rounds"] = "replan", "sourcing", load["rounds"]
        load["fixedSell"] = load["quote"]["sell_total"]
        load["replanAt"], load["widenLeft"] = now(), 1
        load["prefer"] = {k: t["agreedBuy"] for k, t in load["threads"].items() if t["status"] == "deal" and k != dropped}
        self._event(load, "Orchestrator", f"{name} dropped out; reliability now {self.reliability_of(dropped)}/5. Goal unchanged: "
                                          f"re-book at the shipper's ₹{load['fixedSell']:,} above the {E.MARGIN['floorPct']:g}% floor.")
        self._to_broker(load, f"⚠️ {name} ne {load['id']} chhod diya. Main khud backup gaadi dhoondh raha hoon, shipper ka rate "
                              f"wahi ₹{load['fixedSell']:,} rahega. Margin floor khatre mein aaya tabhi aapko bolunga.")
        if load["prefer"]:
            load["rounds"] += 1
            load["deadline"] = now() + self.timeout()
            for k in load["prefer"]:
                th = load["threads"][k]
                th["status"], th["round"] = "reconfirm", load["rounds"]
                th["sell"] = load["fixedSell"]
                self._to_owner(load, th, f"Namaste 🙏 {load['lane']['origin']} → {load['lane']['destination']} wala load abhi bhi hai. "
                                         f"Aapka ₹{round(th['agreedBuy'] / load['lane']['distanceKm'], 1)}/km wala rate chalega? *HAAN* / *NAHI*")
            self._event(load, "Sourcing", f"Re-confirming {len(load['prefer'])} owner(s) who agreed earlier.")
        else:
            self._broadcast(load)
        self.save()

    # ---------------- sandbox owners (personas, clearly labelled) ----------------
    def _sim_reply(self, load, th):
        o = self.owners[th["ownerKey"]]
        b = o["behavior"]
        delay = max(1.5, min(20.0, b["responseDelaySec"] / 10, 0.5 * self.timeout()))   # always inside the reply window
        seed = int(hashlib.md5(f"{th['ownerKey']}|{load['id']}|{th['rounds']}|{th['status']}".encode()).hexdigest(), 16)
        km = load["lane"]["distanceKm"]
        cost = E.lane_cost(load["lane"], th["type"])["est_cost"]
        cost += E.deadhead_cost(th["deadheadKm"] or 0, th["type"]) * E.SRC["deadheadChargedShare"]
        ask, floor = cost * b["ratePremium"] / km, cost * b["minRateMult"] / km
        st, last = th["status"], th["messages"][-1]["text"]
        if st == "sent" and E.MATERIALS.get(load["load"].get("material_class") or "general", {}).get("hazmat") and seed % 2:
            text = "Hazardous maal nahi le jaate bhai, permit nahi hai. Sorry 🙏"     # many real owners refuse it
            return threading.Timer(delay, lambda: self.post("owner_message", key=th["ownerKey"], text=text, lid=load["id"])).start()
        if st == "sent":
            if b["responseDelaySec"] > 180 or seed % 10 == 0:
                return                                      # this one never answers: the timeout handles it
            text = f"₹{ask:.1f}/km lagega." + (f" Gaadi {th['city']} mein hai." if th["deadheadKm"] else "")
        elif st == "countered":
            if th["counter"] and th["counter"] >= floor - 0.05:
                text = f"Theek hai, ₹{th['counter']}/km chalega 👍"
            elif b["ratePremium"] > 1.08 or th["rounds"] >= 2:
                text = "Nahi bhai, isse kam nahi hoga."
            else:
                text = f"₹{max(floor, (ask + th['counter']) / 2 if th['counter'] else ask):.1f}/km last."
        elif st == "hold_requested":
            text = "Haan, gaadi hold kar di ✅"
        elif st == "reconfirm":
            text = "Haan, chalega." if (seed % 100) / 100 < b["honoursHoldProb"] else "Sorry, gaadi nikal gayi."
        else:
            return
        threading.Timer(delay, lambda: self.post("owner_message", key=th["ownerKey"], text=text, lid=load["id"])).start()

    # ---------------- broker-facing text and console view ----------------
    def quote_text(self, load):
        th, q, f, lane = load["threads"][load["winner"]], load["quote"], load["load"], load["lane"]
        ths = [t for t in load["threads"].values()]
        n = lambda s: sum(t["status"] == s for t in ths)
        m = E.MATERIALS.get(f.get("material_class") or "general", {})
        lines = [f"*{load['id']}* {lane['origin']} → {lane['destination']} · {lane['distanceKm']} km · {f['weight_ton']:g} T"
                 + (f" {f['material']}" if f.get("material") and f["material"] != "general" else "")]
        if load["searchType"] != f["truck_type"]:
            lines.append(f"🔄 {f['truck_type']} ki jagah {load['searchType']} li hai")
        lines += ["🧾 " + x for x in self.paperwork_lines(load)]
        if lane.get("seasonPct"):
            lines.append(f"📅 {lane['season']} season: rate +{lane['seasonPct']}% (drivers ghar jaate hain, gaadiyan kam)")
        if m.get("premiumPct"):
            lines.append(f"📦 {m['label']}: +{m['premiumPct']}% rate" + (". Hazchem permit, UN number e-way bill/LR par, trained driver." if m.get("hazmat") else ""))
        lines += [f"📲 {len(ths)} owners se poocha: {n('deal')} raazi, {n('declined')} mana, {n('silent')} ka jawab nahi",
                  f"🚛 {th['name']}{' (AI owner)' if th.get('sandbox') and load['mode'] == 'live' else ''} · {th['type']} · {th['reliability']}★",
                  f"💰 Quote *₹{q['sell_total']:,}* (₹{q['rate_per_km']}/km) · owner ko ₹{q['buy_total']:,}",
                  f"📊 Margin *{q['margin_pct']}%* (floor {E.MARGIN['floorPct']:g}%)"]
        note = q.get("note") or ""
        if note.startswith("shipper budget"):
            note = f"Shipper ka budget ₹{f.get('budget_rs') or 0:,.0f} floor se kam hai, isliye floor wala rate lagaya"
        elif note.startswith("capped"):
            note = f"Rate {E.MARGIN['ceilingPct']:g}% ceiling pe roka"
        elif note == "matched shipper budget":
            note = "Shipper ke budget jitna rate rakha"
        if note:
            lines.append(f"ℹ️ {note}")
        lines.append("Approve / Reject button dabaiye (ya *approve* likhiye).")
        return "\n".join(lines)

    STAGE_HI = {"routing": "route nikal raha hoon", "sourcing": "owners se rate le raha hoon",
                "holding": "approve ho gaya, owner se gaadi hold karwa raha hoon", "rejected": "aapne reject kiya, kuch book nahi hua"}

    def broker_loads(self, wa, open_only=False):
        return [l for l in list(self.loads) if l.get("brokerPhone") == wa
                and (not open_only or l["status"] not in ("escalated", "rejected"))]

    def broker_status(self, load):
        """One load, as the broker should hear it: (text, buttons). Read-only, so the server thread may call it."""
        lane, f, s = load.get("lane") or {}, load["load"], load["status"]
        head = f"{load['id']} {lane.get('origin') or f.get('origin')} → {lane.get('destination') or f.get('destination')}: "
        if s == "needs_approval":
            return self.quote_text(load), [("approve:" + load["id"], "Approve ✅"), ("reject:" + load["id"], "Reject ❌")]
        if s == "sourcing":
            cur = [t for t in load["threads"].values() if t["round"] == load["rounds"]]
            done = sum(t["status"] in ("deal", "declined", "silent") for t in cur)
            word = "backup gaadi dhoondh raha hoon" if load["phase"] == "replan" else "owners se rate le raha hoon"
            return head + f"{word} ({done}/{len(cur)} ne jawab diya).", None
        if s == "waiting_trucks":
            return head + f"abhi gaadi khaali nahi, {clock(load.get('watchUntil'))[:5]} tak intezaar karunga.", None
        if s in ("booked", "recovered"):
            th = load["threads"].get(load["winner"]) or {}
            return head + f"booking pakki: {th.get('name')}, shipper rate ₹{load['quote']['sell_total']:,}.", None
        if s == "escalated":
            return head + f"quote nahi bana. {load.get('reason') or ''}", None
        return head + self.STAGE_HI.get(s, s) + ".", None

    def view(self):
        """A copy for the console, taken while the worker keeps changing loads; phone numbers and chat ids never leave."""
        for attempt in range(5):
            try:
                v = json.loads(json.dumps(self._view(), ensure_ascii=False))
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            v = json.loads(json.dumps(self._view(), ensure_ascii=False))
        # keyed with a secret salt: a plain hash of a phone number, next to its last 4 digits, is reversed in a second
        alias = lambda k: k if not k or str(k).startswith("sb:") else \
            "o-" + hmac.new(ALIAS_SALT, str(k).encode(), hashlib.sha256).hexdigest()[:10]
        for o in v["owners"]:
            o["key"] = alias(o["key"])
            for t in o["trucks"]:
                t["ownerKey"] = alias(t["ownerKey"])
        for l in v["loads"]:
            l["threads"] = {alias(k): {**th, "ownerKey": alias(th["ownerKey"])} for k, th in l["threads"].items()}
            l["winner"] = alias(l.get("winner"))
            if l.get("brokerPhone"):
                l["brokerPhone"] = "•••• " + str(l["brokerPhone"])[-4:]
        return v

    def _view(self):
        today = datetime.now(IST).date()
        loads = [l for l in self.loads if datetime.fromtimestamp(l["createdAt"], IST).date() == today]
        ready = [l for l in loads if l.get("quoteAt")]
        mins = sorted((l["quoteAt"] - l["createdAt"]) / 60 for l in ready)
        booked = [l for l in loads if l["status"] in ("booked", "recovered")]
        margins = [l["quote"]["margin_pct"] for l in booked if l.get("quote")]
        kpis = {"received": len(loads), "quoted": len(ready), "booked": len(booked),
                "waiting": sum(l["status"] in ("needs_approval", "needs_call") for l in loads),
                "escalated": sum(l["status"] == "escalated" for l in loads),
                "medianQuoteMin": round(mins[len(mins) // 2], 1) if mins else None,
                "avgMargin": round(sum(margins) / len(margins), 1) if margins else None}
        shadow = [l for l in self.loads if l.get("brokerRate") and l.get("quote")]
        kpis["shadowCompared"] = len(shadow)            # all time: the pilot's accuracy evidence
        kpis["shadowWithin3Pct"] = round(100 * sum(abs(l["quote"]["sell_total"] - l["brokerRate"]) <= 0.03 * l["brokerRate"]
                                                   for l in shadow) / len(shadow)) if shadow else None
        live_owners = [o for o in list(self.owners.values()) if not o.get("sandbox")]
        pool = [t for t in self.pool if (t.get("source") == "sandbox") == (self.settings["mode"] == "sandbox")]
        return {"now": now(), "settings": self.settings, "kpis": kpis, "checkin": self.checkin,
                "owners": [{**{k: o.get(k) for k in ("key", "name", "baseCity", "sandbox")},
                            "phone": ("•••• " + o["phone"][-4:]) if o.get("phone") else None,
                            "reliability": self.reliability_of(o["key"]),
                            "trucks": [t for t in pool if t["ownerKey"] == o["key"]]}
                           for o in (live_owners if self.settings["mode"] == "live" else
                                     [o for o in list(self.owners.values()) if o.get("sandbox")])],
                "poolSize": len(pool), "liveOwners": len(live_owners),
                "aiOwners": len({t["ownerKey"] for t in self.pool if t.get("source") == "sandbox"})
                            if self.settings["mode"] == "live" and self.settings.get("aiAgents") else 0,
                "loads": [{k: v for k, v in l.items() if k not in ("contacted", "prefer")} for l in reversed(self.loads[-60:])],
                "whatsapp": bool(self.send_wa)}
