"""Kalshi version of goal_sync: after a game-market jump, buy cheap stale Over lines of the same match.
  uv run python -I scripts/edge/kalshi_goal_sync.py [events_per_league]
Kalshi public API only. Trades cached under ~/.polywatch/kalshi_tapes/. Entry = the first YES trade on an Over line
D seconds after the trigger (a real fill at that price), only if it is < 0.20 and within 3pt of the last trade
60s before. Fee 0.07 * p * (1 - p) per contract.
"""
import bisect, json, random, sys, time, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"
C = Path.home() / ".polywatch/kalshi_tapes"; C.mkdir(exist_ok=True)
LEAGUES = ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL", "MLS")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 80
CUT = datetime.fromisoformat("2026-10-15T00:00:00+00:00").timestamp()


def get(path):
    for i in range(6):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "pw"}), timeout=30))
        except Exception:
            time.sleep(1.5 * (i + 1))  # public API rate-limits: back off and retry
    return {}


def pages(path, key, limit=None):
    out, cur = [], ""
    while True:
        d = get(path + (f"&cursor={cur}" if cur else ""))
        out += d.get(key) or []
        cur = d.get("cursor")
        if not cur or (limit and len(out) >= limit):
            return out


def tape(ticker):
    f = C / f"{ticker}.json"
    if f.exists():
        return json.loads(f.read_text())
    tr = pages(f"/markets/trades?ticker={ticker}&limit=1000", "trades")
    t = sorted((datetime.fromisoformat(x["created_time"].replace("Z", "+00:00")).timestamp(), float(x["yes_price_dollars"]), x.get("taker_side")) for x in tr)
    f.write_text(json.dumps(t))
    return t


matches = []
for lg in LEAGUES:
    evs = pages(f"/events?series_ticker=KX{lg}GAME&status=settled&limit=200&with_nested_markets=true", "events", N)[:N]
    for e in evs:
        sfx = e["event_ticker"].split("-", 1)[1]
        tot = get(f"/markets?event_ticker=KX{lg}TOTAL-{sfx}&limit=50").get("markets") or []
        if tot and e.get("markets"):
            matches.append((lg, sfx, [m["ticker"] for m in e["markets"]], [(m["ticker"], m.get("result")) for m in tot]))
print(f"matches with game + total markets: {len(matches)}", {lg: sum(m[0] == lg for m in matches) for lg in LEAGUES})
need = [t for _, _, g, tot in matches for t in g + [x for x, _ in tot]]
with ThreadPoolExecutor(4) as ex:
    T = dict(zip(need, ex.map(tape, need)))
fee = lambda p: 0.07 * (1 - p)
res = defaultdict(list)
for lg, sfx, game, tot in matches:
    trig = []
    for g in game:
        tt = T[g]; ts = [x[0] for x in tt]
        for i, (t, p, _) in enumerate(tt):
            j = bisect.bisect_left(ts, t - 60)
            if j < i:
                trig.append((t, abs(p - tt[j][1])))
    trig.sort()
    for TH in (0.10, 0.15):
        last = -1e18
        for t, d in trig:
            if d < TH or t - last < 600:
                continue
            last = t
            for tk, result in tot:
                if result not in ("yes", "no"):
                    continue
                tt = T[tk]; ts = [x[0] for x in tt]
                for D in (5, 15, 30, 60):
                    k1 = bisect.bisect_right(ts, t + D - 60) - 1
                    k2 = next((k for k in range(bisect.bisect_left(ts, t + D), len(tt)) if tt[k][2] == "yes"), None)
                    if k2 is None or k1 < 0:
                        continue
                    px = tt[k2][1]
                    if 0.01 < px < 0.20 and abs(px - tt[k1][1]) < 0.03:
                        y = 1.0 if result == "yes" else 0.0
                        res[(TH, D)].append((f"{lg}-{sfx}", t >= CUT, px, y, (y - px) / px - fee(px), ts[k2] - t))
random.seed(0)
for TH in (0.10, 0.15):
    for D in (5, 15, 30, 60):
        for lab, keep in (("all", lambda x: True), ("after 10-15", lambda x: x[1])):
            v = [x for x in res[(TH, D)] if keep(x)]
            if len(v) < 10:
                print(f"trigger >= {TH:.2f} +{D:>2}s {lab:<11} n {len(v)}"); continue
            by = defaultdict(list)
            for x in v:
                by[x[0]].append(x[4])
            g = list(by.values())
            bs = sorted(sum(sum(a) for a in s) / sum(len(a) for a in s) for s in (random.choices(g, k=len(g)) for _ in range(1000)))
            gaps = sorted(x[5] for x in v)
            print(f"trigger >= {TH:.2f} +{D:>2}s {lab:<11} n {len(v):4d} matches {len(g):3d} win {sum(x[3] for x in v) / len(v):.3f} px {sum(x[2] for x in v) / len(v):.3f} roi {sum(x[4] for x in v) / len(v):+.3f} [{bs[50]:+.3f},{bs[950]:+.3f}] fill gap med {gaps[len(gaps) // 2]:.0f}s")
