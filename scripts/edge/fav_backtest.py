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
WK = datetime(2026, 11, 3, 16, tzinfo=timezone.utc).timestamp()
def c1_keys():
    """Trades where live C1_fresh_cluster would fire: 3+ wallets <168h old, same side, 24h window, $5K+ combined."""
    ev = []
    for l in open(H / "rows.jsonl"):
        if '"sell": 0' not in l: continue
        r = json.loads(l)
        if r.get("age_h") is not None and r["age_h"] <= 168:
            ev.append((r["ts"], r["market"], r["outcome_index"], r["wallet"], float(r["usdc"] or 0), r.get("trade_id")))
    ev.sort(); buckets, fired = {}, set()
    for ts, m, o, w, u, tid in ev:
        b = buckets.setdefault((m, o), {})
        prev = b.get(w); b[w] = (ts, (prev[1] if prev else 0) + u)
        for k in [k for k, v in b.items() if ts - v[0] > 86400]: del b[k]
        if len(b) >= 3 and sum(v[1] for v in b.values()) >= 5000: fired.add((tid, w))
    return fired
C1 = c1_keys()
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
    if (r.get("trade_id"), r["wallet"]) in C1: rules.add("C1_fresh_cluster")  # history rows never computed C1
    live_like = not r.get("in_play") and not r.get("active")  # live alerts drop in-play games and very active traders
    vs = (("lane",) if rules else ()) + ("no_rule_gate",)
    vs += tuple(v + "+pregame_not_active" for v in vs) if live_like else ()
    if "C1_fresh_cluster" in rules: vs += ("c1",) + (("c1+pregame",) if not r.get("in_play") else ())
    for v in vs:
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
for v in ("lane", "lane+pregame_not_active", "no_rule_gate+pregame_not_active", "c1", "c1+pregame"):
    rs = [r for (m, o, vv), r in first.items() if vv == v]
    print(f"\n== {v} (first hit per market+side, $1, Kalshi fee, 90% CI) ==")
    stats(rs, "all")
    stats([r for r in rs if r["ts"] < CUT], "before 10-24")
    stats([r for r in rs if r["ts"] >= CUT], "after 10-24 (out of sample)")
    stats([r for r in rs if r["ts"] >= WK], "11-03 on (live week)")
    stats([r for r in rs if r["ts"] < WK], "before 11-03")
    sp = lambda r: bool(SPORT.search(f"{r.get('slug','')} {r.get('title','')}"))
    stats([r for r in rs if sp(r)], "sports/esports")
    stats([r for r in rs if not sp(r)], "other")
    for lo, hi in ((0.60, 0.70), (0.70, 0.78), (0.78, 0.85)):
        stats([r for r in rs if lo <= r["price"] < hi], f"price {lo:.2f}-{hi:.2f}")
    stats([r for r in rs if r["age_h"] < 24], "wallet < 24h")

print("\n== lane+pregame_not_active by week (Monday start) ==")
from collections import defaultdict
wk = defaultdict(list)
for (m, o, v), r in first.items():
    if v == "lane+pregame_not_active":
        wk[datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%G-W%V")].append(r)
for k in sorted(wk):
    rs = wk[k]; ys = [ms[r["market"]]["prices"][r["outcome_index"]] for r in rs]; px = [r["price"] for r in rs]
    v = [roi(y, p) for y, p in zip(ys, px)]
    print(f"{k} n {len(rs):3d} win {sum(ys)/len(ys):.2f} px {sum(px)/len(px):.2f} win-px {sum(ys)/len(ys)-sum(px)/len(px):+.2f} roi {sum(v)/len(v):+.3f}")

print("\n== hot-hand filter: bet only when recent resolved lane bets were profitable (known at bet time) ==")
bets = sorted((r for (m, o, v), r in first.items() if v == "lane+pregame_not_active"), key=lambda r: r["ts"])
for r in bets:
    r["_end"] = r["ts"] + r["hours_to_end"] * 3600
    r["_roi"] = roi(ms[r["market"]]["prices"][r["outcome_index"]], r["price"])
def run(label, recent):
    taken = []
    for r in bets:
        done = [b for b in bets if b["_end"] < r["ts"]]
        sig = recent(done, r["ts"])
        if sig is not None and sum(b["_roi"] for b in sig) / len(sig) > 0: taken.append(r["_roi"])
    random.seed(0)
    bs = sorted(sum(random.choices(taken, k=len(taken))) / len(taken) for _ in range(1000)) if taken else [0] * 1000
    print(f"{label:<40} bets {len(taken):4d} of {len(bets)} | roi {sum(taken)/max(len(taken),1):+.3f} [{bs[50]:+.3f},{bs[950]:+.3f}]")
print(f"{'bet every hit (baseline)':<40} bets {len(bets):4d} | roi {sum(b['_roi'] for b in bets)/len(bets):+.3f}")
for d in (3, 7, 14):
    run(f"trailing {d} days > 0", lambda done, ts, d=d: [b for b in done if b["_end"] >= ts - d * 86400] or None)
for n in (10, 20, 40):
    run(f"last {n} resolved > 0", lambda done, ts, n=n: sorted(done, key=lambda b: b["_end"])[-n:] if len(done) >= n else None)
