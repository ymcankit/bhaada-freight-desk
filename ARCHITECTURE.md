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
| Agents | `agents.py` `AgentsMixin` | Booking agent answers owners' questions mid-deal from the load's facts; with `agentMode` on, a Sourcing agent picks whom to message from a ranked list of twice the usual size, and a Negotiation agent proposes each move (ask, counter, final offer, accept, decline) in the same model call that reads the reply; a Coordinator agent plans each load before the first message (search nearby, search wide from the start, or the lane's regular owners first; how hard to bargain), picks the recovery move when a load is stuck, and picks the winner among offers within 2 margin points of the best |
| Lane learning | `agents.py` `_learn`, `_playbook` | Every quoted or handed-over load records, per lane, how each negotiation went (owner's opening ask against our target, first counter, close, rounds) and what the desk tried. The agents read the lane's playbook before deciding, so the next load on a lane starts from what worked. Demo loads learn only from demo loads |
| Memory | `live.py` save/load | Upstash Redis (compressed blob, coalesced writes), local file fallback |

## What the models do, and what they can't do

- **Read**: intake, broker questions, owner check-ins and chat, owner replies in a negotiation, PDFs and photos.
- **Choose**: the strategist's recovery move, only from the moves code offers. With `agentMode` on (console
  checkbox on Pricing defaults, or env `AGENT_MODE=1`; off by default), also whom to message and the next negotiation
  move, the plan for each load and the winner. Pricing stays a code tool the agents call, never something they set.
  The lane playbook is worked out by code from the desk's own deals; the agents read it, they don't write it.
- **Checked**: `_agent_move` clamps every agent amount to the broker's ceiling, holds the offer when the owner didn't
  move, steps up by at most the owner's own drop, refuses a first-round accept above target or a first-round decline,
  and drops any reason that quotes a number. Anything it rejects goes to the rules (`_next_move`). The Booking agent's
  answer is dropped if it names a number that isn't in the load's facts, and is not sent if the deal moved meanwhile.
- **Never**: set a price, accept a rate nobody said, book or approve anything, see another person's loads. Guardrails in
  code (`_next_move`, `_model_read`, `_options`) override any model output that tries; `test_llm_paths.py` feeds
  hostile model outputs to check this.

## What is simulated

Demo owners and the demo broker (`aiAgents` setting) reply from rules, priced off the same cost model, so they show the
workflow, not market outcomes. They are labelled "demo owner, simulated" and get a "Demo" badge.

## Limits today

Single process and single tenant (one agency); state as one blob; free hosting; WhatsApp waiting on Meta; VAHAN, KYC,
payments and POD not built. See `README.md` for the real-vs-demo table and `evidence/` for the pilot plan.
