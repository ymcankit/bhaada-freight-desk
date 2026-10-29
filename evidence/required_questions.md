# The questions every load and every owner must answer

Bhaada won't message owners about a load, or put an owner's truck in the pool, until these are answered. The list
comes from two truck-owner interviews in Bhiwandi (October 2026, see below). One owner named what they need before
accepting a booking: *pickup and delivery location, the goods' weight, the truck type, loading time and payment
terms*. The other said most of their time with brokers goes on confirming load details, pickup timing and the rate.

## Broker: before owners are messaged

| # | Question (as the bot asks it) | Field | Required? | If not given |
|---|---|---|---|---|
| 1 | Kahan se load uthana hai? | `origin` | Yes | Asked |
| 2 | Kahan tak jaana hai? | `destination` | Yes | Asked; a new place name is confirmed on a map |
| 3 | Kitna ton hai? | `weight_ton` | Yes | Asked |
| 4 | Kya maal hai? (kapda, steel, chemical, dawai, carton, anaaj…) | `material` | Yes (unless the broker names the truck) | Asked; decides body, hazmat and premium |
| 5 | Ye chemical khatarnaak hai ya normal? | `hazardous` | Only for chemicals | Asked |
| 6 | Loading kab aur kis time? (kal subah 9 baje) | `pickup_when` | Yes | Asked (was a silent "kal" before) |
| 7 | Shipper kitna de raha hai, aur aapko kitna margin chahiye? | `budget_rs`, `margin_pct` / `margin_rs` | Asked once | "Aap batao" → priced from the market at the agency's default band |
| – | Kaunsi gaadi? | `truck_type` | No | Advised from weight and goods |
| – | Maal ki keemat (invoice value) | `invoice_value_rs` | No | Decides the e-way bill line |

**What the broker's price and margin do:** with a shipper price, the most the desk pays an owner is
price × (1 − margin) and it opens about 3 points lower. If that ceiling is below the truck's running cost, the broker
is warned at once with the margin that is realistic. With only a margin, the desk prices from the market and won't
book below that margin. The broker's quote shows "margin X% (aapne Y% maanga)".

## Truck owner

**When joining** (Telegram or WhatsApp): name or company, and **base city** (asked if missing: it decides which loads
reach them).

**Every morning at 7:00** (automatic, once a day, to every registered owner), one line per free truck:

| Field | Required? | If not given |
|---|---|---|
| Truck type | Yes | Asked again with the list of types |
| Where it is now | Yes | Their base city |
| Free from (aaj / kal / parso) | Yes | Today |
| Rate (₹/km or total) | No | Used as the opening anchor if affordable |

**On a load offer**, the first message carries everything the owner said they need: lane and km, pickup day and
time, weight, goods, truck and body, and the agency's payment terms (advance at loading, balance days after POD,
detention, hamali, no commission, deductions only if written on the POD). The owner answers with: free or not, and
their rate.

**To hold a booking:** HAAN, vehicle number, driver's mobile, reporting time.

## The interviews (primary research)

1. **Bhawana Logistics, Bhiwandi**, truck owner. *Topic: how owners get loads and coordinate with brokers.* Loads
   come mostly through brokers plus some old customers, by phone and WhatsApp. Before taking a load they check which
   truck is free and where the driver is, then price by route and goods. Trucks often stand empty, and a load one way
   often has no return load. Most time with brokers goes on confirming load details, pickup timing and the rate, often
   calling repeatedly for the same information. One place with genuine, confirmed load details would help "if details
   are correct and time isn't wasted".
2. **J K Transport & Logistics Pvt. Ltd., Bhiwandi**, truck owner. *Topic: rate negotiation and booking
   uncertainty.* Rates depend on route, truck type, diesel cost and demand. The broker names a rate, the owner names
   theirs, then they negotiate. After booking, problems include loading delays, goods not ready and last-minute
   cancellations, which waste truck and driver time and lose other loads. Before accepting, they need pickup and
   delivery location, weight, truck type, loading time and payment terms. A tool would help "if information is
   accurate and there's a clear update when the booking is confirmed".
