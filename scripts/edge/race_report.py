"""Who saw each goal first? Reads ~/.polywatch/race/*.jsonl from live_race.py.
  uv run python -I scripts/edge/race_report.py
Goal = ESPN score change. For each, the first Polymarket quote/trade and first Kalshi trade in that match whose
price moved >= 8pt vs 30s earlier, within -60..+120s of ESPN. Matching is by team name words in titles/tickers.
"""
import bisect, glob, json, re
from collections import defaultdict
from pathlib import Path
from statistics import median

rows = [json.loads(l) for f in sorted(glob.glob(str(Path.home() / ".polywatch/race/*.jsonl"))) for l in open(f)]
goals = [r for r in rows if r["src"] == "espn"]
pm = defaultdict(list); ka = defaultdict(list)
for r in rows:
    if r["src"] == "pm_trade":
        pm[(r["cid"], r["oi"])].append((r["t"], r["p"], r["q"] + " " + r["slug"]))
    elif r["src"] == "pm_quote" and r["bid"] > 0 and r["ask"] > 0 and r["ask"] - r["bid"] < 0.1:
        pm[(r["cid"], r["oi"])].append((r["t"], (r["bid"] + r["ask"]) / 2, r["q"] + " " + r["slug"]))
    elif r["src"] == "kalshi":
        ka[r["ticker"]].append((r["t"], r["p"], r["ticker"]))


def onsets(series):
    series.sort(); ts = [x[0] for x in series]; out = []
    for i, (t, p, lab) in enumerate(series):
        j = bisect.bisect_right(ts, t - 30) - 1
        if j >= 0 and abs(p - series[j][1]) >= 0.08:
            out.append((t, lab))
    return out


PM = [o for s in pm.values() for o in onsets(s)]; KA = [o for s in ka.values() for o in onsets(s)]
words = lambda s: {w for w in re.findall(r"[a-z]{4,}", s.lower())} - {"will", "game", "match", "draw", "more", "markets"}
print(f"goals seen by ESPN: {len(goals)}")
res = []
for g in goals:
    w = words(g["name"] or "")
    near = lambda src: min((t for t, lab in src if g["rt"] - 60 <= t <= g["rt"] + 120 and w & words(lab)), default=None)
    p, k = near(PM), near(KA)
    res.append((g["name"], g["rt"], p, k))
    print(f"{g['name'][:40]:40} espn 0s | polymarket {'-' if p is None else f'{p - g['rt']:+.1f}s'} | kalshi {'-' if k is None else f'{k - g['rt']:+.1f}s'}")
for lab, i in (("polymarket", 2), ("kalshi", 3)):
    v = [r[i] - r[1] for r in res if r[i] is not None]
    if v:
        print(f"{lab}: moved a median {median(v):+.1f}s vs ESPN ({len(v)} goals)")
both = [r[3] - r[2] for r in res if r[2] is not None and r[3] is not None]
if both:
    print(f"kalshi minus polymarket: median {median(both):+.1f}s over {len(both)} goals")
