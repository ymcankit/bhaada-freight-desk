# Pilot plan: one agency, three weeks

**Week 1, shadow.** The broker forwards every inquiry to the bot as well as working it the usual way. After quoting the
shipper themselves they type `mera rate BH-xxxx <amount>`. Nothing is sent to owners without the broker's tap.
Measure: share of Bhaada quotes within ±3% of the broker's own (`shadowWithin3Pct`), intake errors per 100 loads.

**Week 2, co-pilot on 3 lanes.** The desk messages the agency's own owners (imported from the coordinator's phone),
negotiates inside the agency's margin band, and the broker approves every quote. AI owners and the AI broker are off
(`aiAgents: false`). Measure: inquiry → booking-ready time against the agency's week-1 baseline, owner reply rate,
holds honoured, back-outs recovered without a phone call, ₹ margin per load.

**Week 3, decision.** Proprietor review: keep, change or stop. Convert to paid (₹2,999/month including 50 loads,
then ₹60–100 per placed load) if: shadow accuracy ≥ 60% within ±3%, booking-ready time down ≥ 30%, owner reply rate
≥ 40%, and the coordinator uses it daily.

Kill criteria: owner reply rate under 30% after week 2, or the broker overrides more than half of the quotes.
