"""Mechanical copy of the 0xa797 style: after an in-play price jump (likely a goal), buy the side that jumped.
  uv run python -I scripts/edge/jump_follow.py [n_markets] [kind]   kind: win | ou | all
Entry at the next fill D seconds after the jump is first seen; hold to resolution; Kalshi taker fee.
"""
import bisect, json, random, re, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from skilled_decay import fills

H = Path.home() / ".polywatch/history"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
KIND = sys.argv[2] if len(sys.argv) > 2 else "all"
RX = {"win": r"win on 20\d\d", "ou": r"O/U", "all": r"win on 20\d\d|O/U|end in a draw|Spread:"}[KIND]
CUT = datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()
ts_of = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
ms = []
for l in open(H / "markets.jsonl"):
    m = json.loads(l); q = m.get("question") or ""
    if re.search(RX, q) and m.get("game_start") and m.get("n_out") == 2 and m.get("prices") and max(m["prices"]) == 1.0:
        ms.append(m)
random.seed(7); ms = random.sample(ms, min(N, len(ms)))
with ThreadPoolExecutor(6) as ex:
    tapes = list(ex.map(lambda m: fills(m["cid"]), ms))
fee = lambda p: 0.07 * (1 - p)
JUMPS, WIN, DELAYS = (0.08, 0.12, 0.20), 60, (5, 15, 30, 60, 300)
res = defaultdict(list)
for m, (fts, p0) in zip(ms, tapes):
    gs = ts_of(m["game_start"])
    if not fts or not gs:
        continue
    last_t = -1e18
    for i, t in enumerate(fts):
        if t < gs or t - last_t < 600:  # in play only; one signal per 10 min per market
            continue
        j = bisect.bisect_left(fts, t - WIN)
        if j >= i:
            continue
        d = p0[i] - p0[j]
        for th in JUMPS:
            if abs(d) >= th:
                side = 0 if d > 0 else 1
                if not th == JUMPS[0]:
                    pass
                for D in DELAYS:
                    k = bisect.bisect_left(fts, t + D)
                    if k >= len(fts):
                        continue
                    px = p0[k] if side == 0 else 1 - p0[k]
                    if 0.03 < px < 0.97:
                        y = m["prices"][side]
                        res[(th, D)].append((m["cid"], t >= CUT, px, y, (y - px) / px - fee(px)))
        if abs(d) >= JUMPS[0]:
            last_t = t
print(f"{KIND}: {len(ms)} markets sampled\n")
random.seed(0)
for th in JUMPS:
    for D in DELAYS:
        for lab, keep in (("all", lambda x: True), ("after 10-15", lambda x: x[1])):
            v = [x for x in res[(th, D)] if keep(x)]
            if not v:
                continue
            by = defaultdict(list)
            for x in v:
                by[x[0]].append(x[4])
            g = list(by.values())
            bs = sorted(sum(sum(a) for a in s) / sum(len(a) for a in s) for s in (random.choices(g, k=len(g)) for _ in range(500)))
            print(f"jump >= {th:.2f} in 60s, enter +{D:>3}s {lab:<11} n {len(v):5d} win {sum(x[3] for x in v) / len(v):.3f} px {sum(x[2] for x in v) / len(v):.3f} roi {sum(x[4] for x in v) / len(v):+.3f} [{bs[25]:+.3f},{bs[475]:+.3f}]")
    print()
