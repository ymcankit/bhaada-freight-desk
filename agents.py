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
Pricing is not an agent: it is a calculator every agent uses, because money must be exact and explainable.
"""
import re
import threading
import time

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
        return _clean(out.get("reply"), allowed)

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
                "agreed_others": sum(1 for x in load["threads"].values() if x is not th and x["status"] == "deal")}

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
            "reason: one short, respectful Hinglish line the owner will read (the lane's going rate, the distance, "
            "quick payment, regular loads). Put no numbers in it; the desk adds the amount.\n"
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
        done = lambda r: {"action": "accept", "rate": r, "by": "agent",
                          "reply": f"Done ✅ {self._money(round(r * km), km)}. Broker se confirm karke turant batate hain."}
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
            reason = _clean(mv.get("reason"), set(), limit=220)
            if act == "final_offer":
                th["finalOffer"] = True
            held = bool(th["counter"]) and total / km <= th["counter"] + 0.01
            money = self._money(total, km)
            body = (f"Aakhri offer {money}, isse upar sach mein nahi ho payega." if act == "final_offer"
                    else f"Hamara offer wahi hai: {money}. Aap thoda kam karein to baat ban sakti hai." if held
                    else f"Hum {money} de sakte hain.")
            text = (reason + " " if reason else "") + body + " Theek lage to *haan* likhiye."
            return {"action": "counter", "rate": total / km, "reply": text, "by": "agent", "reason": reason}
        if act == "decline":
            if ask and ask > cap and rnd >= 2:
                reason = _clean(mv.get("reason"), set(), limit=220)
                return {"action": "decline", "rate": None, "by": "agent",
                        "reply": (reason + " " if reason else "") + "Is baar nahi ho payega 🙏 Agla load aapko pehle denge."}
            return None
        if act == "ask" and not ask:
            reason = _clean(mv.get("reason"), set(), limit=220)
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
        rows = []
        for i, c in enumerate(cands):
            o = self.owners.get(c["ownerKey"]) or {}
            r = self._owner_record(c["ownerKey"])
            rows.append(f"{i}: {o.get('name')}, {c['type']} in {c.get('city')}, {c.get('deadheadKm', '?')} km from pickup, "
                        f"rated {self.reliability_of(c['ownerKey'])}/5, last 30 days: {r['offers']} offers, {r['replied']} "
                        f"replied, {r['deals']} deals, {r['backouts']} backed out"
                        + (f", said ₹{c['ratePerKm']}/km this morning" if c.get("ratePerKm") else ""))
        lane, f = load["lane"], load["load"]
        try:
            out = self._llm_json(
                "You are Bhaada's SOURCING AGENT. Choose which truck owners to message first for this load.\n"
                f"Load: {lane['origin']} → {lane['destination']}, {lane['distanceKm']} km, {f.get('weight_ton')} T, "
                f"{load['searchType']}, loading {f.get('pickup_when') or 'kal'}.\n"
                "Candidates (index: details):\n" + "\n".join(rows) + "\n"
                f"Pick up to {n}. Prefer owners who reply and complete trips, are close to the pickup, and gave a rate this "
                "morning; skip owners who keep ignoring offers or backed out recently, unless there is no one better.\n"
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
