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

**Status 2026-11-09 night**
- touch: FAIL. Kalshi train ROI -4% to -44% every cell; test 5 bets. No edge. Polymarket data-only, -7% train
- nowcast: FAIL. holdout -26% on 133 bets; TSA +30% on 15 (too few); gas history only 68 days. Daily scan 08:30 (com.josh.polywatch-nowcast) grows history; re-test ~Feb
- weather v1 (day-old forecasts): FAIL, Brier 0.116 vs market 0.096, ROI about -15%. Data verified (CLI = Kalshi result 120/120, model calibrated)
- weather v2 (run public at bet time, 6 models incl NBM/HRRR, inverse-variance blend): FAIL. 6 cities: Brier eve 0.116 vs market 0.096, morn 0.108 vs 0.089; ROI -14% to -20%. Lane killed; market prices beat public NWP
- research desk: live 06:15 (com.josh.polywatch-research), 20/day, ~$0.37 each
- hormuz: live 07:05 (com.josh.polywatch-hormuz); aisstream key works but Gulf near-blind; paid AIS not worth it on legal venues
