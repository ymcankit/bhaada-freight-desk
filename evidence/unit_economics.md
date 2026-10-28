# Unit economics (measured where possible, labelled where estimated)

## Cost to run one load

Measured by the local stress test with the real model chain (`stress_test.py real`, 28 Oct 2026): about **6,000 prompt
tokens and 1,200 completion tokens per load** across intake, broker chat and roughly 11 owner messages. The live
desk reports its own running totals in `/healthz` → `llmStats` (calls, tokens, estimated USD per provider).

| Item | Basis | Per load |
|---|---|---|
| Model, OpenAI gpt-5.4-mini ($0.75 / $4.50 per M tokens) | 6k in + 1.2k out | ≈ $0.010 (≈ ₹0.9) |
| Model, Groq gpt-oss-120b ($0.15 / $0.60) when it answers | same | ≈ $0.002 (≈ ₹0.15) |
| Voice note, Whisper large-v3 ($0.111 per audio hour) | 20 s | ≈ ₹0.05 |
| WhatsApp, about 15 business-started utility messages | ₹0.115 + GST each (Meta India rate card) | ≈ ₹2.0 (owner-started messages are free for 24 h) |
| Hosting | Render paid instance ~$7/month + Upstash free | ≈ ₹600/month per agency, fixed |

So the variable cost is roughly **₹3 per load** on WhatsApp (₹1 on Telegram), and about ₹600 a month fixed per agency.
If Meta classes load offers as marketing messages (₹0.86 + GST), WhatsApp rises to about ₹15 a load: design owners to
message first.

## One pricing model (use this everywhere, including the deck)

**₹2,999 a month per agency including 50 placed loads, then ₹75 per placed load.**

- An agency placing 200 loads a month pays ₹2,999 + 150 × ₹75 = **₹14,249**; its variable cost to us is about ₹600 + 200 × ₹3 = **₹1,200**.
- Against the broker's own gross margin per load (₹1,600–3,000 on a ₹40k load at 4–7%), ₹75 is about 3–5%.
- Test ₹4,999 and ₹9,999 flat on the second and third pilots.

## What is assumption, not evidence

The broker-side gains (time to quote, margin points, win rate) are hypotheses until the pilot measures them with shadow
mode (`mera rate BH-xxxx <amount>`, KPI `shadowWithin3Pct`) against the agency's own baseline. See `pilot_plan.md`.
