"""Named insider-pattern rules (local fork).

Each rule looks at one trade plus the trading wallet's history up to that trade and fires on
its own. Any fired rule makes the trade alert, so the net stays wide: a deep market where size
never looks anomalous is still caught by a wallet-side rule, and an old wallet is still caught
by a long-shot or concentration rule.

The same ``evaluate`` runs live (pipeline) and in the historical backtest
(``scripts/backtest_insider_cases.py``), so a backtest result is a statement about live behavior.
Thresholds come from the environment so they can be tuned without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


def _env_dec(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, "").strip() or default
    try:
        return Decimal(raw)
    except ArithmeticError:
        return Decimal(default)


@dataclass(frozen=True)
class RuleThresholds:
    """Every tunable number in one place. Defaults were set against the backtest set."""

    # R1 brand-new wallet, big bet
    fresh_hours: Decimal = field(default_factory=lambda: _env_dec("RULE_FRESH_HOURS", "48"))
    fresh_big_usdc: Decimal = field(default_factory=lambda: _env_dec("FRESH_BIG_BET_USDC", "10000"))
    # R2 young wallet with almost no history, medium bet
    young_days: Decimal = field(default_factory=lambda: _env_dec("RULE_YOUNG_DAYS", "14"))
    young_max_markets: int = field(
        default_factory=lambda: int(_env_dec("RULE_YOUNG_MAX_MARKETS", "3"))
    )
    young_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_YOUNG_USDC", "2000"))
    # R3 long-shot conviction: buying an unlikely outcome hard
    longshot_price: Decimal = field(default_factory=lambda: _env_dec("RULE_LONGSHOT_PRICE", "0.25"))
    longshot_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_LONGSHOT_USDC", "5000"))
    longshot_max_markets: int = field(
        default_factory=lambda: int(_env_dec("RULE_LONGSHOT_MAX_MARKETS", "10"))
    )
    # R4 single-purpose wallet: nearly all of its money in this one market
    focus_share: Decimal = field(default_factory=lambda: _env_dec("RULE_FOCUS_SHARE", "0.8"))
    focus_max_markets: int = field(
        default_factory=lambda: int(_env_dec("RULE_FOCUS_MAX_MARKETS", "3"))
    )
    focus_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_FOCUS_USDC", "5000"))
    # R5 whale-sized bet from a wallet with a thin history (any price)
    thin_max_markets: int = field(
        default_factory=lambda: int(_env_dec("RULE_THIN_MAX_MARKETS", "5"))
    )
    thin_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_THIN_USDC", "25000"))
    # R6 whale long shot: a large stake on an unlikely outcome from any wallet
    whale_longshot_price: Decimal = field(
        default_factory=lambda: _env_dec("RULE_WHALE_LONGSHOT_PRICE", "0.30")
    )
    whale_longshot_usdc: Decimal = field(
        default_factory=lambda: _env_dec("RULE_WHALE_LONGSHOT_USDC", "25000")
    )
    # R7 proven long-shot hitter: wins on earlier long shots, buying another one
    sharp_price: Decimal = field(default_factory=lambda: _env_dec("RULE_SHARP_PRICE", "0.30"))
    sharp_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_SHARP_USDC", "5000"))
    sharp_min_wins: int = field(default_factory=lambda: int(_env_dec("RULE_SHARP_MIN_WINS", "3")))
    sharp_min_rate: Decimal = field(default_factory=lambda: _env_dec("RULE_SHARP_MIN_RATE", "0.4"))
    # R8 event sweep: heavy money spread over several markets of one event
    sweep_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_SWEEP_USDC", "50000"))
    sweep_min_markets: int = field(
        default_factory=lambda: int(_env_dec("RULE_SWEEP_MIN_MARKETS", "3"))
    )
    sweep_share: Decimal = field(default_factory=lambda: _env_dec("RULE_SWEEP_SHARE", "0.3"))
    # Ceiling: buys at or above this price are near-certain "bond" carry, not information edge
    max_price: Decimal = field(default_factory=lambda: _env_dec("RULE_MAX_PRICE", "0.93"))
    # Floor: rules never look at a trade below this (keeps noise and load down)
    min_usdc: Decimal = field(default_factory=lambda: _env_dec("RULE_MIN_USDC", "1000"))


@dataclass(frozen=True)
class TradeFeatures:
    """One BUY trade and the wallet's history strictly before it."""

    notional_usdc: Decimal
    price: Decimal
    side: str  # "BUY" or "SELL"
    wallet_age_hours: float | None  # since the wallet's first Polymarket activity
    prior_markets: int  # distinct markets traded before this trade
    # Share of the wallet's lifetime traded USDC (through this trade) that sits in this market.
    market_share: Decimal | None = None
    # Cumulative USDC this wallet has put into this market through this trade.
    market_cumulative_usdc: Decimal | None = None
    # Earlier long-shot markets (bought at <= 0.30) and how many of them paid out more than cost.
    # USDC this wallet put into this market at long-shot prices (<= 0.30), through this trade.
    market_longshot_usdc: Decimal | None = None
    longshot_markets: int = 0
    longshot_wins: int = 0
    # The wallet's buys across every market of this trade's event (e.g. all "Year in Search" legs).
    event_cumulative_usdc: Decimal | None = None
    event_markets: int = 0
    event_share: Decimal | None = None


def evaluate(f: TradeFeatures, t: RuleThresholds | None = None) -> list[str]:
    """Names of the rules this trade fires; empty when none do."""
    t = t or RuleThresholds()
    if f.side.upper() != "BUY" or f.price >= t.max_price:
        return []
    stake = max(f.notional_usdc, f.market_cumulative_usdc or Decimal(0))
    if stake < t.min_usdc:
        return []
    fired: list[str] = []
    age = f.wallet_age_hours
    if age is not None and age <= float(t.fresh_hours) and stake >= t.fresh_big_usdc:
        fired.append("R1_fresh_big_bet")
    if (
        age is not None
        and age <= float(t.young_days) * 24
        and f.prior_markets <= t.young_max_markets
        and stake >= t.young_usdc
    ):
        fired.append("R2_young_thin_wallet")
    if (
        f.price <= t.longshot_price
        and stake >= t.longshot_usdc
        and f.prior_markets <= t.longshot_max_markets
    ):
        fired.append("R3_longshot_conviction")
    if (
        f.market_share is not None
        and f.market_share >= t.focus_share
        and f.prior_markets <= t.focus_max_markets
        and stake >= t.focus_usdc
    ):
        fired.append("R4_single_purpose_wallet")
    if f.prior_markets <= t.thin_max_markets and stake >= t.thin_usdc:
        fired.append("R5_whale_thin_history")
    cheap_stake = max(f.notional_usdc, f.market_longshot_usdc or Decimal(0))
    if f.price <= t.whale_longshot_price and cheap_stake >= t.whale_longshot_usdc:
        fired.append("R6_whale_longshot")
    if (
        f.price <= t.sharp_price
        and stake >= t.sharp_usdc
        and f.longshot_wins >= t.sharp_min_wins
        and f.longshot_markets
        and Decimal(f.longshot_wins) / Decimal(f.longshot_markets) >= t.sharp_min_rate
    ):
        fired.append("R7_proven_longshot_hitter")
    if (
        f.event_cumulative_usdc is not None
        and f.event_cumulative_usdc >= t.sweep_usdc
        and f.event_markets >= t.sweep_min_markets
        and f.event_share is not None
        and f.event_share >= t.sweep_share
    ):
        fired.append("R8_event_sweep")
    return fired
