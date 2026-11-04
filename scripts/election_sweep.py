"""Sweep every open election market for insider-pattern buys over a recent window.

    uv run python scripts/election_sweep.py [--hours 48] [--min-usdc 500] > sweep.json

For each election market: every BUY of --min-usdc or more in the window (server-side filtered),
grouped per wallet and market, scored by insider_rules at default and at wide thresholds, plus
election signals the per-trade rules cannot see:
  C1_fresh_cluster   3+ wallets under 7 days old buying the same outcome, $5K+ combined
  P1_priced_in       bought at <= 0.40 and the outcome now trades at >= 0.85 (early, right)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from decimal import Decimal

import httpx

from polymarket_insider_tracker.detector.insider_rules import RuleThresholds, evaluate, widened
from polymarket_insider_tracker.profiler.wallet_history import features_for, fetch_wallet_history

GAMMA = "https://gamma-api.polymarket.com/events"
TRADES = "https://data-api.polymarket.com/v2/trades"
TAGS = ("elections", "midterms", "house-races", "politics", "us-politics", "governor", "senate")
TITLE = re.compile(
    r"2026|midterm|governor|gubernatorial|senate|house|congress|mayor|proposition|prop \d|"
    r"ballot|election|attorney general|secretary of state|special election|recall",
    re.I,
)


async def get(client: httpx.AsyncClient, url: str, params: dict) -> object:
    for attempt in range(5):
        try:
            r = await client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError("retry", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 4:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError


async def election_markets(client: httpx.AsyncClient) -> dict[str, dict]:
    markets: dict[str, dict] = {}
    for tag in TAGS:
        for offset in range(0, 2100, 100):
            params = {"tag_slug": tag, "closed": "false", "limit": "100", "offset": str(offset)}
            try:
                events = await get(client, GAMMA, params)
            except httpx.HTTPError:
                break
            if not events:
                break
            for e in events:
                for m in e.get("markets") or []:
                    text = f"{e.get('title', '')} {m.get('question', '')}"
                    cid = (m.get("conditionId") or "").lower()
                    if not cid or not TITLE.search(text):
                        continue
                    if tag == "politics" and "2026" not in text and "midterm" not in text.lower():
                        continue
                    try:
                        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                        outcomes = json.loads(m.get("outcomes") or "[]")
                    except ValueError:
                        prices, outcomes = [], []
                    markets[cid] = {
                        "event": e.get("slug"),
                        "q": m.get("question"),
                        "outcomes": outcomes,
                        "prices": prices,
                        "vol24": float(m.get("volume24hr") or 0),
                    }
            if len(events) < 100:
                break
    return markets


async def big_buys(client, cid: str, since: int, min_usdc: int, max_pages: int) -> list[dict]:
    rows: list[dict] = []
    params = {
        "condition": cid,
        "limit": "1000",
        "side": "BUY",
        "filterType": "CASH",
        "filterAmount": str(min_usdc),
    }
    for _ in range(max_pages):
        body = await get(client, TRADES, params)
        page = body.get("data") or []
        rows += [r for r in page if r["timestamp"] >= since]
        cur = (body.get("pagination") or {}).get("next_cursor")
        if not page or page[-1]["timestamp"] < since or not cur:
            break
        params = {"cursor": cur}
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--min-usdc", type=int, default=500)
    ap.add_argument("--max-pages", type=int, default=10)
    a = ap.parse_args()
    now = int(time.time())
    since = now - int(a.hours * 3600)
    base = RuleThresholds()
    loose = widened(base)
    async with httpx.AsyncClient(timeout=25.0) as client:
        markets = await election_markets(client)
        live = {c: m for c, m in markets.items() if m["vol24"] > 0}
        print(f"markets {len(markets)} active24h {len(live)}", file=sys.stderr)
        sem = asyncio.Semaphore(10)

        async def pull(cid: str) -> list[dict]:
            async with sem:
                try:
                    return await big_buys(client, cid, since, a.min_usdc, a.max_pages)
                except httpx.HTTPError:
                    return []

        per_market = await asyncio.gather(*(pull(c) for c in live))
        trades = [t for rows in per_market for t in rows]
        print(f"buys>={a.min_usdc} {len(trades)}", file=sys.stderr)
        # Aggregate fills per wallet, market, outcome.
        pos: dict[tuple[str, str, int], dict] = {}
        for t in trades:
            k = (t["proxy_wallet"].lower(), t["condition_id"].lower(), int(t["outcome_index"]))
            p = pos.setdefault(
                k, {"usdc": 0.0, "shares": 0.0, "last_ts": 0, "first_ts": 1 << 40, "row": t}
            )
            p["usdc"] += t["size"] * t["price"]
            p["shares"] += t["size"]
            p["last_ts"] = max(p["last_ts"], t["timestamp"])
            p["first_ts"] = min(p["first_ts"], t["timestamp"])
        wallets = sorted({k[0] for k in pos})
        print(f"positions {len(pos)} wallets {len(wallets)}", file=sys.stderr)
        hist: dict[str, object] = {}

        async def load(w: str) -> None:
            async with sem:
                try:
                    hist[w] = await fetch_wallet_history(w, client=client, max_pages=3)
                except Exception:
                    hist[w] = None

        await asyncio.gather(*(load(w) for w in wallets))
    hits: list[dict] = []
    clusters: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for (w, cid, oi), p in pos.items():
        h = hist.get(w)
        m = markets[cid]
        avg = p["usdc"] / p["shares"] if p["shares"] else 0
        now_px = m["prices"][oi] if oi < len(m["prices"]) else None
        rec = {
            "wallet": w,
            "market": cid,
            "event": m["event"],
            "q": m["q"],
            "outcome": m["outcomes"][oi] if oi < len(m["outcomes"]) else oi,
            "usdc_window": round(p["usdc"]),
            "est_pnl": round(p["shares"] * now_px - p["usdc"]) if now_px is not None else None,
            "avg_px": round(avg, 3),
            "now_px": now_px,
            "first": time.strftime("%m-%d %H:%M", time.gmtime(p["first_ts"])),
            "last": time.strftime("%m-%d %H:%M", time.gmtime(p["last_ts"])),
        }
        if h is None:
            rec["err"] = "history"
            hits.append({**rec, "rules": [], "wide": []})
            continue
        r = p["row"]
        f = features_for(
            h,
            condition_id=cid,
            trade_ts=p["last_ts"],
            notional_usdc=Decimal(str(round(p["usdc"], 2))),
            price=Decimal(str(round(avg, 4))),
            side="BUY",
            event_slug=r.get("event_slug") or "",
        )
        rec["age_d"] = round((f.wallet_age_hours or 0) / 24, 1)
        rec["prior_mkts"] = f.prior_markets
        rec["share"] = round(float(f.market_share or 0), 2)
        rules = evaluate(f, base)
        wide_rules = [x for x in evaluate(f, loose) if x not in rules]
        signals = []
        if avg <= 0.40 and now_px is not None and now_px >= 0.85:
            signals.append("P1_priced_in")
        if (f.wallet_age_hours or 1e9) <= 24 * 7:
            clusters[(cid, oi)].append(rec)
        if rules or wide_rules or signals:
            hits.append({**rec, "rules": rules, "wide": wide_rules, "signals": signals})
    cluster_out = []
    for recs in clusters.values():
        total = sum(x["usdc_window"] for x in recs)
        if len(recs) >= 3 and total >= 5000:
            cluster_out.append(
                {
                    "q": recs[0]["q"],
                    "outcome": recs[0]["outcome"],
                    "wallets": len(recs),
                    "usdc": total,
                    "now_px": recs[0]["now_px"],
                    "est_pnl": sum(x["est_pnl"] or 0 for x in recs),
                    "avg_px": round(
                        sum(x["usdc_window"] for x in recs)
                        / max(1, sum(x["usdc_window"] / max(x["avg_px"], 1e-6) for x in recs)),
                        3,
                    ),
                    "members": [
                        {
                            k: x.get(k)
                            for k in (
                                "wallet",
                                "usdc_window",
                                "avg_px",
                                "est_pnl",
                                "age_d",
                                "first",
                                "last",
                            )
                        }
                        for x in recs
                    ],
                }
            )
    hits.sort(key=lambda x: (-len(x["rules"]), -len(x.get("signals", [])), -x["usdc_window"]))
    json.dump(
        {
            "window_h": a.hours,
            "markets": len(markets),
            "active": len(live),
            "buys": len(trades),
            "positions": len(pos),
            "hits": hits,
            "C1_fresh_clusters": sorted(cluster_out, key=lambda x: -x["usdc"]),
        },
        sys.stdout,
        indent=1,
    )


if __name__ == "__main__":
    asyncio.run(main())
