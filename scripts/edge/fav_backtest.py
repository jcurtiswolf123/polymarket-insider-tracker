"""Historical test of the favorites lane on the backfill rows: first lane hit per market+side, $1 at each entry."""
import json, sys, random, re
from pathlib import Path
from datetime import datetime, timezone
H = Path.home() / ".polywatch/history"
SPORT = re.compile(r"\b(vs\.?|nba|nfl|nhl|mlb|ufc|lol|cs2|counter-strike|dota|valorant|o/u|spread|win on 20)\b", re.I)
ms = {}
for l in open(H / "markets.jsonl"):
    m = json.loads(l); p = m.get("prices") or []
    if p and max(p) == 1.0: ms[m["cid"]] = m
CUT = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
first = {}  # (market, outcome, variant) -> row
for l in open(H / "rows.jsonl"):
    if '"sell": 0' not in l: continue
    r = json.loads(l)
    try:
        px, usdc, age, hte = float(r["price"]), float(r["usdc"]), r.get("age_h"), r.get("hours_to_end")
    except (TypeError, ValueError, KeyError): continue
    if age is None or hte is None or r["market"] not in ms: continue
    if not (0.60 <= px < 0.85 and usdc >= 1000 and age < 168 and 0 < hte <= 168): continue
    rules = {x.removeprefix("W_") for x in r.get("rules") or []} - {"M1_model_edge"}
    for v in (("lane",) if rules else ()) + ("no_rule_gate",):
        k = (r["market"], r["outcome_index"], v)
        if k not in first or r["ts"] < first[k]["ts"]: first[k] = r
fee = lambda p: 0.07 * (1 - p)
def roi(y, p): return (y - p) / p - fee(p)
def stats(rs, label):
    if not rs: print(f"{label:<28} n 0"); return
    out = [f"{label:<28} n {len(rs):4d}"]
    ys = [ms[r["market"]]["prices"][r["outcome_index"]] for r in rs]
    out.append(f"win {sum(ys)/len(ys):.3f} px {sum(r['price'] for r in rs)/len(rs):.3f}")
    for col in ("price", "px2", "px10", "px30"):
        pr = [(y, r[col]) for y, r in zip(ys, rs) if r.get(col) and 0.01 < r[col] < 0.97]
        v = [roi(y, p) for y, p in pr]
        if not v: continue
        bs = sorted(sum(random.choices(v, k=len(v))) / len(v) for _ in range(1000))
        out.append(f"{'fill' if col=='price' else '@'+col[2:]+'m'} {sum(v)/len(v):+.3f} [{bs[50]:+.3f},{bs[950]:+.3f}]")
    print(" | ".join(out))
random.seed(0)
for v in ("lane", "no_rule_gate"):
    rs = [r for (m, o, vv), r in first.items() if vv == v]
    print(f"\n== {v} (first hit per market+side, $1, Kalshi fee, 90% CI) ==")
    stats(rs, "all")
    stats([r for r in rs if r["ts"] < CUT], "before 10-24")
    stats([r for r in rs if r["ts"] >= CUT], "after 10-24 (out of sample)")
    sp = lambda r: bool(SPORT.search(f"{r.get('slug','')} {r.get('title','')}"))
    stats([r for r in rs if sp(r)], "sports/esports")
    stats([r for r in rs if not sp(r)], "other")
    for lo, hi in ((0.60, 0.70), (0.70, 0.78), (0.78, 0.85)):
        stats([r for r in rs if lo <= r["price"] < hi], f"price {lo:.2f}-{hi:.2f}")
    stats([r for r in rs if r["age_h"] < 24], "wallet < 24h")
