"""After a goal-like jump in one market of a soccer match, buy Over on the match's O/U lines that have not moved yet.
  uv run python -I scripts/edge/goal_sync.py [n_events]
Trigger: a team-win or draw market's price moves >= TH within 60s, in play. Entry: next O/U fill D seconds after,
only if that Over price moved < 3pt over the 60s before entry (stale). Over is outcome 0. Kalshi taker fee.
"""
import bisect, json, random, re, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from skilled_decay import fills

H = Path.home() / ".polywatch/history"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
CUT = datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()
ts_of = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00").replace("+00", "+00:00") if s.endswith("+00") else s.replace("Z", "+00:00")).timestamp()
ev = defaultdict(list)
for l in open(H / "markets.jsonl"):
    m = json.loads(l)
    if m.get("event") and m.get("game_start") and m.get("n_out") == 2 and m.get("prices") and max(m["prices"]) == 1.0:
        ev[m["event"].removesuffix("-more-markets")].append(m)
is_ou = lambda m: "O/U" in (m.get("question") or "")
is_lead = lambda m: re.search(r"win on 20\d\d|end in a draw", m.get("question") or "")
keys = [k for k, v in ev.items() if any(map(is_ou, v)) and any(is_lead(m) for m in v)]
random.seed(3); keys = random.sample(keys, min(N, len(keys)))
need = [m for k in keys for m in ev[k] if is_ou(m) or is_lead(m)]
with ThreadPoolExecutor(6) as ex:
    T = dict(zip([m["cid"] for m in need], ex.map(lambda m: fills(m["cid"]), need)))
fee = lambda p: 0.07 * (1 - p)
res = defaultdict(list)
for k in keys:
    ms = ev[k]; gs = ts_of(ms[0]["game_start"])
    trig = []
    for m in filter(is_lead, ms):
        fts, p0 = T[m["cid"]]
        for i, t in enumerate(fts):
            j = bisect.bisect_left(fts, t - 60)
            if t >= gs and j < i:
                trig.append((t, abs(p0[i] - p0[j])))
    trig.sort()
    for TH in (0.10, 0.15, 0.25):
        last = -1e18
        for t, d in trig:
            if d < TH or t - last < 600:
                continue
            last = t
            for m in filter(is_ou, ms):
                fts, p0 = T[m["cid"]]
                for D in (5, 15, 30, 60):
                    k2 = bisect.bisect_left(fts, t + D)
                    k1 = bisect.bisect_right(fts, t + D - 60) - 1
                    if k2 >= len(fts) or k1 < 0:
                        continue
                    px, before = p0[k2], p0[k1]
                    stale = abs(px - before) < 0.03
                    if 0.03 < px < 0.97:
                        y = m["prices"][0]
                        res[(TH, D, stale, "cheap" if px < 0.2 else "mid")].append((m["cid"], t >= CUT, px, y, (y - px) / px - fee(px), fts[k2] - t, k, m["cid"], t))
print(f"{len(keys)} matches, {len(need)} markets\n")
random.seed(0)
for TH in (0.10, 0.15, 0.25):
    for D in (5, 15, 30, 60):
        for stale in (True, False):
          for band in ("cheap", "mid"):
            for lab, keep in (("all", lambda x: True), ("after 10-15", lambda x: x[1])):
                v = [x for x in res[(TH, D, stale, band)] if keep(x)]
                if len(v) < 10:
                    continue
                by = defaultdict(list)
                for x in v:
                    by[x[6] if len(x) > 6 else x[0]].append(x[4])  # cluster by match: one goal moves every line
                g = list(by.values())
                bs = sorted(sum(sum(a) for a in s) / sum(len(a) for a in s) for s in (random.choices(g, k=len(g)) for _ in range(500)))
                print(f"trigger >= {TH:.2f}, Over +{D:>2}s {'stale' if stale else 'moved':5} {'px<.20' if band == 'cheap' else 'px>=.20':7} {lab:<11} n {len(v):5d} win {sum(x[3] for x in v) / len(v):.3f} px {sum(x[2] for x in v) / len(v):.3f} roi {sum(x[4] for x in v) / len(v):+.3f} [{bs[25]:+.3f},{bs[475]:+.3f}]")
    print()

from statistics import median
v = res[(0.15, 15, True, "cheap")]
print("stale cheap Over, trigger .15, +15s: entry fill seconds after trigger: median", median(x[5] for x in v),
      "p75", sorted(x[5] for x in v)[int(len(v) * .75)], "| matches", len({x[6] for x in v}))
for x in v[:12]:
    print(f"   +{x[5]:>5}s px {x[2]:.3f} {'WON' if x[3] else 'lost'} {x[6]}")

json.dump([{"cid": x[7], "trigger": x[8], "px": x[2], "won": x[3]} for x in res[(0.15, 15, True, "cheap")]], open(H / "stale_over_entries.json", "w"))
