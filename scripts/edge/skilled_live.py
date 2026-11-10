"""Forward test: live trades (features.jsonl) by wallets scored z >= 4 on history (skilled_wallets.json),
only after the history ended, settled via Gamma, priced at their fill and at the next fill +5s / +60s.
  uv run python -I scripts/edge/skilled_live.py      lines() feeds the Sunday text
"""
import bisect, json, random, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import sms_gate_replay as g
from skilled_decay import fills

P = Path.home() / ".polywatch"
START = 1794294671  # 2026-11-09 23:11 PT: history (and the wallet scores) end here
fee = lambda p: 0.07 * (1 - p)


def bets():
    sk = json.load(open(P / "history/skilled_wallets.json"))
    first = {}
    for l in open(P / "features.jsonl"):
        r = json.loads(l)
        w = (r.get("wallet") or "").lower()
        if w in sk and int(r["ts"]) > START:
            first.setdefault((w, r["market"], r.get("outcome")), r)
    return list(first.values())


def lines():
    bs = bets()
    if not bs:
        return ["- skilled wallets: 0 live bets yet"]
    res = g.resolutions({b["market"] for b in bs})
    done = [(b, w) for b in bs if (w := g.won(b, res)) is not None]
    with ThreadPoolExecutor(6) as ex:
        F = dict(zip(mk := sorted({b["market"] for b, _ in done}), ex.map(fills, mk)))
    out = defaultdict(list)
    for b, w in done:
        fts, p0 = F[b["market"]]
        for d in (0, 5, 60):
            px = float(b["price"])
            if d:
                j = bisect.bisect_left(fts, int(b["ts"]) + d)
                if j >= len(fts):
                    continue
                px = p0[j] if int(b["outcome_index"]) == 0 else 1 - p0[j]
            if 0.02 < px < 0.97:
                out[d].append(((1 if w else 0) - px) / px - fee(px))
    n = sum(w for _, w in done)
    s = f"- skilled wallets live: {n}-{len(done) - n}, {len(bs) - len(done)} open"
    for d in (0, 5, 60):
        v = out[d]
        if v:
            random.seed(0)
            q = sorted(sum(random.choices(v, k=len(v))) / len(v) for _ in range(1000))
            s += f"\n  +{d}s roi {sum(v) / len(v):+.0%} ({q[50]:+.0%} to {q[950]:+.0%})"
    return [s, "- history z>=4 in play: +11% at +5s, +9% at +60s (n~310); copying needs < 5s"]


if __name__ == "__main__":
    print("\n".join(lines()))
