"""Full Polymarket history of each wallet behind a resolved live favorites-lane hit: do they beat the price?"""
import json, sys, time, urllib.request, urllib.parse
from collections import defaultdict
sys.path.insert(0, "scripts/edge")
import sms_gate_replay as g
def get(url):
    for i in range(4):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pw"}), timeout=30))
        except Exception:
            time.sleep(2 * (i + 1))
    return {"data": []}
def activity(w, pages=10):
    out, cur = [], None
    for _ in range(pages):
        d = get(f"https://data-api.polymarket.com/v2/activity?user={w}&limit=500" + (f"&cursor={urllib.parse.quote(cur)}" if cur else ""))
        out += d.get("data", []); cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur: break
    return out, bool(cur)
hits = g.load_hits(); lane = g.firsts(hits, g.in_lane)
res = g.resolutions({h["market"] for h in lane})
lane = [h for h in lane if g.won(h, res) is not None]
wallets = {}
for h in lane: wallets.setdefault(h["wallet"], h)
acts = {w: activity(w) for w in wallets}
pos = {}  # (wallet, cid, oi) -> [usdc, shares, first_ts, title]
for w, (a, _) in acts.items():
    for x in a:
        if x["type"] == "TRADE" and x["side"] == "BUY" and x["condition_id"]:
            k = (w, x["condition_id"], x["outcome_index"]); p = pos.setdefault(k, [0.0, 0.0, x["timestamp"], x["title"]])
            p[0] += x["usdc_size"]; p[1] += x["size"]; p[2] = min(p[2], x["timestamp"])
mres = g.resolutions({c for _, c, _ in pos})
print(f"{'wallet':10} {'lane hit':32} {'res':4} | {'first seen':10} mkts trunc | resolved  W-L   exp.W  win-px   roi   $bought | top categories")
tot = defaultdict(float)
for w, h in wallets.items():
    a, trunc = acts[w]
    first = min((x["timestamp"] for x in a), default=0)
    rows = []
    for (ww, c, oi), (usdc, sh, ts, title) in pos.items():
        if ww != w or usdc <= 0 or sh <= 0: continue
        m = mres.get(c)
        if not m or not m.get("closed"): continue
        px = [float(v) for v in json.loads(m["outcomePrices"])]
        if max(px) < .99 or oi >= len(px): continue
        rows.append((usdc / sh, px[oi] > .5, usdc, title))
    n = len(rows); wins = sum(r[1] for r in rows); exp = sum(r[0] for r in rows)
    pnl = sum(r[2] * (1 / r[0] - 1) if r[1] else -r[2] for r in rows); spent = sum(r[2] for r in rows)
    cats = defaultdict(int)
    for r in rows:
        t = r[3] or ""; cats["esports" if g.ESP.search(t) else "sports" if g.SPT.search(t) else "other"] += 1
    nm = len({c for ww, c, _ in pos if ww == w})
    for k, v in (("n", n), ("wins", wins), ("exp", exp), ("pnl", pnl), ("spent", spent)): tot[k] += v
    print(f"{w[:10]} {(h.get('title') or '')[:32]:32} {'WON' if g.won(h, res) else 'LOST':4} | {time.strftime('%Y-%m-%d', time.gmtime(first))} {nm:4d} {'Y' if trunc else ' ':5} | {n:4d} {wins:4d}-{n-wins:<4d} {exp:6.1f} {(wins-exp)/max(n,1):+.2f} {pnl/max(spent,1):+.2f} {spent:9,.0f} | {dict(sorted(cats.items(), key=lambda kv: -kv[1]))}")
print(f"\nALL: resolved positions {tot['n']:.0f}, wins {tot['wins']:.0f}, expected at price {tot['exp']:.1f}, win-px {(tot['wins']-tot['exp'])/tot['n']:+.3f}, roi {tot['pnl']/tot['spent']:+.3f} on ${tot['spent']:,.0f}")
