"""Scan every open Kalshi event for risk-free bundles after taker fees.

Checks (per $1 contract, Kalshi taker fee ceil(0.07*p*(1-p)*100)/100 per leg):
  ALL_NO   mutually exclusive event: buy No on every leg. At most one leg settles Yes,
           so the bundle pays >= n-1. Risk-free whether or not the legs are exhaustive.
  ALL_YES  mutually exclusive event: buy Yes on every leg. Pays exactly 1 ONLY if the legs
           are exhaustive (one must happen). Flagged, needs a rules check before trading.
  LADDER   "above X" legs in one event: Yes(above a) + No(above b), a < b, pays >= 1.

Depth: size is the top-of-book quantity; the bundle is capped by its thinnest leg.
Run: uv run python scripts/kalshi_arb.py [--min-edge 0.005] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"


def get(url: str, tries: int = 4) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "polywatch-kalshi-arb"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"failed {url}")


def fee(p: float) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * 100, 6)) / 100 if 0 < p < 1 else 0.0


def f(x: object) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def events() -> list[dict]:
    out, cursor = [], ""
    while True:
        d = get(f"{API}/events?status=open&limit=200&with_nested_markets=true&cursor={cursor}")
        out += d.get("events", [])
        cursor = d.get("cursor") or ""
        if not cursor:
            return out


def legs(e: dict) -> list[dict]:
    rows = []
    for m in e.get("markets", []):
        if m.get("status") not in ("active", "open"):
            continue
        rows.append(
            {
                "ticker": m["ticker"],
                "name": m.get("yes_sub_title") or m["ticker"],
                "yes_ask": f(m.get("yes_ask_dollars")),
                "yes_ask_sz": f(m.get("yes_ask_size_fp")),
                "yes_bid": f(m.get("yes_bid_dollars")),
                "yes_bid_sz": f(m.get("yes_bid_size_fp")),
                "no_ask": f(m.get("no_ask_dollars")),
            }
        )
    return rows


def check_all_no(e: dict, ls: list[dict]) -> dict | None:
    if len(ls) < 2 or any(not 0 < x["no_ask"] < 1 for x in ls):
        return None
    cost = sum(x["no_ask"] + fee(x["no_ask"]) for x in ls)
    edge = (len(ls) - 1) - cost
    size = min(x["yes_bid_sz"] for x in ls)  # No ask depth == Yes bid depth
    return {
        "type": "ALL_NO",
        "edge": round(edge, 4),
        "size": size,
        "legs": len(ls),
        "cost": round(cost, 4),
        "payout_min": len(ls) - 1,
    }


def check_all_yes(e: dict, ls: list[dict]) -> dict | None:
    if len(ls) < 2 or any(not 0 < x["yes_ask"] < 1 for x in ls):
        return None
    cost = sum(x["yes_ask"] + fee(x["yes_ask"]) for x in ls)
    return {
        "type": "ALL_YES (needs exhaustive)",
        "edge": round(1 - cost, 4),
        "size": min(x["yes_ask_sz"] for x in ls),
        "legs": len(ls),
        "cost": round(cost, 4),
        "payout_min": 1,
    }


STRIKE = re.compile(r"(above|over|more than|at least|or more|or above|\+)", re.I)
NUM = re.compile(r"-?\$?\d[\d,]*\.?\d*")


def strike_of(name: str) -> float | None:
    if not STRIKE.search(name) or re.search(r"\b(below|under|less|between|to)\b", name, re.I):
        return None
    m = NUM.search(name)
    return float(m.group().replace("$", "").replace(",", "")) if m else None


def subject_of(name: str) -> str:
    """The leg's wording with the strike removed: only legs on the same subject form a ladder."""
    return NUM.sub("#", name).strip().lower()


def check_ladder(e: dict, ls: list[dict]) -> list[dict]:
    pts = [(strike_of(x["name"]), x) for x in ls]
    pts = sorted([(s, x) for s, x in pts if s is not None], key=lambda t: t[0])
    out = []
    for i, (a, la) in enumerate(pts):
        for b, lb in pts[i + 1 :]:
            if b <= a or subject_of(la["name"]) != subject_of(lb["name"]):
                continue  # "LV by 3.5" + "No SF by 4.5" is not a ladder: SF by 10 loses both
            if not (0 < la["yes_ask"] < 1 and 0 < lb["no_ask"] < 1):
                continue
            cost = la["yes_ask"] + fee(la["yes_ask"]) + lb["no_ask"] + fee(lb["no_ask"])
            if 1 - cost > 0:
                out.append(
                    {
                        "type": "LADDER",
                        "edge": round(1 - cost, 4),
                        "size": min(la["yes_ask_sz"], lb["yes_bid_sz"]),
                        "legs": 2,
                        "cost": round(cost, 4),
                        "payout_min": 1,
                        "detail": f"Yes {la['name']} @{la['yes_ask']} + No {lb['name']} @{lb['no_ask']}",
                    }
                )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.0)
    ap.add_argument("--json")
    a = ap.parse_args()
    evs = events()
    hits, me = [], 0
    for e in evs:
        ls = legs(e)
        cands = check_ladder(e, ls)
        if e.get("mutually_exclusive"):
            me += 1
            cands += [c for c in (check_all_no(e, ls), check_all_yes(e, ls)) if c]
        for c in cands:
            if c["edge"] > a.min_edge:
                c.update(event=e["event_ticker"], title=e.get("title"), sub=e.get("sub_title"))
                c["dollars_at_top"] = round(c["edge"] * c["size"], 2)
                hits.append(c)
    hits.sort(key=lambda c: -c["dollars_at_top"])
    print(f"events {len(evs)}  mutually-exclusive {me}  hits {len(hits)}")
    for c in hits[:40]:
        print(
            f"{c['type']:26} edge {c['edge']:+.3f}/bundle  size {c['size']:>8,.0f}  "
            f"~${c['dollars_at_top']:>8,.2f}  legs {c['legs']:>2} | {c['event']} | {str(c['title'])[:60]}"
            + (f"\n{'':28}{c['detail']}" if c.get("detail") else "")
        )
    if a.json:
        Path(a.json).write_text(json.dumps(hits, indent=1))


if __name__ == "__main__":
    main()
