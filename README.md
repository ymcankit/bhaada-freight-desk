# Bhaada: Agentic Freight Desk (Abhikalp 2026, Case 9)

A freight broker's desk that takes a load from WhatsApp to a booking-ready truck: it reads the request, finds the road
distance, messages fleet owners on WhatsApp in parallel, negotiates inside the broker's margin band, asks for one
approval, gets the owner to hold, and re-books on its own when an owner backs out. Prices come only from code.

## Run
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml     # add your Groq key (and WhatsApp keys)
    uvicorn server:app --port 8000                                   # open http://localhost:8000

**On a public host** set `CONSOLE_PASSWORD`: without it the console on Render is view-only (`CONSOLE_OPEN=1` opens
it to anyone). `PUBLIC_VIEW=1` lets anyone watch while only the password approves or changes settings.

**Live owners** works loads against real people on WhatsApp (setup in WHATSAPP.md) or Telegram: make a bot with @BotFather and set `TELEGRAM_BOT_TOKEN`; the server registers its own webhook.
**Sandbox owners** uses persona owners from `data/owners.json` so anyone can try the desk; the console labels it.

## What is real
| Part | Source |
|---|---|
| Load intake (text, Hinglish, voice notes) | LLM (Groq), every field validated in code |
| Cities and road distance | OpenStreetMap Nominatim + OSRM, any Indian city pair, cached |
| Owners, check-in, negotiation, holds, back-outs | Real WhatsApp messages (Meta Cloud API) in Live mode |
| Negotiation replies | LLM agent; the ceiling and round limit are enforced in code; rule-based fallback |
| Diesel, tolls, margin band | Settings with source and date, editable in the console |
| Price | `engine.price_quote` only |

## Files
    server.py        FastAPI: console, JSON API, WhatsApp webhook
    live.py          The live desk: event-driven load lifecycle, owners, negotiation, re-plan
    geo.py           Nominatim + OSRM with a cache
    engine.py        Pricing, intake, truck types, the tool-calling orchestrator used by the tests
    static/          The console (HTML/CSS/JS, IBM Plex Sans)
    test_server.py   End-to-end test over fake WhatsApp traffic (no keys needed)
    test_engine.py   Pricing, matching and orchestrator tests
    smoke_test.py    Live check of the LLM orchestrator with your key
    app.py           Earlier Streamlit prototype (kept until the new console is deployed)
    render.yaml      One-click deploy on Render
