# Bhaada: Agentic Freight Desk (Abhikalp 2026, Case 9)

A freight broker's desk that takes a load from WhatsApp to a booking-ready truck: it reads the request, finds the road
distance, messages fleet owners on WhatsApp in parallel, negotiates inside the broker's margin band, asks for one
approval, gets the owner to hold, and re-books on its own when an owner backs out. Prices come only from code.

## Run
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml     # add GROQ_API_KEY, optionally OPENAI_API_KEY and TELEGRAM_BOT_TOKEN
    uvicorn server:app --port 8000                                   # open http://localhost:8000

**On a public host** set `CONSOLE_PASSWORD`: without it the console on Render is view-only (`CONSOLE_OPEN=1` opens
it to anyone). `PUBLIC_VIEW=1` lets anyone watch while only the password approves or changes settings.

**Live owners** works loads against real people on WhatsApp (setup in WHATSAPP.md) or Telegram: make a bot with @BotFather and set `TELEGRAM_BOT_TOKEN`; the server registers its own webhook.
**Sandbox owners** uses persona owners from `data/owners.json` so anyone can try the desk; the console labels it.

## What is real and what is demo
| Part | Status |
|---|---|
| Channel | **Telegram bot live** (@Bhaada_Desk_bot). WhatsApp Cloud API integrated and signature-checked, waiting on Meta number onboarding |
| Load intake (text, Hinglish, Hindi voice notes) | LLM chain OpenAI gpt-5.4-mini → Groq gpt-oss-120b → 20b → rules; every field validated in code; new places confirmed by the broker |
| Goods and truck choice | Material rules (body, hazmat, volume, premium) and a truck adviser in code (`engine.truck_fit`) |
| Cities and road distance | OpenStreetMap Nominatim/Photon + OSRM (car profile), cached |
| Price | `engine.price_quote` only: market rate fitted to FR8 published lane rates, owner running-cost floor (fuel, toll, driver and helper, EMI/maintenance estimate), lane memory from booked rates |
| Negotiation | The LLM reads the owner; the code plans every move (concession ladder, ceiling, rival offers) and explains it with the trip's costs |
| Recovery | AI strategist picks among moves the code lists and checks (widen, bigger truck, final offer, ask the broker, hand over) |
| After HAAN | Payment terms, e-way bill and GST lines, vehicle and driver capture with a format check. **VAHAN, KYC, payments, POD: not built (VAHAN marked demo)** |
| AI owners and AI broker | **Demo personas**, labelled "(AI owner)" / "Demo load"; their rates come from the same cost model, so they show the workflow, not market outcomes. Turn off with the `aiAgents` setting |
| Memory | Upstash Redis (free tier), local file fallback |

## Files
    server.py        FastAPI: console, JSON API, WhatsApp webhook
    live.py          The live desk: event-driven load lifecycle, owners, negotiation, re-plan
    geo.py           Nominatim + OSRM with a cache
    engine.py        Pricing, intake, truck types, the tool-calling orchestrator used by the tests
    static/          The console (HTML/CSS/JS, IBM Plex Sans)
    test_server.py   End-to-end test over fake WhatsApp traffic (no keys needed)
    test_engine.py   Pricing, matching and orchestrator tests
    smoke_test.py    Live check of the LLM orchestrator with your key
    test_telegram.py End-to-end test over fake Telegram traffic
    test_agents.py   AI owners/broker, timers, strategist, two-load routing
    app.py           Earlier Streamlit prototype (not deployed)
    render.yaml      One-click deploy on Render
