"""How fast does a skilled wallet's edge disappear? Price of the next fill at +5s..+120s after each followed bet.
  uv run python -I scripts/edge/skilled_decay.py [4.0|3.0]   (needs history/skilled_follow.json from wallet_edge.py)
"""
import bisect, json, random, sys, urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from history_backfill import get, TRADES_URL

H = Path.home() / ".polywatch/history"
DELAYS = (0, 5, 15, 30, 60, 120)


def fills(cid):
    c = H / "tapes" / f"{cid}.json"  # disk cache: resolved markets never change
    if c.exists():
        return tuple(json.loads(c.read_text()))
    out = _fetch(cid)
    c.parent.mkdir(exist_ok=True); c.write_text(json.dumps(out))
    return out


def _fetch(cid):
    params, raw = {"condition": cid, "limit": 500}, []
    for _ in range(40):
        try:
            body = get(f"{TRADES_URL}?{urllib.parse.urlencode(params)}")
        except Exception:
            break
        raw += body.get("data") or []
        cur = (body.get("pagination") or {}).get("next_cursor")
        if not (body.get("pagination") or {}).get("has_more") or not cur:
            break
        params = {"condition": cid, "limit": 500, "cursor": cur}
    raw.sort(key=lambda t: int(t["timestamp"]))
    return [int(t["timestamp"]) for t in raw], [float(t["price"]) if t.get("outcome_index") == 0 else 1 - float(t["price"]) for t in raw]



def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "4.0"
    bets = json.load(open(H / "skilled_follow.json"))[key]
    mk = sorted({b["m"] for b in bets})
    with ThreadPoolExecutor(6) as ex:
        F = dict(zip(mk, ex.map(fills, mk)))
    fee = lambda p: 0.07 * (1 - p)
    res = defaultdict(list)
    for b in bets:
        fts, p0 = F[b["m"]]
        for d in DELAYS:
            if d == 0:
                px = b["p"]
            else:
                j = bisect.bisect_left(fts, b["ts"] + d)
                if j >= len(fts):
                    continue
                px = p0[j] if b["oi"] == 0 else 1 - p0[j]
            if 0.02 < px < 0.97:
                res[d].append((b["m"], (b["y"] - px) / px - fee(px), b["inplay"]))
    random.seed(0)
    print(f"z >= {key}: {len(bets)} bets, {len(mk)} markets")
    for lab, keep in (("all", lambda x: True), ("in play", lambda x: x[2]), ("pre-game", lambda x: not x[2])):
        print(f"-- {lab}")
        for d in DELAYS:
            v = [x for x in res[d] if keep(x)]
            if not v:
                continue
            by = defaultdict(list)
            for m, r, _ in v:
                by[m].append(r)
            g = list(by.values())
            bs = sorted(sum(sum(x) for x in s) / sum(len(x) for x in s) for s in (random.choices(g, k=len(g)) for _ in range(1000)))
            print(f"   +{d:>3}s  n {len(v):5d}  roi {sum(r for _, r, _ in v) / len(v):+.3f}  [{bs[50]:+.3f},{bs[950]:+.3f}]")


if __name__ == "__main__":
    main()
