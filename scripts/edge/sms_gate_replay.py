"""Replay live SMS gates over every logged rule hit (rule_hits.jsonl), settle via Gamma, $1 per first hit per market+side.
  uv run --env-file .env python scripts/edge/sms_gate_replay.py           full gate table
  lane_lines() -> weekly favorites-lane lines for the Sunday text (scripts/fav_lane.py report --sms)
shortcut: fills at the flagged wallet's price; real fills are worse.
"""
import json, re, random, urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

P = Path.home() / ".polywatch"
STRONG = {"R1_fresh_big_bet", "R5_whale_thin_history", "R6_whale_longshot", "R7_cluster", "R8_pre_resolution", "M1_model_edge"}
ESP = re.compile(r"counter-strike|lol:|dota|valorant|cs2|esports|overwatch|rainbow", re.I)
SPT = re.compile(r"\bvs\.?\b|o/u|spread|handicap|win on 20|nba|nfl|nhl|ufc", re.I)
HIST = "history 07-27..11-02: 463 mkts, win 69% vs px 70%, -3%"  # scripts/edge/fav_backtest.py, lane+pregame_not_active


def load_hits() -> list[dict]:
    hits = []
    for l in open(P / "rule_hits.jsonl"):
        h = json.loads(l)
        try:
            for k in ("price", "usdc", "ml_edge"):
                if h.get(k) is not None:
                    h[k] = float(h[k])
        except (TypeError, ValueError):
            continue
        hits.append(h)
    return hits


def resolutions(cids) -> dict:
    cids, res = sorted(cids), {}
    for i in range(0, len(cids), 40):
        for closed in ("true", "false"):
            q = "&".join(f"condition_ids={c}" for c in cids[i:i + 40])
            req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets?{q}&closed={closed}&limit=100", headers={"User-Agent": "pw"})
            for m in json.load(urllib.request.urlopen(req, timeout=30)):
                res[m["conditionId"]] = m
    return res


def won(h, res):
    m = res.get(h["market"])
    if not m or not m.get("closed"):
        return None
    outs, px = json.loads(m["outcomes"]), [float(x) for x in json.loads(m["outcomePrices"])]
    if max(px) < 0.99 or h.get("outcome") not in outs:
        return None
    return px[outs.index(h["outcome"])] > 0.5


rules = lambda h: {r.removeprefix("W_") for r in h.get("rules") or []}
roi = lambda h, w: (1 / h["price"] - 1 if w else -1) - 0.07 * (1 - h["price"])  # Kalshi taker fee
in_lane = lambda h: 0.60 <= h["price"] < 0.85 and h["usdc"] >= 1000 and (h.get("age_h") or 1e9) < 168 and bool(rules(h) - {"M1_model_edge"})


def firsts(hits, keep):
    first = {}
    for h in hits:
        try:
            if keep(h):
                first.setdefault((h["market"], h.get("outcome")), h)
        except (TypeError, ValueError, KeyError):
            continue
    return list(first.values())


def lane_lines() -> list[str]:
    hits = load_hits()
    lane = firsts(hits, in_lane)
    res = resolutions({h["market"] for h in lane})
    done = [(h, w) for h in lane if (w := won(h, res)) is not None]
    if not done:
        return ["- live replay: 0 resolved"]
    wk = defaultdict(list)
    for h, w in done:
        wk[datetime.fromisoformat(h["ts"]).strftime("%G-W%V")].append((h, w))
    out = []
    for k in sorted(wk)[-4:]:
        d = wk[k]; n = sum(w for _, w in d)
        out.append(f"- {k[5:]}: {n}-{len(d) - n}, win {n / len(d):.0%} vs px {sum(h['price'] for h, _ in d) / len(d):.0%}, roi {sum(roi(h, w) for h, w in d) / len(d):+.0%}")
    v = [roi(h, w) for h, w in done]; n = sum(w for _, w in done)
    random.seed(0)
    bs = sorted(sum(random.choices(v, k=len(v))) / len(v) for _ in range(2000))
    out.append(f"- live total: {n}-{len(done) - n}, roi {sum(v) / len(v):+.0%} (90% {bs[100]:+.0%} to {bs[1900]:+.0%}), {len(lane) - len(done)} open")
    out.append(f"- {HIST}")
    out.append("- call it at 200 resolved: need roi > +5% with range above 0")
    return out


def main():
    hits = load_hits()
    res = resolutions({h["market"] for h in hits})
    ESPc = lambda h: "esports" if ESP.search(h.get("title") or "") else "sports" if SPT.search(h.get("title") or "") else "other"

    def book(label, keep):
        rs = [(h, won(h, res)) for h in firsts(hits, keep)]
        done = [(h, w) for h, w in rs if w is not None]
        if not done:
            print(f"{label:<44} n 0 (open {len(rs)})"); return
        v = [roi(h, w) for h, w in done]
        bs = sorted(sum(random.choices(v, k=len(v))) / len(v) for _ in range(2000))
        wins = sum(w for _, w in done); px = sum(h["price"] for h, _ in done) / len(done)
        print(f"{label:<44} {wins:3d}-{len(done) - wins:<3d} open {len(rs) - len(done):3d} | win {wins / len(done):.2f} px {px:.2f} | roi {sum(v) / len(v):+.3f} [{bs[100]:+.3f},{bs[1900]:+.3f}]")

    random.seed(1)
    e = lambda h: h.get("ml_edge") is not None
    pxok = lambda h: h["price"] <= 0.85
    print(f"hits {len(hits)}, with model edge {sum(map(e, hits))}\n")
    book("A. rule gate ($5K+, strong rule or 2+)", lambda h: h["usdc"] >= 5000 and pxok(h) and (rules(h) & STRONG or len(rules(h)) >= 2))
    book("B. model gate (edge>=.12, $5K+)", lambda h: e(h) and h["usdc"] >= 5000 and pxok(h) and h["ml_edge"] >= 0.12)
    for c in ("esports", "sports", "other"):
        book(f"   B, {c}", lambda h, c=c: e(h) and h["usdc"] >= 5000 and pxok(h) and h["ml_edge"] >= 0.12 and ESPc(h) == c)
    book("C. favorites lane (fresh<7d, .60-.85, $1K+)", in_lane)
    book("all rule hits, price<=.85", pxok)
    print("\n" + "\n".join(lane_lines()))


if __name__ == "__main__":
    main()
