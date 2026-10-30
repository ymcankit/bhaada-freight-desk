"""The desk's AI agents. Each owns one stage of a load and decides what to do next; code checks every action before
it happens, and today's rules run whenever a model is down or proposes something outside the guardrails.

- Answers (always on): a question an owner asks mid-deal (during a hold, a re-confirmation, as a backup) gets an
  answer from that load's facts instead of a fixed message. Any number the model writes that isn't in the facts
  drops the answer.
- Negotiation agent (agentMode): the model that reads the owner's reply also chooses the move (accept, counter with
  an amount and a reason, final offer, decline, ask). Code holds the line: never above the broker's ceiling, never
  down from our last offer, no step up unless the owner came down, ₹500 lumps, the round limit.
- Sourcing agent (agentMode): picks which owners to message from a longer ranked list, using each owner's record
  with this desk (offers, replies, deals, back-outs). Code checks every pick is a real candidate.
- Coordinator agent (agentMode): plans each load before the first message (search near or wide, the lane's regular
  owners first, how hard to bargain), picks the recovery move when a load is stuck, and picks the winner among
  offers that clear the floor. It only chooses among tools code offers, and code runs them.
- Lane learning (always recorded, used in agentMode): every quoted or handed-over load leaves a record per lane of
  how each negotiation went (opening ask, first counter, close, rounds) and what the desk tried. The agents read the
  lane's playbook before they decide, so the next load on a lane starts from what worked last time.
Pricing is not an agent: it is a calculator every agent uses, because money must be exact and explainable.
"""
import re
import threading
import time
from collections import Counter

import engine as E

NUM = re.compile(r"\d+(?:[,.]\d+)*")


def _nums(s):
    return {n.replace(",", "") for n in NUM.findall(str(s or ""))}


