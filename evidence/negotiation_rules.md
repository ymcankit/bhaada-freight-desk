# How Bhaada negotiates with truck owners

Plain-language rules, taken from the code (`live.py`: `_econ`, `_model_read`, `_negotiate`, `_advance_ask`,
`_plausible_ask`, `_next_move`, `_lump`, `_money`, `_decide`, `_options`, `_strategize`, `_hold_reply`; `engine.py`:
`band`, `fair_cost`, `lane_cost`, `season`, `price_quote`; numbers from `data/rates.json`).

Revised on 29 Oct 2026 after an independent review from the Indian trucking angle (feasibility 6/10 on the first
version). What changed is marked **(new)**.

## 1. Who does what

- **A language model reads** the owner's message: a rate, a yes to our offer, a no, a question, or something else.
- **Code decides every number.** The model never sets, invents or accepts a rate. A number the owner clearly wrote
  with a unit ("21 hazaar", "84/km", "2.5L") beats the model's reading.
- The model may answer an owner's question only from the desk's own terms; an answer with any number not in those
  terms is thrown away.
- **(new)** "Haan, 95 mein" names a new rate, so it is read as a counter, never a yes. "Ok dekhte hain", "baad mein
  batata hoon" are not acceptances.

## 2. The price band (set before anyone is messaged)

1. **Shipper's market rate**: fixed charge + per-km rate fitted to published FR8 lane listings (32 ft MXL: ₹8,000 +
   ₹55/km). Still to be checked against real lane quotes (`evidence/lane_quotes.csv`).
