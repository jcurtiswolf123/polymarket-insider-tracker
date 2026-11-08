"""Cross-venue game-winner gaps: Kalshi Yes on one team + Polymarket on the other, after Kalshi taker fee."""

import json, math, re, sys, urllib.request
from datetime import datetime


def get(u):
    return json.load(
        urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": "polywatch"}), timeout=30
        )
    )


fee = lambda p: math.ceil(round(0.07 * p * (1 - p) * 100, 6)) / 100
K = "https://api.elections.kalshi.com/trade-api/v2"
MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
SERIES = {"KXNFLGAME": "nfl", "KXNBAGAME": "nba", "KXNHLGAME": "nhl", "KXNCAAFGAME": "cfb"}
out = []
for ser, lg in SERIES.items():
    try:
        evs = get(f"{K}/events?series_ticker={ser}&status=open&with_nested_markets=true&limit=200")[
            "events"
        ]
    except Exception as e:
        print(ser, e)
        continue
    n = 0
    for e in evs:
        m = re.match(rf"{ser}-(\d\d)([A-Z]{{3}})(\d\d)([A-Z]+)$", e["event_ticker"])
        if not m:
            continue
        yy, mon, dd, teams = m.groups()
        legs = {mk["ticker"].rsplit("-", 1)[1]: mk for mk in e["markets"]}
        ab = [t for t in legs if teams.startswith(t)]
        if not ab:
            continue
        away = ab[0]
        home = teams[len(away) :]
        if home not in legs:
            continue
        slug = f"{lg}-{away.lower()}-{home.lower()}-20{yy}-{MON[mon]:02d}-{dd}"
        try:
            pm = get(f"https://gamma-api.polymarket.com/markets?slug={slug}")
        except Exception:
            continue
        if not pm or pm[0].get("closed"):
            continue
        p = pm[0]
        ba, bb = float(p.get("bestAsk") or 0), float(p.get("bestBid") or 0)
        poly_ask = {away: ba, home: 1 - bb}  # outcome 0 = away
        n += 1
        for a, b in ((away, home), (home, away)):
            ka = float(legs[a].get("yes_ask_dollars") or 0)
            pb = poly_ask[b]
            if 0 < ka < 1 and 0 < pb < 1:
                out.append(
                    (
                        round(1 - ka - fee(ka) - pb, 4),
                        f"Kalshi {a} @{ka:.2f} + Poly {b} @{pb:.3f}",
                        slug,
                    )
                )
    print(f"{lg}: {n} games matched")
out.sort(reverse=True)
print("best gaps (positive = locked profit per $1 pair):")
for r in out[:8]:
    print(f"  {r[0]:+.4f}  {r[1]}  {r[2]}")
print("pairs with positive gap:", sum(r[0] > 0 for r in out), "of", len(out))
