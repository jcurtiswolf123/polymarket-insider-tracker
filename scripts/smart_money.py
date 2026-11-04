"""Where the top-PnL Polymarket wallets hold open positions that resolve soon.

    uv run python scripts/smart_money.py [--days 10] [--top 50]

Consensus = several leaderboard wallets on the same outcome. Research aid only, not advice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

API = "https://data-api.polymarket.com/v2"


async def get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    for attempt in range(4):
        try:
            r = await client.get(f"{API}/{path}", params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            if attempt == 3:
                return {}
            await asyncio.sleep(1.5 * (attempt + 1))
    return {}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--top", type=int, default=50)
    a = ap.parse_args()
    horizon = (datetime.now(UTC) + timedelta(days=a.days)).date().isoformat()
    today = datetime.now(UTC).date().isoformat()
    async with httpx.AsyncClient(timeout=25.0) as client:
        wallets: dict[str, dict] = {}
        for period in ("WEEK", "MONTH", "ALL"):
            body = await get(
                client, "leaderboard", {"timePeriod": period, "orderBy": "PNL", "limit": a.top}
            )
            for row in body.get("data") or []:
                w = wallets.setdefault(row["user_id"], {"name": row.get("user_name"), "pnl": {}})
                w["pnl"][period] = round(row.get("pnl") or 0)
        sem = asyncio.Semaphore(8)

        async def positions(w: str) -> list[dict]:
            async with sem:
                body = await get(client, "positions", {"user": w, "limit": 500, "sizeThreshold": 1})
                return body.get("data") or []

        results = await asyncio.gather(*(positions(w) for w in wallets))
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"wallets": [], "value": 0.0, "cost": 0.0, "shares": 0.0}
    )
    for w, rows in zip(wallets, results, strict=True):
        for p in rows:
            end = p.get("end_date") or ""
            px = p.get("current_price") or 0
            if p.get("status") != "OPEN" or p.get("redeemable") or not end:
                continue
            if not (today <= end <= horizon) or not (0.10 <= px <= 0.90):
                continue
            if (p.get("current_value") or 0) < 1000:
                continue
            g = groups[(p["condition_id"], p["outcome"])]
            g.update(title=p["title"], outcome=p["outcome"], px=px, end=end, slug=p["event_slug"])
            g["wallets"].append(wallets[w]["name"] or w[:10])
            g["value"] += p["current_value"]
            g["cost"] += p.get("total_cost_usdc") or 0
            g["shares"] += p.get("current_size") or 0
    out = []
    for (cid, outcome), g in groups.items():
        opp = [k for k in groups if k[0] == cid and k[1] != outcome]
        out.append(
            {
                "title": g["title"],
                "outcome": outcome,
                "now_px": g["px"],
                "avg_entry": round(g["cost"] / g["shares"], 3) if g["shares"] else None,
                "top_wallets": len(g["wallets"]),
                "names": g["wallets"][:8],
                "value_usdc": round(g["value"]),
                "against": sum(len(groups[k]["wallets"]) for k in opp),
                "ends": g["end"],
                "condition": cid,
            }
        )
    out.sort(key=lambda x: (-(x["top_wallets"] - x["against"]), -x["value_usdc"]))
    json.dump({"wallets": len(wallets), "picks": out[:60]}, sys.stdout, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