def _clean(text, allowed, limit=400):
    """A model's sentence, kept only if every number in it is one the code knows (the facts or the amount)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:limit]
    if not text:
        return ""
    ok = {a.replace(",", "") for a in allowed}
    return text if _nums(text) <= ok else ""


# How hard the coordinator tells the negotiation agent to bargain on a load (the limits are the same for all three)
STANCES = {"firm": "bargain hard: open low and concede slowly; owners on this lane usually come down a long way.",
           "standard": "open near our target and meet owners part-way when they come down.",
           "quick": "close fast: pickup is soon or trucks are scarce, so accept a fair ask inside the limit sooner."}


def _med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


# Promises the broker never made (when payment comes, more loads later): the desk can't hold an owner to them
PROMISE = re.compile(r"payment|paisa|paise|pay\b|advance|bhugtan|regular|aage bhi|agla load|agle load|future|"
                     r"aur load|more loads|repeat|har hafte|roz ka|kaam milta|kaam milega|kaam dete|pehle denge", re.I)
# The broker's side of the deal is never told to an owner: margin, our limit, what the shipper pays
INSIDE = re.compile(r"margin|munafa|munaafa|commission|ceiling|limit|shipper|broker ka|hamara fayda|profit", re.I)


def _reason(mv, allowed=()):
    """The agent's line to the owner: no numbers except this owner's own past amounts, and no promises."""
    r = _clean((mv or {}).get("reason"), set(allowed), limit=220)
    return "" if PROMISE.search(r) or INSIDE.search(r) else r


class AgentsMixin:
    # ---------------- switches ----------------
    def agent_mode(self):
        return bool(self.settings.get("agentMode")) and E.llm_ready()

    # ---------------- facts the agents may state ----------------
    def _stage_facts(self, load, th):
        """What is true about this owner and this load right now, in plain lines, plus every number in them."""
        lane, f = load.get("lane") or {}, load["load"]
        km = lane.get("distanceKm") or 1
        lines = [f"Load {load['id']}: {lane.get('origin')} → {lane.get('destination')}, {km} km, {f.get('weight_ton')} T"
                 + (f" {f['material']}" if f.get("material") and f["material"] != "general" else "")
                 + f", truck {th.get('type') or load.get('searchType')}, loading {f.get('pickup_when') or 'kal'}."]
        if th.get("agreedBuy"):
            lines.append(f"Rate agreed with this owner: ₹{th['agreedBuy']:,} total (diesel and toll included).")
        lines.append("Payment terms: " + self.terms_line(th))
        winner = load.get("winner")
        status = th.get("status")
        if status == "hold_requested":
            lines.append("The broker approved this owner's rate. The owner must reply HAAN with the vehicle number, the "
                         "driver's mobile and the reporting time at loading to hold the truck.")
        elif status == "reconfirm":
            lines.append("The owner who was booked has backed out. This owner had agreed earlier; we are asking if their "
                         "earlier rate still holds so the load can be given to them.")
        elif status == "held":
            lines.append("This owner's truck is booked for this load.")
        elif winner and winner != th.get("ownerKey") and status == "deal":
            w = load["threads"].get(winner) or {}
            why = []
            if w.get("agreedBuy") and th.get("agreedBuy") and w["agreedBuy"] < th["agreedBuy"]:
                why.append(f"the other owner agreed at a lower rate (₹{w['agreedBuy']:,})")
            if w.get("reliability") and th.get("reliability") and w["reliability"] > th["reliability"]:
                why.append("the other owner has a stronger record of completing trips")
            lines.append("Another owner was chosen for this load" + (" because " + " and ".join(why) if why else "")
                         + ". This owner is the backup: if the chosen owner doesn't send the truck, this owner is asked first.")
        allowed = set()
        for ln in lines:
            allowed |= _nums(ln)
        return lines, allowed

    def _stage_answer(self, load, th, text):
        """A model's answer to an owner's question about this load, or "" (then the caller's fixed message stands)."""
        if not E.llm_ready():
            return ""
        lines, allowed = self._stage_facts(load, th)
        hist = "\n".join(("Bhaada: " if m["dir"] == "out" else "Owner: ") + m["text"] for m in th.get("messages", [])[-6:])
        try:
            out = self._llm_json(
                "You are Bhaada's BOOKING AGENT, talking to a truck owner in the middle of a deal. Answer their message "
                "in 1-2 short lines using ONLY these facts:\n" + "\n".join("- " + x for x in lines) + "\n"
                f"Recent chat (latest last):\n{hist}\nOwner's message: \"{text}\"\n"
                "Rules: never invent a rate, a date, a time or a promise; if the answer isn't in the facts, say the broker "
                "will confirm. Don't ask them to reply HAAN/NAHI (the desk adds that). Reply in Hinglish (Hindi words in "
                "Roman script), warm and plain, like a good coordinator.\n"
                'Return ONLY JSON {"reply":""}', tier="cheap")
        except Exception as e:
            print("booking agent failed, using the fixed message:", repr(e)[:120])
            return ""
        reply = _clean(out.get("reply"), allowed)
        return "" if INSIDE.search(reply) else reply

    def _answer_then(self, load, th, text, after):
        """Answer off the worker (a model call can take seconds), then send the answer plus `after` on the worker."""
        snap_load = {"id": load["id"], "lane": dict(load.get("lane") or {}), "load": dict(load["load"]),
                     "winner": load.get("winner"), "threads": {k: dict(v) for k, v in load["threads"].items()},
                     "reason": load.get("reason")}
        snap_th = {**th, "messages": list(th.get("messages", [])[-6:])}

        def run():
            reply = self._stage_answer(snap_load, snap_th, text)
            self.post("owner_answer", lid=load["id"], key=th["ownerKey"], reply=reply, after=after,
                      status=th.get("status"))
        threading.Thread(target=run, daemon=True).start()

    def _on_owner_answer(self, lid, key, reply, after, status):
        load = self._find(lid)
        th = (load or {}).get("threads", {}).get(key)
        if not th or th.get("status") != status:
            return                                     # the deal moved on while the model was answering
        if reply:
            self._event(load, "Booking", f"Booking agent answered {th['name']}: “{reply[:80]}”", by="AI")
        self._to_owner(load, th, (reply + "\n" if reply else "") + after)

    # ---------------- negotiation agent ----------------
    def _agent_ctx(self, load, th, ec):
        """The numbers the negotiation agent decides with (₹ totals for this trip), worked out by code."""
        km = ec["km"]
        rival = self._best_rival(load, th)
        cap = min(ec["ceiling"], rival) if rival else ec["ceiling"]
        return {"km": km, "target": round(ec["target"] * km), "cap": round(cap * km), "cost": round(ec["cost"] * km),
                "last": round(th["counter"] * km) if th.get("counter") else None,
                "prev_ask": round(th["ask"] * km) if th.get("ask") else None,
                "round": th["rounds"], "max_rounds": self.settings["maxRounds"],
                "agreed_others": sum(1 for x in load["threads"].values() if x is not th and x["status"] == "deal"),
                "playbook": self._playbook(load, th.get("type")), "stance": (load.get("plan") or {}).get("stance"),
                "history": self._owner_history(load, th)[0]}

    @staticmethod
    def _agent_prompt(ctx):
        last = f"₹{ctx['last']:,}" if ctx["last"] else "none yet"
        prev = f"₹{ctx['prev_ask']:,}" if ctx["prev_ask"] else "none yet"
        return (
            "\nYou are also Bhaada's NEGOTIATION AGENT, buying this trip for the broker. Facts in ₹ totals for the trip "
            f"({ctx['km']} km): aim to buy at about ₹{ctx['target']:,}; the most we may pay is ₹{ctx['cap']:,} (never "
            f"more); the truck's running cost is about ₹{ctx['cost']:,}; our last offer: {last}; owner's previous ask: "
            f"{prev}; this is round {ctx['round']} of {ctx['max_rounds']}; other owners already agreed: {ctx['agreed_others']}.\n"
            "Choose the next move:\n"
            "- accept: their ask is at or below the most we may pay, and pushing further risks losing a good truck.\n"
            "- counter: amount_total between our last offer and the most we may pay, and below their ask. Move up by "
            "less than they came down; hold your offer if they didn't come down.\n"
            "- final_offer: only in the last round; one last amount, not above the most we may pay.\n"
            "- decline: their ask stays far above the most we may pay after 2 or more rounds.\n"
            "- ask: they haven't said a rate yet.\n"
            + (f"What this desk learned on this lane from its own past deals: {'; '.join(ctx['playbook'])}. Pitch your "
               "counters where owners here actually settle, not at the bottom of the range and not above it.\n"
               if ctx.get("playbook") else "")
            + (f"The coordinator's stance for this load: {STANCES[ctx['stance']]}\n" if ctx.get("stance") in STANCES else "")
            + (f"This owner's own history with us on this lane: {'; '.join(ctx['history'])}. You may remind them of their "
               "own past rate in the reason (e.g. \"Pichli baar aapne isi route pe ₹21,500 mein gaadi di thi\"), using only "
               "these exact amounts and no dates.\n" if ctx.get("history") else "")
            + "reason: one short, respectful Hinglish line the owner will read, from the lane's going rate, the distance "
            "or the truck's costs only. Promise nothing the broker hasn't said: not when payment comes, not more "
            "loads later. Never mention our margin, our limit or what the shipper pays. Put no numbers in it; the "
            "desk adds the amount.\n"
            'Add to the JSON: "move":{"action":"accept|counter|final_offer|decline|ask","amount_total":null,"reason":""}')

    def _agent_move(self, load, th, ec, read):
        """The agent's move after code's checks, or None (then the rules decide)."""
        mv = read.get("move") or {}
        act = mv.get("action")
        km, ask, rnd, last = ec["km"], th["ask"], th["rounds"], self.settings["maxRounds"]
        if read["intent"] in ("refuse",) or (read["intent"] == "accept_offer" and th["counter"]):
            return None                                 # the rules already answer these exactly
        if ask and ask < ec["cost"] and not th.get("lowChecked"):
            return None                                 # a below-cost ask is confirmed by the rules first
        rival = self._best_rival(load, th)
        cap = min(ec["ceiling"], rival) if rival else ec["ceiling"]
        moved = th.get("askDrop", 0) > 0
        mine = self._owner_history(load, th)[1]          # the only rupee amounts a reason may quote: this owner's own deals
        done = lambda r: {"action": "accept", "rate": r, "by": "agent",
                          "reply": f"{self._money(round(r * km), km)} note kar liya ✅ Abhi pakka nahi hai: broker se confirm karke turant batate hain."}
        if act == "accept":
            if not ask or ask > cap:
                return None
            if ask > ec["target"] and rnd < 2:
                return None                             # no paying above target on the first reply: counter first
            return done(ask)
        if act in ("counter", "final_offer"):
            try:
                r = float(str(mv.get("amount_total")).replace(",", "")) / km
            except (TypeError, ValueError):
                return None
            if act == "final_offer" and rnd < last - 1:
                return None
            if rnd > last:
                return None
            if ask and r >= ask:
                if ask <= cap:
                    return done(ask)
                r = cap                                  # more than they asked, but their ask is over our limit
            r = min(r, cap)
            if th["counter"] and not moved:              # they didn't come down: repeat our exact offer
                total = round(th["counter"] * km)
            else:
                if th["counter"]:
                    r = max(r, th["counter"])            # never down from our last offer
                    r = min(r, th["counter"] + max(th.get("askDrop") or 0, 0.5))
                total = self._lump(r, km, cap, floor=th["counter"] or 0)
            if ask and total / km >= ask:
                return done(ask) if ask <= cap else None
            reason = _reason(mv, mine)
            if act == "final_offer":
                th["finalOffer"] = True
            held = bool(th["counter"]) and total / km <= th["counter"] + 0.01
            money = self._money(total, km)
            body = (f"Aakhri offer {money}, isse upar sach mein nahi ho payega." if act == "final_offer"
                    else f"Hamara offer wahi hai: {money}. Aap thoda kam karein to baat ban sakti hai." if held
                    else f"Hum {money} de sakte hain.")
            last = [x for x in self._owner_history(load, th)[0] if x.startswith("their last trip")]
            if last and rnd <= 1 and "ichli baar" not in reason:   # their own past rate on this route, said once
                body += f" Pichli baar aapne isi route pe {last[0].split(' was ')[1].replace(' total', '')} mein gaadi di thi."
            text = (reason + " " if reason else "") + body + " Theek lage to *haan* likhiye."
            return {"action": "counter", "rate": total / km, "reply": text, "by": "agent", "reason": reason}
        if act == "decline":
            if ask and ask > cap and rnd >= 2:
                reason = _reason(mv, mine)
                return {"action": "decline", "rate": None, "by": "agent",
                        "reply": (reason + " " if reason else "") + "Is baar nahi ho payega 🙏 Dhanyavaad."}
            return None
        if act == "ask" and not ask:
            reason = _reason(mv, mine)
            return {"action": "ask", "rate": None, "by": "agent", "reply": (reason + " " if reason else "")
                    + "Is load ka aapka rate kya hoga? Total (ya ₹/km) bata dijiye."}
        return None

    # ---------------- sourcing agent ----------------
    def _owner_record(self, key, days=30):
        """This owner's record with the desk lately: offers, replies, deals, back-outs."""
        since = time.time() - days * 86400
        rec = {"offers": 0, "replied": 0, "deals": 0, "backouts": 0}
        for load in self.loads[-300:]:
            th = load["threads"].get(key)
            if not th or (load.get("createdAt") or 0) < since:
                continue
            rec["offers"] += 1
            if th["status"] not in ("sent", "silent"):
                rec["replied"] += 1
            if th.get("agreedBuy"):
                rec["deals"] += 1
            if th["status"] == "backed_out":
                rec["backouts"] += 1
        return rec

    def _agent_pick(self, load, cands, n):
        """The sourcing agent chooses whom to message from the ranked candidates; code keeps only real candidates."""
        if len(cands) <= n:
            return cands, ""
        rows, regs = [], self._regulars(load)
        for i, c in enumerate(cands):
            o = self.owners.get(c["ownerKey"]) or {}
            r = self._owner_record(c["ownerKey"])
            rows.append(f"{i}: {o.get('name')}, {c['type']} in {c.get('city')}, {c.get('deadheadKm', '?')} km from pickup, "
                        f"rated {self.reliability_of(c['ownerKey'])}/5, last 30 days: {r['offers']} offers, {r['replied']} "
                        f"replied, {r['deals']} deals, {r['backouts']} backed out"
                        + (f", said ₹{c['ratePerKm']}/km this morning" if c.get("ratePerKm") else "")
                        + (f", ran this lane for us {regs[c['ownerKey']]} time(s)" if regs.get(c["ownerKey"]) else ""))
        lane, f = load["lane"], load["load"]
        try:
            out = self._llm_json(
                "You are Bhaada's SOURCING AGENT. Choose which truck owners to message first for this load.\n"
                f"Load: {lane['origin']} → {lane['destination']}, {lane['distanceKm']} km, {f.get('weight_ton')} T, "
                f"{load['searchType']}, loading {f.get('pickup_when') or 'kal'}.\n"
                "Candidates (index: details):\n" + "\n".join(rows) + "\n"
                f"Pick up to {n}. Prefer owners who reply and complete trips, are close to the pickup, and gave a rate this "
                "morning or already ran this lane for us; skip owners who keep ignoring offers or backed out recently, unless there is no one better.\n"
                "In why, describe the owners you chose; don't mention index numbers.\n"
                'Return ONLY JSON {"pick":[indices in order],"why":"one short sentence"}', tier="cheap")
            picks = []
            for x in out.get("pick") or []:
                try:
                    i = int(x)
                except (TypeError, ValueError):
                    continue                            # one bad pick doesn't throw away the good ones
                if 0 <= i < len(cands) and i not in picks:
                    picks.append(i)
            picks = picks[:n]
            if picks:
                return [cands[i] for i in picks], str(out.get("why") or "")[:160]
        except Exception as e:
            print("sourcing agent failed, using the ranked order:", repr(e)[:120])
        return cands[:n], ""

    # ---------------- lane learning: what worked on this lane, from the desk's own deals ----------------
    @staticmethod
    def _book_key(load):
        """Demo owners teach demo loads and real owners teach real loads: one never skews the other."""
        demo = load.get("mode") == "sandbox" or load.get("demoOnly")
        return load["lane"]["laneId"] + ("|demo" if demo else "")

    def _first_ask(self, load, th):
        if th.get("firstAsk"):
            return th["firstAsk"]
        for m in th.get("messages", []):                # loads from before firstAsk was kept
            if m.get("dir") == "in":
                r = self._owner_rate(m.get("text", ""), load["lane"]["distanceKm"])
                if r:
                    return r
        return th.get("ask")

    def _learn(self, load, outcome):
        """Once per load, at its first quote or when it's handed over: how each negotiation went and what the desk
        tried, kept per lane (the last 40 negotiations, 20 loads)."""
        lane = load.get("lane") or {}
        if load.get("learned") or not lane.get("laneId") or load.get("phase") == "replan":
            return
        load["learned"] = True
        t = load.get("quoteAt") or load.get("closedAt") or load.get("createdAt") or time.time()
        book = self.lane_book.setdefault(self._book_key(load), {"negs": [], "loads": []})
        replied = 0
        for th in load["threads"].values():
            heard = any(m.get("dir") == "in" for m in th.get("messages", []))
            replied += heard
            first = self._first_ask(load, th)
            if not first or not th.get("sell"):
                continue
            target = self._econ(load, th)["target"]
            out = "deal" if th.get("agreedBuy") else "declined" if th["status"] == "declined" else "open"
            book["negs"].append({"t": t, "type": th["type"], "out": out, "open": round(first / target, 3),
                                 "close": round(th["agreedBuy"] / lane["distanceKm"] / first, 3) if th.get("agreedBuy") else None,
                                 "c1": round(th["firstCounter"] / first, 3) if th.get("firstCounter") else None,
                                 "rounds": th.get("rounds", 0), "by": "agent" if th.get("agentMoves") else "rules"})
        book["loads"].append({"t": t, "outcome": outcome, "asked": len(load["threads"]), "replied": replied,
                              "deals": sum(1 for x in load["threads"].values() if x.get("agreedBuy")),
                              "wide": load.get("rounds", 1) > 1 or bool(load.get("wideStart")),
                              "tried": list(load.get("tried", [])), "plan": dict(load.get("plan") or {})})
        del book["negs"][:-40]
        del book["loads"][:-20]

    def _backfill_book(self):
        """A desk that starts with past loads but no playbook learns from them once."""
        for load in self.loads:
            try:
                if load.get("quote") or load.get("quoteAt"):
                    self._learn(load, "quoted")
                elif load["status"] == "escalated":
                    self._learn(load, "escalated")
            except Exception as e:
                print("lane learning skipped a load:", load.get("id"), repr(e)[:80])

    def _playbook(self, load, tt=None):
        """What the desk learned on this lane in the last 60 days, as plain lines for the agents ([] if too little)."""
        if not (load.get("lane") or {}).get("laneId"):
            return []
        book = self.lane_book.get(self._book_key(load)) or {}
        since = time.time() - 60 * 86400
        negs = [r for r in book.get("negs", []) if r["t"] >= since and (not tt or r["type"] == tt)]
        loads = [r for r in book.get("loads", []) if r["t"] >= since]
        pct = lambda x: round(abs(x) * 100)
        lines = []
        if len(negs) >= 3:
            o = _med([r["open"] for r in negs]) - 1
            lines.append(f"owners' opening ask was a median {pct(o)}% {'above' if o >= 0 else 'below'} our target "
                         f"({len(negs)} owners)" + (", already close to it" if o <= 0.03 else ""))
            deals = [r for r in negs if r["out"] == "deal" and r.get("close")]
            if deals:
                give = 1 - _med([r["close"] for r in deals])
                lines.append(f"deals closed a median {pct(give)}% below the owner's opening ask, in a median "
                             f"{_med([r['rounds'] for r in deals])} round(s) ({len(deals)} deals)"
                             + (": owners here mostly settle at their first ask, so there is little room to bargain" if give < 0.03
                                else ": owners here come down a long way when pushed" if give >= 0.08 else ""))
            near = [r for r in negs if r.get("c1") and r["c1"] >= 0.93]
            deep = [r for r in negs if r.get("c1") and r["c1"] < 0.93]
            if near and deep:
                rate = lambda rs: f"{sum(r['out'] == 'deal' for r in rs)} of {len(rs)}"
                lines.append(f"first counters within 7% of the ask closed {rate(near)}; deeper first counters closed {rate(deep)}")
            lines.append(f"{sum(r['out'] == 'declined' for r in negs)} of {len(negs)} negotiations ended with no deal")
        if len(loads) >= 2:
            asked = sum(r["asked"] for r in loads) or 1
            lines.append(f"over the last {len(loads)} loads {pct(sum(r['replied'] for r in loads) / asked)}% of owners messaged "
                         f"replied, {sum(r['wide'] for r in loads)} needed a wider search, "
                         f"{sum(r['outcome'] == 'quoted' for r in loads)} reached a quote")
            tried = Counter(m for r in loads for m in r["tried"])
            worked = Counter(m for r in loads if r["outcome"] == "quoted" for m in r["tried"])
            if tried:
                lines.append("recovery moves: " + ", ".join(f"{m.replace('_', ' ')} led to a quote {worked[m]} of {n} times"
                                                            for m, n in tried.most_common()))
        return lines

    def _regulars(self, load):
        """Owners who already ran this lane for us, and how many times (from lane memory)."""
        lid, out = load["lane"]["laneId"], {}
        for k, rows in list(self.lane_rates.items()):
            if k.partition("|")[0] == lid:
                for r in rows:
                    if len(r) > 2:
                        out[r[2]] = out.get(r[2], 0) + 1
        return out

    # ---------------- coordinator agent ----------------
    def _start_sourcing(self, load):
        """The coordinator plans the load before the first message goes out; code checks the plan and runs it."""
        if self.agent_mode():
            try:
                self._coordinator_plan(load)
            except Exception as e:
                print("coordinator plan failed, sourcing as usual:", repr(e)[:120])
        self._broadcast(load)

    def _coordinator_plan(self, load):
        lane, f, tt = load["lane"], load["load"], load["searchType"]
        n, radius = E.SRC["ownersPerBroadcast"], self.settings["maxDeadheadKm"]
        near = self._candidates(load, radius, n=50)
        tools = {"source_nearby": f"message the best {n} owners within {radius} km of {lane['origin']} ({len(near)} available)"}
        if load.get("widenLeft", 0) > 0:
            wide = self._candidates(load, radius * 2, n=50)
            if len(wide) > len(near):
                tools["source_wide"] = (f"start with a {radius * 2} km search ({len(wide)} owners) now instead of keeping "
                                        "it for later")
        regs = self._regulars(load)
        known = [c for c in near if regs.get(c["ownerKey"])]
        if known:
            names = ", ".join((self.owners.get(c["ownerKey"]) or {}).get("name", "?") for c in known[:4])
            tools["regulars_first"] = f"message first the {len(known)} nearby owner(s) who already ran this lane for us ({names})"
        sell = self._sell(load, tt)
        mb, c = E.band(lane), E.lane_cost(lane, tt)
        cap, target = round(sell * (1 - mb["floorPct"] / 100)), round(sell * (1 - mb["targetPct"] / 100))
        book = self._playbook(load, tt)
        out = self._llm_json(
            "You are Bhaada's COORDINATOR AGENT. Plan how the desk sources and negotiates this load before any owner is "
            "messaged. Pick ONE tool and ONE stance.\n"
            f"Load: {lane['origin']} → {lane['destination']}, {lane['distanceKm']} km, {f.get('weight_ton')} T, {tt}, "
            f"loading {f.get('pickup_when') or 'kal'}. We aim to pay owners about ₹{target:,} and may pay at most ₹{cap:,}; "
            f"the lane's usual owner rate is about ₹{round(c['owner_market'], -2):,.0f} and the truck costs about "
            f"₹{c['owner_cost']:,} to run.\n"
            "What this desk learned on this lane: " + ("; ".join(book) if book else "no history yet") + ".\n"
            "Tools:\n" + "\n".join(f"- {k}: {v}" for k, v in tools.items()) + "\n"
            "Stances (the price limits are the same for all):\n" + "\n".join(f"- {k}: {v}" for k, v in STANCES.items()) + "\n"
            "Guidance: few owners nearby or a lane where many loads needed a wider search: source_wide. Regular owners "
            "who closed here before are the likeliest to say yes: regulars_first. firm only when the history says owners "
            "come down a long way; little room to bargain: standard; loading today or a thin lane: quick. With no "
            "history, source_nearby and standard. Base your why only on the facts above.\n"
            'Return ONLY JSON {"tool":"<tool>","stance":"firm|standard|quick","why":"one short sentence"}', tier="cheap")
        tool = out.get("tool") if out.get("tool") in tools else "source_nearby"
        stance = out.get("stance") if out.get("stance") in STANCES else "standard"
        why = re.sub(r"\s+", " ", str(out.get("why") or "")).strip()[:200]
        load["plan"] = {"tool": tool, "stance": stance}
        if tool == "source_wide":
            load["widenLeft"] -= 1
            load["wideStart"] = True
        elif tool == "regulars_first":
            load["prefer"] = {c["ownerKey"]: None for c in known}
        if book:
            self._event(load, "Learning", f"Lane playbook for {lane['origin']} → {lane['destination']}: " + "; ".join(book) + ".")
        self._event(load, "Coordinator", f"(AI) plan: {tool.replace('_', ' ')}, {stance} stance." + (f" Why: {why}" if why else ""),
                    by="AI", moves=tools, chosen=tool, why=why)

    def _coordinator_pick(self, load, offers):
        """Among offers that clear the floor, the coordinator may prefer a surer owner over a slightly better margin.
        offers: (margin %, rating, key), best margin first. Returns (offer, why) or (None, "")."""
        best = offers[0][0]
        ok = [o for o in offers if best - o[0] <= 2]    # never give up more than 2 points of margin
        if len(ok) < 2:
            return None, ""
        regs, rows = self._regulars(load), []
        for i, (m, rel, key) in enumerate(ok):
            th, r = load["threads"][key], self._owner_record(key)
            rows.append(f"{i}: {th['name']}, margin {m}%, rated {rel}/5, {th.get('deadheadKm', '?')} km from pickup, last 30 "
                        f"days {r['deals']} deals and {r['backouts']} back-outs, ran this lane for us {regs.get(key, 0)} time(s)")
        try:
            out = self._llm_json(
                "You are Bhaada's COORDINATOR AGENT. These owners all agreed a rate that clears the broker's margin. Choose "
                "who gets the load; the others stay as backups. A no-show costs far more than a point of margin.\n"
                + "\n".join(rows) + '\nReturn ONLY JSON {"pick":<index>,"why":"one short sentence"}', tier="cheap")
            i = int(out.get("pick"))
        except Exception as e:
            print("coordinator pick failed, using the rules:", repr(e)[:120])
            return None, ""
        if not 0 <= i < len(ok):
            return None, ""
        return ok[i], re.sub(r"\s+", " ", str(out.get("why") or "")).strip()[:200]

    # ---------------- this owner's own history with the desk ----------------
    def _owner_history(self, load, th):
        """What this owner did with us on this lane: plain lines for the negotiator, and the exact amounts it may quote.
        Only their own deals, so nothing about other owners or the broker's side leaks into a message."""
        lane, key = load.get("lane") or {}, th.get("ownerKey")
        lid = lane.get("laneId")
        if not lid or not key:
            return [], set()
        trips = sum(1 for k, rows in list(self.lane_rates.items()) if k.partition("|")[0] == lid
                    for r in rows if len(r) > 2 and r[2] == key)
        booked, agreed = [], []
        for l in self.loads[-300:]:
            t = l["threads"].get(key)
            if l is load or not t or not t.get("agreedBuy") or (l.get("lane") or {}).get("laneId") != lid:
                continue
            when = l.get("bookedAt") or l.get("quoteAt") or l["createdAt"]
            if l.get("winner") == key and l["status"] in ("booked", "recovered", "holding"):
                booked.append((when, t["agreedBuy"]))
            elif l.get("winner") and l.get("winner") != key:
                agreed.append((when, t["agreedBuy"]))
        lines, amounts = [], set()
        if trips:
            lines.append(f"ran this lane for us {trips} time(s)")
        if booked:
            amt = max(booked)[1]
            lines.append(f"their last trip with us on this lane was ₹{amt:,} total")
            amounts.add(str(amt))
        if agreed:
            amt = max(agreed)[1]
            lines.append(f"last time they agreed ₹{amt:,} total, but another owner was chosen and they were the backup")
            amounts.add(str(amt))
        return lines, amounts
