"""How many dollars traded at (or under) the stale price in the 5 min after each trigger: a ceiling on what we could buy."""
import json, sys, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
sys.path.insert(0, "scripts")
from history_backfill import get, TRADES_URL
H = Path.home() / ".polywatch/history"
E = json.load(open(H / "stale_over_entries.json"))
def raw(cid):
    params, out = {"condition": cid, "limit": 500}, []
    for _ in range(40):
        b = get(f"{TRADES_URL}?{urllib.parse.urlencode(params)}"); out += b.get("data") or []
        cur = (b.get("pagination") or {}).get("next_cursor")
        if not (b.get("pagination") or {}).get("has_more") or not cur: break
        params = {"condition": cid, "limit": 500, "cursor": cur}
    return out
cids = sorted({e["cid"] for e in E})
with ThreadPoolExecutor(6) as ex:
    R = dict(zip(cids, ex.map(raw, cids)))
avail = []
for e in E:
    usd = 0.0
    for t in R[e["cid"]]:
        ts = int(t["timestamp"])
        p_over = float(t["price"]) if t.get("outcome_index") == 0 else 1 - float(t["price"])
        if e["trigger"] <= ts <= e["trigger"] + 300 and t.get("side") == "BUY" and t.get("outcome_index") == 0 and p_over <= e["px"] + 0.01:
            usd += float(t["size"]) * float(t["price"])
    avail.append(usd)
a = sorted(avail)
print(f"entries {len(a)}: Over bought at <= stale price +1c within 5 min of the goal, per entry: median ${median(a):,.0f}, p25 ${a[len(a)//4]:,.0f}, p75 ${a[3*len(a)//4]:,.0f}, p90 ${a[9*len(a)//10]:,.0f}, total ${sum(a):,.0f}")
sub = [(e, u) for e, u in zip(E, avail) if u > 0]
fee = lambda p: 0.07 * (1 - p)
if sub:
    w = sum(e["won"] for e, _ in sub); px = sum(e["px"] for e, _ in sub) / len(sub)
    roi = sum((e["won"] - e["px"]) / e["px"] - fee(e["px"]) for e, _ in sub) / len(sub)
    dollar = sum(u * ((e["won"] - e["px"]) / e["px"] - fee(e["px"])) for e, u in sub)
    print(f"entries where an Over buy happened at <= stale +1c: {len(sub)}, won {w} ({w/len(sub):.1%}) vs avg px {px:.3f}, roi per bet {roi:+.2f}, profit on all that volume ${dollar:,.0f} of ${sum(u for _, u in sub):,.0f}")
