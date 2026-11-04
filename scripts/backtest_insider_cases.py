"""Replay known insider cases (and a control sample of ordinary trades) through insider_rules.

    uv run python scripts/backtest_insider_cases.py cases.json [--control-pages N]

A case is caught when any BUY trade the wallet made in the case's market fires a rule. The
control sample is the latest public trades of $1,000+; its fire rate estimates alerts per day.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from polymarket_insider_tracker.detector.insider_rules import evaluate
from polymarket_insider_tracker.profiler.wallet_history import (
    features_for,
    fetch_wallet_history,
)

GAMMA = "https://gamma-api.polymarket.com"
TRADES = "https://data-api.polymarket.com/v2/trades"


async def condition_ids_for_slug(client: httpx.AsyncClient, slug: str) -> set[str]:
    """Condition ids for a market slug or an event slug (an event holds several markets)."""
    ids: set[str] = set()
    for path, key in (("/markets", None), ("/events", "markets")):
        for closed in ("true", "false"):
            r = await client.get(f"{GAMMA}{path}", params={"slug": slug, "closed": closed})
            if r.status_code != 200:
                continue
            for item in r.json() or []:
                markets = item.get(key) if key else [item]
                for m in markets or []:
                    if m.get("conditionId"):
                        ids.add(m["conditionId"].lower())
    return ids


async def run_case(client: httpx.AsyncClient, case: dict) -> dict:
    raw = case.get("market_slug") or ""
    slugs = [x.strip().split(" ")[0].strip("/").split("/")[-1] for x in raw.split(";") if x.strip()]
    conds: set[str] = set()
    for one_slug in slugs:
        conds |= await condition_ids_for_slug(client, one_slug)
    slug = ";".join(slugs)
    out = {"name": case["name"], "slug": slug, "markets": len(conds), "wallets": []}
    for raw_wallet in case.get("wallets") or []:
        m = re.search(r"0x[0-9a-fA-F]{40}", raw_wallet)
        if not m:
            out["wallets"].append(
                {"wallet": raw_wallet[:40], "caught": False, "note": "no full address"}
            )
            continue
        wallet = m.group(0)
        hist = await fetch_wallet_history(wallet, client=client, max_pages=20)
        in_case = [h for h in hist.trades if h.condition_id in conds] if conds else []
        if not conds:
            # No slug resolved: judge the wallet's largest buys anywhere.
            in_case = sorted((h for h in hist.trades if h.side == "BUY"), key=lambda h: -h.usdc)[
                :10
            ]
        fired_at: list[tuple[int, list[str], float]] = []
        best: tuple[Decimal, dict] | None = None
        for h in in_case:
            if h.side != "BUY":
                continue
            f = features_for(
                hist,
                condition_id=h.condition_id,
                trade_ts=h.timestamp,
                notional_usdc=h.usdc,
                price=h.price,
                side=h.side,
                event_slug=h.event_slug,
            )
            rules = evaluate(f)
            snap = {
                "ts": h.timestamp,
                "usdc": float(h.usdc),
                "price": float(h.price),
                "age_h": round(f.wallet_age_hours or 0, 1),
                "prior_mkts": f.prior_markets,
                "share": round(float(f.market_share or 0), 2),
                "cum": float(f.market_cumulative_usdc or 0),
                "rules": rules,
            }
            if rules:
                fired_at.append((h.timestamp, rules, float(h.usdc)))
            stake = f.market_cumulative_usdc or Decimal(0)
            if best is None or stake > best[0]:
                best = (stake, snap)
        out["wallets"].append(
            {
                "wallet": wallet,
                "trades_in_case": len(in_case),
                "history_trades": len(hist.trades),
                "first_seen": _d(hist.first_ts),
                "caught": bool(fired_at),
                "first_fire": _d(fired_at[0][0]) if fired_at else None,
                "rules": sorted({r for _, rs, _ in fired_at for r in rs}),
                "biggest_position": best[1] if best else None,
            }
        )
    out["caught"] = any(w["caught"] for w in out["wallets"])
    return out


async def control(client: httpx.AsyncClient, pages: int) -> dict:
    """Fire rate on the latest ordinary trades of $1,000+ (BUY side)."""
    rows: list[dict] = []
    params: dict[str, str] = {"limit": "1000"}
    for _ in range(pages):
        r = await client.get(TRADES, params=params)
        body = r.json()
        rows += body.get("data") or []
        cur = (body.get("pagination") or {}).get("next_cursor")
        if not cur:
            break
        params = {"cursor": cur}
    big = [x for x in rows if x["side"] == "BUY" and x["size"] * x["price"] >= 1000]
    span_h = (max(x["timestamp"] for x in rows) - min(x["timestamp"] for x in rows)) / 3600
    counts: Counter[str] = Counter()
    fired_trades = 0
    sem = asyncio.Semaphore(8)

    async def one(x: dict) -> list[str]:
        async with sem:
            try:
                hist = await fetch_wallet_history(x["proxy_wallet"], client=client, max_pages=2)
            except Exception:
                return ["ERR"]
        usdc = Decimal(str(x["size"])) * Decimal(str(x["price"]))
        f = features_for(
            hist,
            condition_id=x["condition_id"].lower(),
            trade_ts=int(x["timestamp"]),
            notional_usdc=usdc,
            price=Decimal(str(x["price"])),
            side=x["side"],
            event_slug=x.get("event_slug") or "",
        )
        return evaluate(f)

    results = await asyncio.gather(*(one(x) for x in big))
    examples = []
    for x, rules in zip(big, results, strict=True):
        if rules == ["ERR"]:
            counts["fetch_error"] += 1
            continue
        if rules:
            fired_trades += 1
            counts.update(rules)
            if len(examples) < 12:
                examples.append((round(x["size"] * x["price"]), x["price"], x["title"][:60], rules))
    return {
        "span_hours": round(span_h, 2),
        "trades_total": len(rows),
        "buys_1k_plus": len(big),
        "fired": fired_trades,
        "per_rule": dict(counts),
        "est_alerts_per_day": round(fired_trades / span_h * 24) if span_h else None,
        "examples": examples,
    }


def _d(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M") if ts else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("--control-pages", type=int, default=5)
    a = ap.parse_args()
    with open(a.cases) as fh:
        cases = json.load(fh)
    async with httpx.AsyncClient(timeout=20.0) as client:
        results = []
        for c in cases:
            if not c.get("wallets"):
                results.append({"name": c["name"], "caught": None, "note": "no wallet address"})
                continue
            try:
                results.append(await run_case(client, c))
            except Exception as e:  # keep the batch going; report the case as errored
                results.append({"name": c["name"], "caught": None, "error": repr(e)[:200]})
        ctrl = await control(client, a.control_pages) if a.control_pages else None
    json.dump({"cases": results, "control": ctrl}, sys.stdout, indent=1, default=str)


if __name__ == "__main__":
    asyncio.run(main())
