# How Bhaada works

One sentence: **code owns money and state; language models read people and choose among moves the code allows.**
Every step a load goes through is logged with who decided it (AI, Code, Human, or Demo for simulated personas), and the
console shows that trace.

## The pieces

| Piece | File | What it does |
|---|---|---|
| Channels | `server.py` | Telegram bot (live), WhatsApp Cloud API webhook (built and signature-checked, waiting on Meta onboarding), web console |
| Router | `server.py` `on_text` | Role buttons (broker / truck owner); a model call (`broker_brain`) decides whether a broker's message is a load or a question, skipped when the text is plainly a load |
| Intake | `engine.intake_turn` | A model reads the load across turns; code validates every field (cities, tons, goods, truck, day, budget, invoice value); rules read it when every model is down |
| Goods and truck | `engine.truck_fit` | Material rules (body, hazmat, volume, premium) pick or correct the truck in code |
| Routing | `geo.py` | Nominatim/Photon geocoding and OSRM road km, cached; new places are confirmed by the broker |
| The desk | `live.py` `Desk` | One event queue and worker plus a 1 s clock own every load's state; timers (reply rounds, hold, quote expiry, waiting for trucks) |
| Sourcing | `Desk._broadcast` | Messages nearby owners at once: real owners first, then simulated demo owners if switched on |
| Negotiation | `Desk._model_read` + `Desk._next_move` | A model reads the owner's reply on a background thread; code plans the counter (concession ladder, ceiling, real rival offers) and explains it with the trip's costs |
| Pricing | `engine.price_quote` | Only code: market rate fitted to FR8 lane listings, owner running-cost floor, goods and season premiums, lane memory |
| Recovery | `Desk._strategize` | When nothing clears the floor, a model picks one move from the list code builds (widen, bigger truck, final offer, ask the broker, hand over); code checks and runs it; a fixed order is the fallback |
| After the quote | `Desk._on_approve` … `_take_dispatch` | Broker approves; owner holds; broker relays the shipper's yes or no; owner sends vehicle and driver; dispatch card, LR (bilty) draft PDF, e-way bill and GST lines |
| Back-outs | `Desk._replan` | Re-confirms earlier owners, then re-sources, at the shipper's fixed price |
| Memory | `live.py` save/load | Upstash Redis (compressed blob, coalesced writes), local file fallback |

## What the models do, and what they can't do

- **Read**: intake, broker questions, owner check-ins and chat, owner replies in a negotiation, PDFs and photos.
- **Choose**: the strategist's recovery move, only from the moves code offers.
- **Never**: set a price, accept a rate nobody said, book or approve anything, see another person's loads. Guardrails in
  code (`_next_move`, `_model_read`, `_options`) override any model output that tries; `test_llm_paths.py` feeds
  hostile model outputs to check this.

## What is simulated

Demo owners and the demo broker (`aiAgents` setting) reply from rules, priced off the same cost model, so they show the
workflow, not market outcomes. They are labelled "demo owner, simulated" and get a "Demo" badge.

## Limits today

Single process and single tenant (one agency); state as one blob; free hosting; WhatsApp waiting on Meta; VAHAN, KYC,
payments and POD not built. See `README.md` for the real-vs-demo table and `evidence/` for the pilot plan.
