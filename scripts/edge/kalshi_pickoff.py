"""How much money sits at the old price right after a goal on Kalshi soccer game markets?
  uv run python -I scripts/edge/kalshi_pickoff.py [n_matches]
Per goal-like jump (>= 8pt vs 30s earlier): contracts bought on the side that then rose, at <= the pre-goal price
+2c, from 20s before to 3s after the jump's first trade. Value = contracts x (price 60s later - fill) - fee.
That is the profit a feed earlier than everyone else could have taken; it is a ceiling, not a forecast.
"""
import bisect, json, random, sys, time, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"
C = Path.home() / ".polywatch/kalshi_tapes_sized"; C.mkdir(exist_ok=True)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100


def get(path):
    for i in range(6):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "pw"}), timeout=30))
        except Exception:
            time.sleep(1.5 * (i + 1))
    return {}


def tape(tk):
    f = C / f"{tk}.json"
    if f.exists():
        return json.loads(f.read_text())
    out, cur = [], ""
    while True:
        d = get(f"/markets/trades?ticker={tk}&limit=1000" + (f"&cursor={cur}" if cur else ""))
        out += d.get("trades") or []; cur = d.get("cursor")
        if not cur:
            break
    t = sorted((datetime.fromisoformat(x["created_time"].replace("Z", "+00:00")).timestamp(), float(x["yes_price_dollars"]),
                x.get("taker_side"), float(x.get("count_fp") or 0)) for x in out)
    f.write_text(json.dumps(t)); return t


games = sorted({p.stem for p in (Path.home() / ".polywatch/kalshi_tapes").glob("KX*GAME-*.json")})
ev = defaultdict(list)
for g in games:
    ev[g.rsplit("-", 1)[0]].append(g)
random.seed(5); keys = random.sample(sorted(ev), min(N, len(ev)))
tks = [t for k in keys for t in ev[k]]
with ThreadPoolExecutor(4) as ex:
    T = dict(zip(tks, ex.map(tape, tks)))
fee = lambda p: 0.07 * p * (1 - p)  # per contract
per_match, goals, contracts = defaultdict(float), 0, 0.0
for k in keys:
    for tk in ev[k]:
        tt = T[tk]; ts = [x[0] for x in tt]; last = -1e18
        for i, (t, p, side, n) in enumerate(tt):
            j = bisect.bisect_right(ts, t - 30) - 1
            if j < 0 or abs(p - tt[j][1]) < 0.08 or t - last < 120:
                continue
            last = t; goals += 1
            up = p > tt[j][1]; pre = tt[j][1]
            a = bisect.bisect_left(ts, t + 60)
            if a >= len(tt):
                continue
            post = tt[a][1]
            for x in tt[bisect.bisect_left(ts, t - 20): bisect.bisect_right(ts, t + 3)]:
                # yes side rose: cheap YES buys; no side rose: cheap NO buys (price of yes stays high)
                if up and x[2] == "yes" and x[1] <= pre + 0.02:
                    per_match[k] += x[3] * (post - x[1] - fee(x[1])); contracts += x[3]
                elif not up and x[2] == "no" and (1 - x[1]) <= (1 - pre) + 0.02:
                    per_match[k] += x[3] * ((1 - post) - (1 - x[1]) - fee(x[1])); contracts += x[3]
v = sorted(per_match.get(k, 0.0) for k in keys)
print(f"matches {len(keys)}, goal-like jumps {goals // 1}, stale contracts taken {contracts:,.0f}")
print(f"value taken at the old price per match: mean ${sum(v) / len(v):,.0f}, median ${v[len(v) // 2]:,.0f}, p90 ${v[int(len(v) * .9)]:,.0f}, total ${sum(v):,.0f}")
