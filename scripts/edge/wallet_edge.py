"""Hunt for insiders and skilled wallets in the history backfill, scored only on what was known at bet time.
  uv run python -I scripts/edge/wallet_edge.py
Data: ~/.polywatch/history rows.jsonl (BUYs >= $500, price <= 0.93, markets >= $10K volume, 2026-08-01 on).
Returns per $1 after a Kalshi taker fee, at the wallet's fill and at the price 2/10 min later (what a copier gets).
"""
import heapq, json, math, random, re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

H = Path.home() / ".polywatch/history"
ESP = re.compile(r"counter-strike|lol:|dota|valorant|cs2|esports|overwatch|rainbow", re.I)
SPT = re.compile(r"\bvs\.?\b|o/u|spread|handicap|win on 20|nba|nfl|nhl|mlb|ufc|atp|wta", re.I)
CUT = datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()  # tests also report trades after this date alone
fee = lambda p: 0.07 * (1 - p)
roi = lambda y, p: (y - p) / p - fee(p)


def ts_of(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


ms = {}
for l in open(H / "markets.jsonl"):
    m = json.loads(l); p = m.get("prices") or []
    if p and max(p) == 1.0 and (e := ts_of(m.get("end"))):
        ms[m["cid"]] = (p, e)

bets = {}  # first BUY per wallet+market+side
for l in open(H / "rows.jsonl"):
    if '"sell": 0' not in l:
        continue
    r = json.loads(l)
    if r["market"] not in ms:
        continue
    k = (r["wallet"], r["market"], r["outcome_index"])
    if k in bets and bets[k]["ts"] <= r["ts"]:
        continue
    prices, end = ms[r["market"]]
    t = f"{r.get('slug') or ''} {r.get('title') or ''}"
    bets[k] = dict(oi=r["outcome_index"], w=r["wallet"], m=r["market"], ts=r["ts"], end=end, p=float(r["price"]), y=prices[r["outcome_index"]],
                   usdc=float(r["usdc"] or 0), pm=r.get("prior_markets"), age=r.get("age_h"), inplay=r.get("in_play"),
                   px2=r.get("px2"), px10=r.get("px10"), ev=r.get("event") or r["market"],
                   cat="esports" if ESP.search(t) else "sports" if SPT.search(t) else "other")
B = sorted(bets.values(), key=lambda b: b["ts"])
print(f"bets {len(B):,} (first buy per wallet+market+side), wallets {len({b['w'] for b in B}):,}\n")


def report(label, sel):
    if not sel:
        print(f"{label:<52} n 0"); return
    out = [f"{label:<52} n {len(sel):6,d} mkts {len({b['m'] for b in sel}):5,d}",
           f"win {sum(b['y'] for b in sel) / len(sel):.3f} px {sum(b['p'] for b in sel) / len(sel):.3f}"]
    random.seed(0)
    for col in ("p", "px2", "px10"):
        v = [roi(b["y"], b[col]) for b in sel if b.get(col) and 0.02 < b[col] < 0.97]
        if len(v) < 5:
            continue
        # bootstrap by market so many wallets on one market don't fake precision
        by = defaultdict(list)
        for b in sel:
            if b.get(col) and 0.02 < b[col] < 0.97:
                by[b["m"]].append(roi(b["y"], b[col]))
        g = list(by.values())
        bs = sorted(sum(sum(x) for x in s) / sum(len(x) for x in s) for s in (random.choices(g, k=len(g)) for _ in range(500)))
        out.append(f"{'fill' if col == 'p' else '@' + col[2:] + 'm'} {sum(v) / len(v):+.3f} [{bs[25]:+.3f},{bs[475]:+.3f}]")
    print(" | ".join(out))


def both(label, f):
    sel = [b for b in B if f(b)]
    report(label, sel)
    report("   after 10-15 only", [b for b in sel if b["ts"] >= CUT])


print("== baseline ==")
both("all bets", lambda b: True)
both("pre-game, price 0.20-0.85", lambda b: not b["inplay"] and 0.2 <= b["p"] <= 0.85)

print("\n== 1. true-insider shape: almost no history, big bet, pre-game ==")
for pm in (0, 2, 4):
    for u in (5000, 20000):
        both(f"prior markets <= {pm}, ${u:,}+, pre-game", lambda b, pm=pm, u=u: b["pm"] is not None and b["pm"] <= pm and b["usdc"] >= u and not b["inplay"])
both("prior <= 2, $5K+, pre-game, niche (cat other)", lambda b: b["pm"] is not None and b["pm"] <= 2 and b["usdc"] >= 5000 and not b["inplay"] and b["cat"] == "other")
both("prior <= 2, $5K+, pre-game, price 0.20-0.60", lambda b: b["pm"] is not None and b["pm"] <= 2 and b["usdc"] >= 5000 and not b["inplay"] and 0.2 <= b["p"] < 0.6)

print("\n== 2. skilled wallets: score from markets already resolved at bet time, then follow ==")
# walk bets in time order; settle a wallet's earlier bets once their market end has passed
st = defaultdict(lambda: [0, 0.0, 0.0])  # wallet -> [n settled, sum(y - p), sum p(1-p)]
pend, follow = [], defaultdict(list)
for b in B:
    while pend and pend[0][0] <= b["ts"]:
        _, _, o = heapq.heappop(pend)
        s = st[o["w"]]; s[0] += 1; s[1] += o["y"] - o["p"]; s[2] += o["p"] * (1 - o["p"])
    s = st[b["w"]]
    if s[0] >= 20 and s[2] > 0:
        z = s[1] / math.sqrt(s[2])
        for zmin in (2.0, 3.0, 4.0):
            if z >= zmin:
                follow[zmin].append(b)
        if z <= -3.0:
            follow["fade"].append(b)
    heapq.heappush(pend, (b["end"], id(b), b))
for zmin in (2.0, 3.0, 4.0):
    sel = follow[zmin]
    report(f"follow wallets with z >= {zmin} on 20+ settled", sel)
    report("   after 10-15 only", [b for b in sel if b["ts"] >= CUT])
    report("   pre-game, price 0.20-0.85", [b for b in sel if not b["inplay"] and 0.2 <= b["p"] <= 0.85])
report("wallets with z <= -3 (losers), their bets", follow["fade"])

print("\n== 3. top wallets by z at the end (in-sample, for names only) ==")
for w, s in sorted(st.items(), key=lambda kv: -(kv[1][1] / math.sqrt(kv[1][2]) if kv[1][2] else 0))[:15]:
    if s[0] >= 20:
        mine = [b for b in B if b["w"] == w]
        cats = defaultdict(int)
        for b in mine:
            cats[b["cat"]] += 1
        print(f"{w}  settled {s[0]:4d}  win-px {s[1] / s[0]:+.3f}  z {s[1] / math.sqrt(s[2]):5.1f}  avg ${sum(b['usdc'] for b in mine) / len(mine):,.0f}  {dict(cats)}")

print("\n== 4. what the z >= 4 follow bets are ==")
sel = follow[4.0]
for lab, f in (("in play", lambda b: b["inplay"]), ("pre-game", lambda b: not b["inplay"])):
    report(lab, [b for b in sel if f(b)])
for c in ("sports", "esports", "other"):
    report(f"cat {c}", [b for b in sel if b["cat"] == c])
titles = defaultdict(int)
for l in open(H / "rows.jsonl"):
    if any(w in l for w in ("0xa797d6bd8f", "0x6d57da09ef", "0xc8b9a30184", "0x1f9e15f39b")):
        r = json.loads(l)
        titles[(r["wallet"][:10], re.sub(r"\d+", "#", (r.get("title") or "")[:40]))] += 1
for (w, t), n in sorted(titles.items(), key=lambda kv: -kv[1])[:16]:
    print(f"{w} {n:4d}  {t}")

json.dump({str(k): [{x: b[x] for x in ("w", "m", "ts", "p", "y", "usdc", "inplay", "cat")} | {"oi": b["oi"]} for b in v]
           for k, v in follow.items() if k in (3.0, 4.0)}, open(H / "skilled_follow.json", "w"))
sk = {w: {"settled": s[0], "win_px": round(s[1] / s[0], 3), "z": round(s[1] / math.sqrt(s[2]), 2)}
      for w, s in st.items() if s[0] >= 20 and s[2] > 0 and s[1] / math.sqrt(s[2]) >= 4.0}
json.dump(sk, open(H / "skilled_wallets.json", "w"), indent=1)
print(f"\nwrote {len(sk)} wallets with z >= 4 to {H / 'skilled_wallets.json'}")
