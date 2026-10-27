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
import json, queue, re, threading, time, hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import engine as E
import geo

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = Path(__file__).parent / "data" / "live_state.json"
geo.seed_cities(E.CITY["cities"])
E.CITY_RESOLVER = geo.resolve_city

YES = re.compile(r"\b(haan|haa|ha|han|yes|yep|ok|okay|pakka|confirm|confirmed|done|theek|thik|chalega|sure|book)\b|👍|✅", re.I)
NO = re.compile(r"\b(nahi|nahin|nhi|na|no|cancel|sorry|nikal|nikal gayi|not|mat)\b|❌", re.I)

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
        self.checkin = {"sentAt": None, "asked": 0}
        self._seq = 1000
        self._load()
        self._apply_settings()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._clock, daemon=True).start()

    # ---------------- persistence ----------------
    def _load(self):
        try:
            s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self.settings.update(s.get("settings", {}))
        self.owners = s.get("owners", {})
        self.pool = s.get("pool", [])
        self.loads = s.get("loads", [])
        self.reliability = s.get("reliability", {})
        self._seq = s.get("seq", self._seq)

    def save(self):
        live_owners = {k: v for k, v in self.owners.items() if not v.get("sandbox")}
        data = {"settings": self.settings, "owners": live_owners, "reliability": self.reliability, "seq": self._seq,
                "pool": [t for t in self.pool if t.get("source") == "checkin"], "loads": self.loads[-200:]}
        try:
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

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
            self.post("tick")

    def _worker(self):
        while True:
            kind, data = self.q.get()
            try:
                getattr(self, "_on_" + kind)(**data)
            except Exception as e:
                print("desk event failed:", kind, repr(e))

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
        self.save()
        return self.owners[phone]

    def sandbox_on(self):
        """Load the persona owners and their trucks as today's pool, marked sandbox."""
        self.owners = {k: v for k, v in self.owners.items() if not v.get("sandbox")}
        self.pool = [t for t in self.pool if t.get("source") != "sandbox"]
        for o in E.owners:
            if not o["behavior"]["respondsToCheckin"]:
                continue
            key = "sb:" + o["ownerId"]
            self.owners[key] = {"key": key, "phone": None, "name": o["name"], "baseCity": o["baseCity"],
                                "reliability": o["reliability"], "sandbox": True, "behavior": o["behavior"]}
            for tid in o["truckIds"]:
                t = E.TRUCKS_BY_ID.get(tid)
                if t:
                    self.pool.append({"ownerKey": key, "type": t["type"], "capacityTon": t["capacityTon"],
                                      "city": t["currentCity"], "available": t["availableFrom"], "ratePerKm": None,
                                      "source": "sandbox", "at": now()})

    def reliability_of(self, key):
        o = self.owners.get(key, {})
        return round(max(1.0, o.get("reliability", 4.5) + self.reliability.get(key, 0)), 1)

    # ---------------- inbound ----------------
    def _on_owner_message(self, key, text):
        th, load = self._open_thread(key)
        if th:
            th["messages"].append({"dir": "in", "text": text, "t": now()})
            return self._owner_reply(load, th, text)
        o = self.owners.get(key)
        if o and not o.get("sandbox"):
            self._parse_checkin(o, text)

    def _open_thread(self, key):
        for load in reversed(self.loads):
            th = load["threads"].get(key)
            if th and th["status"] in ("sent", "countered", "hold_requested", "reconfirm", "held"):
                return th, load
        return None, None

    def send_checkin(self):
        live = [o for o in self.owners.values() if not o.get("sandbox")]
        for o in live:
            if self.send_wa:
                self.send_wa(o["phone"], f"Namaste {o['name']} 🙏 Bhaada desk se. Aaj gaadi khaali hai?\n"
                                         "Aise bhejiye: *32 ft MXL, Bhiwandi, aaj, ₹38/km* (ek line mein ek gaadi).\n"
                                         "Koi gaadi free nahi to *nahi* likh dijiye.")
        self.checkin = {"sentAt": now(), "asked": len(live)}
        return len(live)

    def _parse_checkin(self, o, text):
        if NO.search(text) and not re.search(r"\d", text):
            self.pool = [t for t in self.pool if t["ownerKey"] != o["key"]]
            return self._wa(o, "Theek hai, dhanyavaad. Kal phir poochenge 🙏")
        trucks = []
        try:
            out = self._llm_json(
                "An Indian truck owner answered 'is your truck free today?'. Extract every truck they offer.\n"
                f"Truck types (exact names): {', '.join(E.KNOWN_TYPES)}\n"
                'Return ONLY JSON {"trucks":[{"type":null,"city":null,"available":"today|tomorrow|in 2 days","rate_per_km":null}]}\n'
                f'Owner\'s base city: {o.get("baseCity") or "unknown"}. Message: "{text}"')
            trucks = out.get("trucks") or []
        except Exception as e:
            print("check-in parse failed:", repr(e))
        if not trucks:
            trucks = self._checkin_rules(o, text)
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
            return self._wa(o, "Samajh nahi aaya 🙏 Aise bhejiye: *32 ft MXL, Bhiwandi, aaj, ₹38/km*")
        self.pool = [t for t in self.pool if t["ownerKey"] != o["key"]] + added
        self.save()
        lines = ", ".join(f"{t['type']} in {t['city']} ({t['available']})" for t in added)
        self._wa(o, f"Noted ✅ {lines}. Load aate hi message karenge.")

    def _checkin_rules(self, o, text):
        """Fallback when the model is unavailable: one truck per line, '32 ft MXL, Bhiwandi, aaj, ₹38/km'."""
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
        self._seq += 1
        load = {"id": f"BH-{self._seq}", "createdAt": now(), "source": source, "brokerPhone": broker_phone,
                "raw": raw, "load": fields, "status": "routing", "events": [], "threads": {}, "brokerMessages": [],
                "quote": None, "winner": None, "rounds": 0, "mode": self.settings["mode"], "phase": "quote",
                "fixedSell": None, "contacted": [], "reason": "", "widenLeft": 1}
        self.loads.append(load)
        self.post("route", lid=load["id"])
        return load

    def _find(self, lid):
        return next((l for l in self.loads if l["id"] == lid), None)

    def _on_route(self, lid):
        load = self._find(lid)
        f = load["load"]
        o, d = geo.resolve_city(f["origin"]), geo.resolve_city(f["destination"])
        self._event(load, "Intake", f"{f['origin']} → {f['destination']} · {f['weight_ton']:g} T · {f['truck_type']} · "
                                   f"{f.get('pickup_when') or 'kal'}" + (f" · budget ₹{f['budget_rs']:,.0f}" if f.get("budget_rs") else ""))
        if not o or not d:
            return self._escalate(load, f"Couldn't find {'origin' if not o else 'destination'} on the map.")
        r = geo.road_km(o, d)
        known = E.find_lane(o["name"], d["name"])
        load["lane"] = {"laneId": f"{o['name']}-{d['name']}", "origin": o["name"], "destination": d["name"],
                        "distanceKm": max(1, r["km"]), "hours": r.get("hours"), "distanceSrc": r["src"],
                        "returnLoadProb": known["returnLoadProb"] if known else self.settings["defaultReturnLoadProb"]}
        self._event(load, "Routing", f"{r['km']} km by road ({r['src']})" + (f", about {r['hours']} h driving" if r.get("hours") else ""))
        tc = E.type_check(f)
        load["searchType"] = f["truck_type"] if tc["fits"] else (tc["suggest"][0] if tc["suggest"] else None)
        if not load["searchType"]:
            return self._escalate(load, f"No truck type carries {f['weight_ton']:g} T. Split the load or quote a trailer.")
        if load["searchType"] != f["truck_type"]:
            self._event(load, "Orchestrator", f"{f['truck_type']} carries max {tc['capacity_ton']} T, so sourcing a {load['searchType']}.")
        self._broadcast(load)

    # ---------------- sourcing ----------------
    def _candidates(self, load, radius):
        f, lane = load["load"], load["lane"]
        origin = geo.resolve_city(lane["origin"])
        day = E.pickup_day(f.get("pickup_when"))
        w = f["weight_ton"]
        seen, out = set(load["contacted"]), []
        mode_sandbox = load["mode"] == "sandbox"
        rows = []
        for t in self.pool:
            o = self.owners.get(t["ownerKey"])
            if not o or bool(o.get("sandbox")) != mode_sandbox or t["ownerKey"] in seen:
                continue
            if t["type"] != load["searchType"] or t["capacityTon"] < w or E.AVAIL_DAY.get(t["available"], 1) > day:
                continue
            c = geo.resolve_city(t["city"])
            dh = geo.road_km(c, origin, precise=False)["km"] if c else 9999
            if dh > radius:
                continue
            rows.append((0 if t["ownerKey"] in load.get("prefer", {}) else 1, dh, -self.reliability_of(t["ownerKey"]), t, dh))
        for _, _, _, t, dh in sorted(rows, key=lambda r: r[:3]):
            if t["ownerKey"] in {x["ownerKey"] for x in out}:
                continue
            out.append({**t, "deadheadKm": dh})
        if not mode_sandbox and len(out) < E.SRC["ownersPerBroadcast"]:
            # broadcast on demand: registered owners who haven't answered today's check-in yet
            for k, o in self.owners.items():
                if o.get("sandbox") or k in seen or k in {x["ownerKey"] for x in out}:
                    continue
                out.append({"ownerKey": k, "type": load["searchType"], "capacityTon": E.rates["capacityTonByType"][load["searchType"]],
                            "city": o.get("baseCity") or "", "available": "unknown", "ratePerKm": None,
                            "source": "on-demand", "deadheadKm": None})
        return out[:E.SRC["ownersPerBroadcast"]]

    def _broadcast(self, load):
        radius = self.settings["maxDeadheadKm"] * (1 if load["rounds"] == 0 else 2)
        cands = self._candidates(load, radius)
        if not cands:
            return self._decide(load, final=True) if load["threads"] else self._escalate(
                load, f"No {load['searchType']} in today's pool within {radius} km of {load['lane']['origin']}.")
        load["rounds"] += 1
        load["status"] = "sourcing"
        load["deadline"] = now() + self.timeout()
        f, lane = load["load"], load["lane"]
        for c in cands:
            key = c["ownerKey"]
            o = self.owners[key]
            sell = load["fixedSell"] or E.target_sell(lane, c["type"])
            th = {"ownerKey": key, "name": o["name"], "sandbox": bool(o.get("sandbox")), "type": c["type"],
                  "city": c["city"], "deadheadKm": c["deadheadKm"], "checkinRate": c.get("ratePerKm"),
                  "source": c["source"], "reliability": self.reliability_of(key), "status": "sent", "messages": [],
                  "sell": sell, "rounds": 0, "ask": None, "counter": None, "agreedBuy": None, "round": load["rounds"]}
            load["threads"][key] = th
            load["contacted"].append(key)
            when = f.get("pickup_when") or "kal"
            hint = f" Aapne subah ₹{c['ratePerKm']}/km bataya tha." if c.get("ratePerKm") else ""
            self._to_owner(load, th, f"🚚 Load: {lane['origin']} → {lane['destination']} ({lane['distanceKm']} km), {when}, "
                                     f"{f['weight_ton']:g} T, {c['type']}.{hint} Gaadi free hai? Rate batayiye (₹/km ya total).")
        self._event(load, "Sourcing", f"Messaged {len(cands)} owners near {lane['origin']} at once"
                                      + (f" (round {load['rounds']}, within {radius} km)" if load["rounds"] > 1 else "")
                                      + f". Waiting up to {self.timeout() // 60 or 1} min for replies.")
        self.save()

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
        """LLM reads the owner's reply and answers; the code holds the ceiling and the round limit."""
        last_out = next((m["text"] for m in reversed(th["messages"][:-1]) if m["dir"] == "out"), "")
        d = None
        if E.llm_ready():
            try:
                hist = "\n".join(("Bhaada: " if m["dir"] == "out" else "Owner: ") + m["text"] for m in th["messages"][-6:])
                out = self._llm_json(
                    "You are Bhaada's negotiation agent hiring a truck from an Indian fleet owner on WhatsApp.\n"
                    f"Trip {ec['km']} km. Target buy rate ₹{ec['target']}/km. HARD CEILING ₹{ec['ceiling']}/km: never agree above it.\n"
                    f"Round {th['rounds']} of {self.settings['maxRounds']}. Your last counter: {th['counter'] or 'none'}.\n"
                    "Rules: if the owner's rate is at or below target, accept it. If above, counter (start near target, "
                    "move towards the ceiling only if they push). If they accept your counter, accept at your counter. "
                    "If they insist above the ceiling on the last round, decline politely. A total in rupees means total/km per km.\n"
                    f"Conversation:\n{hist}\n"
                    'Return ONLY JSON {"owner_rate_per_km":null,"action":"accept|counter|decline|ask","rate_per_km":null,'
                    '"reply":"<short Hinglish WhatsApp reply>"}')
                d = {"action": out.get("action"), "rate": E.norm_weight(out.get("rate_per_km")),
                     "ask": E.norm_weight(out.get("owner_rate_per_km")), "reply": (out.get("reply") or "").strip()}
            except Exception as e:
                print("negotiation LLM failed, using rules:", repr(e))
        if not d or d["action"] not in ("accept", "counter", "decline", "ask"):
            d = self._negotiate_rules(th, text, ec)
        if d.get("ask"):
            ask = d["ask"] if d["ask"] < 300 else d["ask"] / ec["km"]
            th["ask"] = round(ask, 1)
        # guardrails, enforced in code whatever the model said
        if d["action"] == "accept":
            rate = d["rate"] or th["counter"] or th["ask"]
            if not rate:
                d = {"action": "ask", "rate": None, "reply": ""}
            elif rate > ec["ceiling"]:
                d = ({"action": "counter", "rate": ec["ceiling"], "reply": ""} if th["rounds"] < self.settings["maxRounds"]
                     else {"action": "decline", "rate": None, "reply": ""})
            else:
                d["rate"] = round(rate, 1)
        if d["action"] == "counter":
            rate = min(d["rate"] or ec["target"], ec["ceiling"])
            if th["ask"] and th["ask"] <= rate:              # never counter above what they asked: just accept
                d = {"action": "accept", "rate": th["ask"], "reply": ""}
            elif th["rounds"] >= self.settings["maxRounds"]:
                d = {"action": "decline", "rate": None, "reply": ""}
            else:
                d["rate"] = round(rate, 1)
        return d

    def _negotiate_rules(self, th, text, ec):
        nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", text)]
        ask = None
        if nums:
            n = max(nums)
            ask = n / ec["km"] if n > 300 else n
        if YES.search(text) and th["counter"] and not ask:
            return {"action": "accept", "rate": th["counter"], "ask": None, "reply": ""}
        if NO.search(text) and not ask:
            return {"action": "decline", "rate": None, "ask": None, "reply": "Koi baat nahi 🙏"}
        if not ask:
            return {"action": "ask", "rate": None, "ask": None, "reply": ""}
        if ask <= ec["target"]:
            return {"action": "accept", "rate": ask, "ask": ask, "reply": ""}
        if th["rounds"] == 1:
            return {"action": "counter", "rate": ec["target"], "ask": ask, "reply": ""}
        if ask <= ec["ceiling"]:
            return {"action": "accept", "rate": ask, "ask": ask, "reply": ""}
        if th["rounds"] < self.settings["maxRounds"]:
            return {"action": "counter", "rate": round((ec["target"] + ec["ceiling"]) / 2, 1), "ask": ask, "reply": ""}
        return {"action": "decline", "rate": None, "ask": ask, "reply": ""}

    def _llm_json(self, prompt):
        msg = E.chat([{"role": "user", "content": prompt}], json_mode=True, retries=2, max_wait=8)
        return E.parse_json(msg.get("content") or "")

    def _all_answered(self, load):
        live = [t for t in load["threads"].values() if t["round"] == load["rounds"]]
        return all(t["status"] in ("deal", "declined", "silent") for t in live)

    # ---------------- deciding ----------------
    def _on_tick(self):
        t = now()
        for load in self.loads:
            if load["status"] in ("sourcing",) and t > load.get("deadline", t + 1):
                for th in load["threads"].values():
                    if th["status"] in ("sent", "countered", "reconfirm") and th["round"] == load["rounds"]:
                        th["status"] = "declined" if th["status"] == "countered" else "silent"
                self._decide(load)
            elif load["status"] == "holding" and t > load.get("deadline", t + 1):
                th = load["threads"][load["winner"]]
                th["status"] = "backed_out"
                self._event(load, "Booking", f"{th['name']} didn't confirm the hold in time.")
                self._replan(load, load["winner"])

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
            if load.get("widenLeft", 0) > 0 and not final:
                load["widenLeft"] -= 1
                self._event(load, "Orchestrator", "No offer clears the floor yet. Widening the search.")
                return self._broadcast(load)
            return self._escalate(load, "No owner agreed a rate that clears the margin floor."
                                  if load["threads"] else "No owner replied.")
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
            self._to_owner(load, th, f"Booked ✅ {lane['origin']} → {lane['destination']} at ₹{round(th['agreedBuy'] / lane['distanceKm'], 1)}/km. "
                                     "Pickup details jaldi bhejte hain.")
            self._event(load, "Booking", f"Re-booked with {th['name']}: margin {q['margin_pct']}% at the shipper's fixed ₹{q['sell_total']:,}.")
            self._to_broker(load, f"✅ {load['id']} re-booked with {th['name']} · margin {q['margin_pct']}% "
                                  f"(floor {E.MARGIN['floorPct']:g}%). Shipper still pays ₹{q['sell_total']:,}. No action needed.")
        else:
            load["status"], load["quoteAt"] = "needs_approval", now()
            self._event(load, "Pricing", f"Buy ₹{q['buy_total']:,} → sell ₹{q['sell_total']:,}, margin {q['margin_pct']}% "
                                         f"(floor {E.MARGIN['floorPct']:g}%)." + (f" {q['note']}." if q.get("note") else ""))
            self._to_broker(load, self.quote_text(load), [("approve:" + load["id"], "Approve ✅"), ("reject:" + load["id"], "Reject ❌")])
        self.save()

    def _escalate(self, load, reason):
        load["status"], load["reason"] = "escalated", reason
        load["closedAt"] = now()
        self._event(load, "Orchestrator", f"Escalated to you: {reason}")
        self._to_broker(load, f"❌ {load['id']} not quoted. {reason} Over to you.")
        self.save()

    # ---------------- approval, hold, back-out ----------------
    def _on_approve(self, lid):
        load = self._find(lid)
        if not load or load["status"] != "needs_approval":
            return
        th = load["threads"][load["winner"]]
        load["status"], load["approvedAt"], load["deadline"] = "holding", now(), now() + self.timeout()
        load["fixedSell"] = load["quote"]["sell_total"]
        self._event(load, "Broker", "Approved the quote.")
        th["status"] = "hold_requested"                  # before the message, so the reply is read as the hold answer
        self._to_owner(load, th, f"Pakka karte hain? {load['lane']['origin']} → {load['lane']['destination']}, "
                                 f"₹{round(th['agreedBuy'] / load['lane']['distanceKm'], 1)}/km. Gaadi hold karne ke liye *HAAN* likhiye.")
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
            th["status"] = "backed_out"
            self._event(load, "Booking", f"{th['name']} backed out: “{text[:60]}”.")
            return self._replan(load, key)
        if th["status"] == "hold_requested" and YES.search(text):
            th["status"] = "held"
            load["status"], load["bookedAt"] = "booked", now()
            secs = load["bookedAt"] - load["createdAt"]
            self._event(load, "Booking", f"{th['name']} confirmed the hold. Booking-ready in {int(secs // 60)}:{int(secs % 60):02d}.")
            self._to_owner(load, th, "Dhanyavaad ✅ Booking pakki. Pickup details jaldi bhejte hain.")
            self._to_broker(load, f"✅ {load['id']} booking-ready: {th['name']} confirmed the hold. Quote ₹{load['quote']['sell_total']:,} "
                                  f"can go to the shipper.")
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
        self._to_broker(load, f"⚠️ {name} backed out of {load['id']}. Re-planning on my own; I'll tell you only if the floor is at risk.")
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
        cost = E.fair_cost(km, th["type"], load["lane"]["returnLoadProb"])["est_cost"]
        cost += E.deadhead_cost(th["deadheadKm"] or 0, th["type"]) * E.SRC["deadheadChargedShare"]
        ask, floor = cost * b["ratePremium"] / km, cost * b["minRateMult"] / km
        st, last = th["status"], th["messages"][-1]["text"]
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
        threading.Timer(delay, lambda: self.post("owner_message", key=th["ownerKey"], text=text)).start()

    # ---------------- broker-facing text and console view ----------------
    def quote_text(self, load):
        th, q, f, lane = load["threads"][load["winner"]], load["quote"], load["load"], load["lane"]
        ths = [t for t in load["threads"].values()]
        n = lambda s: sum(t["status"] == s for t in ths)
        lines = [f"*{load['id']}* {lane['origin']} → {lane['destination']} · {lane['distanceKm']} km · {f['weight_ton']:g} T"]
        if load["searchType"] != f["truck_type"]:
            lines.append(f"🔄 Using a {load['searchType']} ({f['truck_type']} can't carry {f['weight_ton']:g} T)")
        lines += [f"📲 {len(ths)} owners asked: {n('deal')} agreed, {n('declined')} declined, {n('silent')} no reply",
                  f"🚛 {th['name']} · {th['type']} · {th['reliability']}★",
                  f"💰 Quote *₹{q['sell_total']:,}* (₹{q['rate_per_km']}/km) · buy ₹{q['buy_total']:,}",
                  f"📊 Margin *{q['margin_pct']}%* (floor {E.MARGIN['floorPct']:g}%)"]
        if q.get("note"):
            lines.append(f"ℹ️ {q['note']}")
        return "\n".join(lines)

    def view(self):
        today = datetime.now(IST).date()
        loads = [l for l in self.loads if datetime.fromtimestamp(l["createdAt"], IST).date() == today]
        ready = [l for l in loads if l.get("quoteAt")]
        mins = sorted((l["quoteAt"] - l["createdAt"]) / 60 for l in ready)
        booked = [l for l in loads if l["status"] in ("booked", "recovered")]
        margins = [l["quote"]["margin_pct"] for l in booked if l.get("quote")]
        kpis = {"received": len(loads), "quoted": len(ready), "booked": len(booked),
                "waiting": sum(l["status"] == "needs_approval" for l in loads),
                "escalated": sum(l["status"] == "escalated" for l in loads),
                "medianQuoteMin": round(mins[len(mins) // 2], 1) if mins else None,
                "avgMargin": round(sum(margins) / len(margins), 1) if margins else None}
        live_owners = [o for o in self.owners.values() if not o.get("sandbox")]
        pool = [t for t in self.pool if (t.get("source") == "sandbox") == (self.settings["mode"] == "sandbox")]
        return {"now": now(), "settings": self.settings, "kpis": kpis, "checkin": self.checkin,
                "owners": [{**{k: o.get(k) for k in ("key", "name", "baseCity", "sandbox")},
                            "phone": ("•••• " + o["phone"][-4:]) if o.get("phone") else None,
                            "reliability": self.reliability_of(o["key"]),
                            "trucks": [t for t in pool if t["ownerKey"] == o["key"]]}
                           for o in (live_owners if self.settings["mode"] == "live" else
                                     [o for o in self.owners.values() if o.get("sandbox")])],
                "poolSize": len(pool), "liveOwners": len(live_owners),
                "loads": [{k: v for k, v in l.items() if k not in ("contacted", "prefer")} for l in reversed(self.loads[-60:])],
                "whatsapp": bool(self.send_wa)}
