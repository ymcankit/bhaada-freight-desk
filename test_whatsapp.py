"""Offline test of the WhatsApp webhook: fake Meta payloads in, captured outgoing messages out.
No Meta account, no network, no LLM.   Run:  python test_whatsapp.py   (needs fastapi + httpx)
"""
import functools
from fastapi.testclient import TestClient
import engine as E
import whatsapp_bot as W

SENT = []
W._post = lambda payload: SENT.append(payload)
E.LLM["key"] = "test"                                        # pretend the model is configured...
E.intake_turn = lambda hist, load, msg: (                    # ...but parse the hero load without calling it
    E.normalise_load(dict(origin="Bhiwandi", destination="Surat", weight_ton=18, truck_type="32 MXL",
                          pickup_when="kal subah")), "Finding the best truck now", [])
W.E.run_quote = functools.partial(E.run_quote, use_llm=False)   # deterministic planner, same tools
_replan = E.run_replan
W.E.run_replan = lambda prev, pool, use_llm=True: _replan(prev, pool, use_llm=False)

client = TestClient(W.app)
ME = "919800000001"

def inbound(msg_id, **content):
    return {"entry": [{"changes": [{"value": {"messages": [{"id": msg_id, "from": ME, **content}]}}]}]}

def body(p):
    return p.get("text", {}).get("body") or p.get("interactive", {}).get("body", {}).get("text", "")

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond

ok = True
r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": W.WA_VERIFY, "hub.challenge": "42"})
ok &= check("webhook verification", r.status_code == 200 and r.text == "42")
ok &= check("wrong verify token rejected", client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x"}).status_code == 403)

client.post("/webhook", json=inbound("m1", type="text", text={"body": "Kal subah Bhiwandi se Surat, 18 ton kapda, 32 MXL chahiye"}))
quote = SENT[-1]
ok &= check("quote sent with buttons", quote.get("type") == "interactive" and "Approve" in str(quote))
ok &= check("quote mentions Taurus substitution", "Taurus" in body(quote))
ok &= check("quote shows margin vs floor", "margin" in body(quote) and "floor" in body(quote))

client.post("/webhook", json=inbound("m1", type="text", text={"body": "duplicate delivery"}))
ok &= check("duplicate message ignored", SENT[-1] is quote)

client.post("/webhook", json=inbound("m2", type="interactive", interactive={"type": "button_reply", "button_reply": {"id": "approve", "title": "Approve"}}))
ok &= check("approve confirms hold", "confirmed the hold" in body(SENT[-1]))

n = len(SENT)
client.post("/webhook", json=inbound("m3", type="interactive", interactive={"type": "button_reply", "button_reply": {"id": "backout", "title": "Owner backs out"}}))
replan = SENT[-1]
ok &= check("back-out triggers re-plan", len(SENT) == n + 2 and "backed out" in body(replan))
ok &= check("re-plan re-books above floor", "Re-booked" in body(replan), body(replan)[:160])
ok &= check("reliability penalised in memory", any(v < 0 for v in W.MEMORY.get("reliability", {}).values()))

client.post("/webhook", json=inbound("m4", type="image", image={"id": "x"}))
ok &= check("unsupported type answered politely", "text and voice" in body(SENT[-1]))
ok &= check("health endpoint", client.get("/").json()["service"] == "bhaada-whatsapp")

print("\n--- what the broker would see on WhatsApp ---\n" + body(quote) + "\n\n" + body(replan))
print("\nALL PASS" if ok else "\nSOME FAILED")
raise SystemExit(0 if ok else 1)
