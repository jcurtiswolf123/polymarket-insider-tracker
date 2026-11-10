**Edge builds, started 2026-11-09**

**Rules**
- paper only; no live orders anywhere
- trade venues: Kalshi; Polymarket US only after Joshua's KYC; Polymarket main = data only (CA geoblock)
- gate for every lane: ROI > +5% after Kalshi taker fee on 200+ out-of-sample bets
- no outreach to people; public data only

**Lanes**
- weather: `scripts/edge/weather.py` (Kalshi KXHIGH*, Open-Meteo past forecasts, NWS settle)
- research desk: `scripts/edge/research_desk.py` (claude -p, 20 markets/day cap, blind to price)
- touch ladders: `scripts/edge/touch.py` (Kalshi + Polymarket "hit X" vs options-implied)
- nowcast: `scripts/edge/nowcast.py` (Kalshi weekly TSA, AAA gas)
- hormuz: `scripts/edge/hormuz.py` (PortWatch vs live AIS; alert only)
- surveys: later, needs approval per spend

**Status**
- see per-lane results below as they land
