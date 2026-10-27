# Bhaada: Agentic Freight Desk (Abhikalp 2026, Case 9)

A WhatsApp-style freight desk where an LLM orchestrator takes a messy load request to a booking-ready
quote, then re-books on its own when an owner backs out. Prices never come from the LLM.

## Run locally
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # add a Groq key
    streamlit run app.py

Without a key the app still runs: the sidebar sample loads go through the deterministic planner.

## Files
    whatsapp_bot.py WhatsApp Cloud API webhook: the same desk on a real WhatsApp number (see WHATSAPP.md)
    test_whatsapp.py Offline webhook test with fake Meta payloads
    app.py          Streamlit UI: broker chat, parallel owner chats, agent trace, approval, back-out re-plan
    engine.py       Pricing, truck matching, simulated owner replies, tools, LLM orchestrator + fallback planner
    test_engine.py  Offline tests (no key): matching, pricing, re-plan, orchestrator loop with a scripted model
    smoke_test.py   Live test with the real model on every sample load (run before freezing the build)
    data/           Synthetic owners, trucks, lanes, cost drivers (rates.json), city coordinates, sample loads
    docs/           Case brief, Round 1 slides, master project document

## How a load flows
1. Intake (LLM) turns chat or a voice note into a load; every field is validated against known cities and truck types.
2. The orchestrator (LLM tool-calling) calls `check_load` -> `find_trucks` -> `message_owners` -> `price_quote` ->
   `propose_quote` or `escalate`. It substitutes a bigger truck when the requested one can't carry the weight,
   widens the search when nobody agrees, and can't propose anything below the margin floor.
3. If the LLM call fails or times out, the same tools run on a deterministic planner and the UI says so.
4. Back-out: the owner's reliability drops, the shipper's price stays fixed, owners who agreed earlier are re-confirmed
   first, and the floor is re-checked. Below the floor, it escalates instead of booking.

The header clock is a modelled real-world time built from each owner's reply delay; the demo itself runs faster.
