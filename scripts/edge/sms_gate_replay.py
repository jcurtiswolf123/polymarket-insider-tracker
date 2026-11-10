"""Replay live SMS gates over every logged rule hit (rule_hits.jsonl), settle via Gamma, $1 per first hit per market+side."""
import json, re, random, urllib.request, sys
from pathlib import Path
P = Path.home() / ".polywatch"
hits = [json.loads(l) for l in open(P / "rule_hits.jsonl")]
for h in hits:
    for k in ("price", "usdc", "ml_edge"):
        if h.get(k) is not None: h[k] = float(h[k])
cids = sorted({h["market"] for h in hits})
res = {}
for i in range(0, len(cids), 40):
    for closed in ("true", "false"):
        q = "&".join(f"condition_ids={c}" for c in cids[i:i + 40])
        req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets?{q}&closed={closed}&limit=100", headers={"User-Agent": "pw"})
        for m in json.load(urllib.request.urlopen(req, timeout=30)):
            res[m["conditionId"]] = m
def won(h):
    m = res.get(h["market"])
    if not m or not m.get("closed"): return None
    outs, px = json.loads(m["outcomes"]), [float(x) for x in json.loads(m["outcomePrices"])]
    if max(px) < 0.99 or h.get("outcome") not in outs: return None
    return px[outs.index(h["outcome"])] > 0.5
ESP = re.compile(r"counter-strike|lol:|dota|valorant|cs2|esports|overwatch|rainbow", re.I)
SPT = re.compile(r"\bvs\.?\b|o/u|spread|handicap|win on 20|nba|nfl|nhl|ufc", re.I)
cat = lambda h: "esports" if ESP.search(h.get("title") or "") else "sports" if SPT.search(h.get("title") or "") else "other"
STRONG = {"R1_fresh_big_bet", "R5_whale_thin_history", "R6_whale_longshot", "R7_cluster", "R8_pre_resolution", "M1_model_edge"}
rules = lambda h: {r.removeprefix("W_") for r in h.get("rules") or []}
def book(label, keep):
    first = {}
    for h in hits:
        try:
            if not keep(h): continue
        except (TypeError, ValueError): continue
        k = (h["market"], h.get("outcome"))
        first.setdefault(k, h)
    rs = [(h, won(h)) for h in first.values()]
    done = [(h, w) for h, w in rs if w is not None]
    if not done: print(f"{label:<42} n 0 (open {len(rs)})"); return
    v = [(1 / h["price"] - 1 if w else -1) - 0.07 * (1 - h["price"]) for h, w in done]
    bs = sorted(sum(random.choices(v, k=len(v))) / len(v) for _ in range(2000))
    wins = sum(w for _, w in done); px = sum(h["price"] for h, _ in done) / len(done)
    print(f"{label:<42} {wins:3d}-{len(done)-wins:<3d} open {len(rs)-len(done):3d} | win {wins/len(done):.2f} px {px:.2f} | roi {sum(v)/len(v):+.3f} [{bs[100]:+.3f},{bs[1900]:+.3f}]")
random.seed(1)
e = lambda h: h.get("ml_edge") is not None
pxok = lambda h: float(h["price"]) <= 0.85
print(f"hits {len(hits)}, with model edge {sum(map(e, hits))}, markets {len(cids)}, resolved {sum(1 for m in res.values() if m.get('closed'))}\n")
book("A. rule gate ($5K+, strong rule or 2+)", lambda h: float(h["usdc"]) >= 5000 and pxok(h) and (rules(h) & STRONG or len(rules(h)) >= 2))
book("B. model gate (edge>=.12, $5K+) = 11-05..07 texts", lambda h: e(h) and float(h["usdc"]) >= 5000 and pxok(h) and h["ml_edge"] >= 0.12)
for c in ("esports", "sports", "other"):
    book(f"   B, {c}", lambda h, c=c: e(h) and float(h["usdc"]) >= 5000 and pxok(h) and h["ml_edge"] >= 0.12 and cat(h) == c)
for t in (0.05, 0.08, 0.12, 0.20):
    for u in (1000, 2000, 5000):
        book(f"   model edge>={t:.2f}, ${u}+", lambda h, t=t, u=u: e(h) and float(h["usdc"]) >= u and pxok(h) and h["ml_edge"] >= t)
book("   model edge<0 (control), $5K+", lambda h: e(h) and float(h["usdc"]) >= 5000 and pxok(h) and h["ml_edge"] < 0)
book("C. favorites lane (fresh<7d, .60-.85, $1K+)", lambda h: 0.60 <= float(h["price"]) < 0.85 and float(h["usdc"]) >= 1000 and (h.get("age_h") or 1e9) < 168 and rules(h) - {"M1_model_edge"})
book("all rule hits, price<=.85", pxok)
