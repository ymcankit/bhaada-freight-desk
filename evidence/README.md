# Evidence kit

What the judges and any investor will ask first: "who did you talk to, and what did the product do with real loads?"
This folder is how the team collects that evidence before the Grand Finale and in the first pilot weeks.
Nothing here is filled in yet; record only what really happened, with dates.

| File | What it gives | Time |
|---|---|---|
| `interview_broker.md` | 15-minute call script for freight brokers / transporters | 5 calls × 20 min |
| `interview_owner.md` | 10-minute call script for truck owners | 5 calls × 15 min |
| `lane_quotes.csv` | Real market quotes for the demo lanes, to check the FR8-fitted rate card | 3 lanes × 3 quotes |
| `pilot_plan.md` | 3-week shadow → co-pilot plan with the KPIs the desk already measures | — |
| `loi_template.md` | One-page letter of intent for a pilot agency | — |

The desk measures its own evidence in the pilot:
- **Shadow accuracy:** a broker types `mera rate BH-1005 27500` after quoting the shipper themselves; the console's
  `/api/state` KPI `shadowWithin3Pct` is the share of Bhaada quotes within ±3% of the broker's own.
- **Inquiry → booking-ready time:** every load's events carry timestamps ("Booking-ready in m:ss").
- **Owner reply rate, holds honoured, back-outs recovered:** per-load threads and events.
