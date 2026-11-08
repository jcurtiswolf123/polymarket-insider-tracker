"""How much lane edge is left if you enter d seconds after the insider's trade."""

import json, sys, time, random, urllib.request
from datetime import datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import learner as L, fav_lane as F


def ep(t):
    if isinstance(t, (int, float)):
        return float(t)
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()


def trades(cond):
    out, cur = [], None
    for _ in range(15):
        u = f"https://data-api.polymarket.com/v2/trades?condition={cond}&limit=1000" + (
            f"&cursor={cur}" if cur else ""
        )
        d = json.load(
            urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": "polywatch"}), timeout=30
            )
        )
        out += d["data"]
        cur = d["pagination"].get("next_cursor")
        if not cur:
            break
    return sorted(out, key=lambda t: t["timestamp"])


rows = L.read_rows()
labels = json.loads(L.LABELS_PATH.read_text())
first = {}
for r in sorted(rows, key=lambda r: ep(r["ts"])):
    lab = labels.get(r.get("market") or "")
    if not lab or not lab.get("closed") or r["market"] in first:
        continue
    h = r.get("hours_to_end")
    if not (F.prefilter(r) and h is not None and 0 < h <= F.MAX_HOURS_TO_END):
        continue
    i = r.get("outcome_index")
    ps = lab.get("prices") or []
    if i is None or i >= len(ps) or ps[i] not in (0.0, 1.0):
        continue
    first[r["market"]] = (ep(r["ts"]), i, float(r["price"]), int(ps[i] == 1.0), r.get("slug"))
print("lane markets:", len(first))
DELAYS = [0, 30, 120, 600, 1800]
res = {d: [] for d in DELAYS}
for n, (cond, (t0, i, p0, won, slug)) in enumerate(first.items()):
    try:
        tr = trades(cond)
    except Exception as e:
        print("skip", cond[:10], e)
        continue
    for d in DELAYS:
        nxt = next((x for x in tr if x["timestamp"] >= t0 + d), None)
        if not nxt:
            continue
        px = nxt["price"] if nxt["outcome_index"] == i else 1 - nxt["price"]
        if 0.01 < px < 0.99:
            res[d].append((won - px, px))
    time.sleep(0.2)
for d, v in res.items():
    ex = [e for e, _ in v]
    k = len(ex)
    bs = sorted(sum(random.choice(ex) for _ in ex) / k for _ in range(3000))
    print(
        f"enter +{d:>4}s: {k} mkts, avg entry {sum(p for _, p in v) / k:.3f}, edge {sum(ex) / k:+.3f}, 90% CI [{bs[150]:+.3f},{bs[2850]:+.3f}]"
    )
hits = [json.loads(l) for l in open(F.HITS)]
lag = sorted(ep(h["seen"]) - ep(h["ts"]) for h in hits if F.prefilter(h) and h.get("seen"))
print(
    f"our detection lag on lane hits: median {lag[len(lag) // 2]:.0f}s, p90 {lag[int(len(lag) * 0.9)]:.0f}s, n {len(lag)}"
)