2. **Owner's running cost**: diesel (km ÷ mileage × **₹97.8/L (new)**, Mumbai Oct 2026), tolls by axle count, driver
   + allowance + helper for **at least 2 days (new)** (loading and unloading waits), EMI/insurance/tyres/maintenance
   **₹16/km for an MXL (new)** (other types scaled the same way), ₹600 handling.
   - **(new)** No return-load adjustment. The per-lane "chance of a return load" was our guess, so it no longer moves
     prices either way (it used to take 3% off; the review's empty-return charge is dropped with it). To come back from
     real data: return loads actually booked per lane.
3. **Expected buy price** = the higher of (a) the market rate less the broker's target margin and (b) the running
   cost. Lane memory pulls (a) halfway to what owners really took. Goods premiums and festival seasons go on top.
4. **(new) Margin band per lane.** Short-haul FTL under 400 km: floor 5%, target 8%, ceiling 12%. Longer lanes and
   special goods (hazardous, perishable): the settings' band, 7 / 11 / 14% by default.
5. **Sell price** = expected buy ÷ (1 − target), **(new) but never more than 3% above the lane's market rate**. When
   a trip costs more than the market pays (e.g. a long Taurus run), the margin shrinks and the strategist asks the
   broker instead of quoting a price no shipper will accept. From it, per owner: **target buy rate** (at the target
   margin) and **ceiling** (at the floor: the most we ever pay).
6. **(new) The broker's own terms win.** Every load asks once for the shipper's price and the broker's margin ("25
   hazaar, 8% margin"; "aap batao" keeps the market pricing). With a shipper price: sell = that price, the most we
   pay = price × (1 − margin), and the desk opens 3 points lower. With only a margin: market pricing, never below that
   margin. If price × (1 − margin) is below the truck's running cost, the broker is warned at once with the margin
   that is realistic. The quote shows "margin X% (aapne Y% maanga)".
7. **(new) Festival seasons, by supply and destination:** pre-Diwali (29 Oct–5 Nov) +8%; Diwali week (6–14 Nov)
   +15% because drivers go home, except loads into Surat (its textile markets are shut until Labh Pancham); Chhath
   (15–18 Nov) +6%; financial year-end +5%; rabi harvest +4%.

## 3. The first message to an owner

Load, lane, km, day, weight, goods, truck, body rules, then **the agency's terms (new, all of them up front):**
advance % at loading (UPI/bank), balance within 7 days of POD, rate includes diesel and toll, 24 hours free then
₹1,500/day detention, hamali on the party, no commission from the owner, shortage/damage deducted only if written on
the POD. **(new)** It ends with the time the round closes ("12:40 tak").

## 4. Reading the ask

Understands ₹/km, totals, "21k", "21 hazaar", "1 lakh 20 hazaar", "2.5L", "saade 27 hazaar", "dedh", "dhai". Ignores
tons, feet, times, percentages, **"90 advance" (new)**, truck and phone numbers. A bare "26" that is far below
running cost but plausible as thousands is read as ₹26,000. "Toll alag" adds the per-km toll for the axles.

**(new) Advance is negotiable.** "90% advance chahiye" → the desk gives up to 90% (the agency's maximum) for ₹500
less per extra 10%, says so, and that owner's target and ceiling drop by the same amount.

## 5. The rules for each reply, in order

1. **Refuses** → thank them, close politely.
2. **Yes to our last offer** → deal at exactly that offer.
3. **(new) Ask below the truck's running cost** → confirm once (a typo, a misread or an owner in distress), never an
   instant accept. (Was: below 80% of cost.)
4. **No rate** → ask for it (total or ₹/km).
5. **Ask at or below target** → accept.
6. **(new) Reciprocity**: a "few paise apart" close (within ₹0.50/km of our counter, or ₹0.60/km / 2% of the
   target) happens only if the owner came down since our last offer. An owner who didn't move hears "Hamara offer
   wahi hai" and our offer stays where it was, so edging up a few paise doesn't pay.
7. **Cap** = ceiling, or the cheapest rate another owner has already agreed, whichever is lower. **(new)** A rival
   counts only if they are rated 3.5+ and their rate isn't below the truck's running cost (a sham lowball can't cap
   everyone). **(new)** The rival is never quoted to the owner: it only limits what we pay.
8. **Last round (3 max)**: accept if within the cap. **(new)** Otherwise an "aakhri offer" goes to about 60% of owners,
   landing 40–80% of the way from target to cap (steady per owner, not guessable), never always at the cap; the rest,
   and anyone who refuses it, get an honest decline ("isse upar humara margin nahi bachta").
9. **Otherwise counter on a concession ladder:** anchor = the owner's morning check-in rate if affordable, else target;
   round 1 offers the anchor, round 2 moves halfway to min(ask, cap), round 3 all the way; never move up by more than
   they came down (minimum ₹0.50/km).
   - **(new) ₹500 lump sums, rounded to the nearest ₹500** (up, once the owner has come down twice), never above
     the cap and never below our last offer. Was: always rounded down.
   - **(new) Owners hear totals only** on lanes under 500 km; ₹/km is added only on long lanes.
   - **(new) Round 1 cites the lane's market** ("Bhiwandi → Surat (250 km) pe 32 ft MXL owners ko aam taur pe
     ~₹20,000 milta hai"), recent lane rates and the short empty run. It no longer explains the
     owner's own diesel, EMI and driver costs to them.

## 6. Picking the winner

- A round closes when every owner has answered or the window ends. **(new)** The window is 5 minutes for a pickup
  today and **15 minutes for tomorrow or later** (owners reply from the road). When one owner accepts at or below
  target, the rest still get **5 minutes (new, was 45 seconds)**. Demo owners run on 30 s / 20 s.
- Only deals at or above the lane's floor count. Best margin wins, but **(new)** an owner rated ≥ 0.3 stars more
  reliable wins a margin gap under **2 points** (was 1): a no-show costs far more.

## 7. When nobody clears the floor (strategist)

Code lists the possible moves; a model picks one; code checks and runs it. Fixed order without a model: widen to
120 km → final offer to owners within 12% of the ceiling, **(new) at 70–100% of the way from target to ceiling, per
owner** → bigger truck → ask the broker → hand over.

## 8. After the deal

- Broker approves → owner holds. **(new) A firm booking:** the hold asks for HAAN *with* the vehicle number, the
  driver's mobile and the reporting time ("HAAN MH04AB1234, 98XXXXXXXX, subah 8 baje"). Sent together, the booking is
  firm and the dispatch card goes out at once; a bare "haan" is logged as tentative until the vehicle number comes.
- Shipper yes/no → dispatch → draft LR, e-way bill and GST lines.
- **Back-out:** reliability −0.4, re-confirm owners who already agreed, re-book at the shipper's fixed price.
  **(new)** The backed-out rate is dropped from lane memory.
- **(new) Lane memory** needs at least 5 booked deals in 30 days before it moves a price.

## 9. Required questions and the morning check-in

See `required_questions.md`: from, to, tons, goods, loading time and the shipper's price/margin before any owner is
messaged; owners give a base city when joining; every registered owner gets the morning check-in automatically at
7:00 IST (once a day) asking truck type, where it is and when it's free.

## 10. What the review raised that is not in code yet

- WhatsApp (built, waiting on Meta approval); Telegram is a barrier for many owners.
- Bengali and Tamil message templates for Kolkata and Chennai lanes; union-rate lanes where haggling doesn't apply.
- Seeing that an owner offered the same truck to other brokers (multi-homing).
- A daily diesel feed (today: the console's diesel field, updated by hand).
- TDS 194C(6) PAN declaration capture, extra loading/unloading points, part loads, cash advance at loading,
  Mumbai no-entry hours.
- Validating the market rate against 20+ real quotes per lane.
