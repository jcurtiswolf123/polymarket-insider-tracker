"""A wallet's Polymarket trade history from the public data API, and rule features from it."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import httpx

from polymarket_insider_tracker.detector.insider_rules import TradeFeatures

logger = logging.getLogger(__name__)

ACTIVITY_URL = "https://data-api.polymarket.com/v2/activity"
PAGE_LIMIT = 1000
# A wallet with more trades than this is an established trader; its history is truncated and
# reported as "many markets", which is all the rules need.
MAX_PAGES = 3
LONGSHOT_PRICE = Decimal("0.3")
ACTIVE_MIN_TX_USDC = Decimal(os.environ.get("RULE_ACTIVE_MIN_TX_USDC", "50") or "50")


@dataclass(frozen=True)
class HistoryTrade:
    timestamp: int
    condition_id: str
    side: str
    price: Decimal
    usdc: Decimal
    transaction_hash: str
    event_slug: str = ""


@dataclass(frozen=True)
class WalletHistory:
    address: str
    first_ts: int | None  # first activity of any kind
    trades: tuple[HistoryTrade, ...]  # TRADE rows, oldest first
    truncated: bool
    # (timestamp, condition_id, usdc) for REDEEM rows: a payout on a resolved market
    redeems: tuple[tuple[int, str, Decimal], ...] = ()


async def fetch_wallet_history(
    address: str,
    *,
    until_ts: int | None = None,
    client: httpx.AsyncClient | None = None,
    max_pages: int = MAX_PAGES,
) -> WalletHistory:
    """Oldest-first activity for ``address``, stopping after ``until_ts`` or ``max_pages``."""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    trades: list[HistoryTrade] = []
    redeems: list[tuple[int, str, Decimal]] = []
    first_ts: int | None = None
    truncated = False
    params: dict[str, str] = {
        "user": address.lower(),
        "limit": str(PAGE_LIMIT),
        "sortDirection": "ASC",
    }
    try:
        for page in range(max_pages):
            body = await _get_json(client, params)
            rows = body.get("data") or []
            done = False
            for row in rows:
                ts = int(row.get("timestamp") or 0)
                if first_ts is None:
                    first_ts = ts
                if until_ts is not None and ts > until_ts:
                    done = True
                    break
                if row.get("type") == "REDEEM":
                    redeems.append(
                        (
                            ts,
                            str(row.get("condition_id") or ""),
                            Decimal(str(row.get("usdc_size") or 0)),
                        )
                    )
                    continue
                if row.get("type") != "TRADE":
                    continue
                price = Decimal(str(row.get("price") or 0))
                usdc = Decimal(str(row.get("usdc_size") or 0))
                trades.append(
                    HistoryTrade(
                        ts,
                        str(row.get("condition_id") or ""),
                        str(row.get("side") or ""),
                        price,
                        usdc,
                        str(row.get("transaction_hash") or ""),
                        str(row.get("event_slug") or ""),
                    )
                )
            cursor = (body.get("pagination") or {}).get("next_cursor")
            more = (body.get("pagination") or {}).get("has_more")
            if done or not more or not cursor:
                break
            if page == max_pages - 1:
                truncated = True
                break
            params = {"user": address.lower(), "cursor": cursor}
    finally:
        if owns:
            await client.aclose()
    return WalletHistory(address.lower(), first_ts, tuple(trades), truncated, tuple(redeems))


async def _get_json(client: httpx.AsyncClient, params: dict[str, str]) -> dict:
    for attempt in range(4):
        try:
            resp = await client.get(ACTIVITY_URL, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if attempt == 3 or (status and status < 500 and status != 429):
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def features_for(
    history: WalletHistory,
    *,
    condition_id: str,
    trade_ts: int,
    notional_usdc: Decimal,
    price: Decimal,
    side: str,
    event_slug: str = "",
) -> TradeFeatures:
    """Rule features for one trade, using only the wallet's history up to that trade."""
    prior = [h for h in history.trades if h.timestamp < trade_ts]
    upto = [h for h in history.trades if h.timestamp <= trade_ts]
    prior_markets = len({h.condition_id for h in prior if h.condition_id != condition_id})
    if history.truncated:
        prior_markets = max(prior_markets, 999)
    total = sum((h.usdc for h in upto if h.side == "BUY"), Decimal(0))
    in_market = sum(
        (h.usdc for h in upto if h.side == "BUY" and h.condition_id == condition_id), Decimal(0)
    )
    if not any(h.timestamp == trade_ts and h.condition_id == condition_id for h in upto):
        # Live trade not yet indexed by the data API: count it.
        total += notional_usdc if side.upper() == "BUY" else Decimal(0)
        in_market += notional_usdc if side.upper() == "BUY" else Decimal(0)
    share = (in_market / total) if total > 0 else None
    event_cum = Decimal(0)
    event_markets: set[str] = set()
    if event_slug:
        for h in upto:
            if h.side == "BUY" and h.event_slug == event_slug:
                event_cum += h.usdc
                event_markets.add(h.condition_id)
        unindexed = not any(
            h.timestamp == trade_ts and h.condition_id == condition_id for h in upto
        )
        if unindexed and side.upper() == "BUY":
            event_cum += notional_usdc
            event_markets.add(condition_id)
    cheap_in_market = sum(
        (
            h.usdc
            for h in upto
            if h.side == "BUY" and h.condition_id == condition_id and h.price <= LONGSHOT_PRICE
        ),
        Decimal(0),
    )
    unindexed_trade = not any(
        h.timestamp == trade_ts and h.condition_id == condition_id for h in upto
    )
    if unindexed_trade and side.upper() == "BUY" and price <= LONGSHOT_PRICE:
        cheap_in_market += notional_usdc
    # Long-shot record: earlier markets where the wallet bought at <= LONGSHOT_PRICE and later
    # redeemed more than it paid there (a win), all before this trade.
    cost: dict[str, Decimal] = {}
    for h in prior:
        if h.side == "BUY" and h.price <= LONGSHOT_PRICE and h.condition_id != condition_id:
            cost[h.condition_id] = cost.get(h.condition_id, Decimal(0)) + h.usdc
    paid: dict[str, Decimal] = {}
    for ts, cid, usdc in history.redeems:
        if ts < trade_ts and cid in cost:
            paid[cid] = paid.get(cid, Decimal(0)) + usdc
    wins = sum(1 for cid, c in cost.items() if paid.get(cid, Decimal(0)) > c)
    age = (trade_ts - history.first_ts) / 3600 if history.first_ts else 0.0
    # Distinct transactions worth >= ACTIVE_MIN_TX_USDC. Dust fills do not count, so a wallet
    # cannot pad its trade count cheaply to look like a bot and dodge the freshness rules.
    tx_usdc: dict[object, Decimal] = {}
    for h in prior:
        key = h.transaction_hash or id(h)
        tx_usdc[key] = tx_usdc.get(key, Decimal(0)) + h.usdc
    prior_trades = sum(1 for v in tx_usdc.values() if v >= ACTIVE_MIN_TX_USDC)
    return TradeFeatures(
        prior_trades=prior_trades,
        notional_usdc=notional_usdc,
        price=price,
        side=side,
        wallet_age_hours=max(0.0, age),
        prior_markets=prior_markets,
        market_share=share,
        market_cumulative_usdc=in_market,
        market_longshot_usdc=cheap_in_market,
        event_cumulative_usdc=event_cum,
        event_markets=len(event_markets),
        event_share=(event_cum / total) if total > 0 and event_slug else None,
        longshot_markets=len(cost),
        longshot_wins=wins,
    )
