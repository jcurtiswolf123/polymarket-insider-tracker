"""Local fork: named insider rules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_insider_tracker.detector.insider_rules import (
    RuleThresholds,
    TradeFeatures,
    evaluate,
)
from polymarket_insider_tracker.profiler.wallet_history import (
    HistoryTrade,
    WalletHistory,
    features_for,
)

T = RuleThresholds(
    fresh_hours=Decimal(48),
    fresh_big_usdc=Decimal(10000),
    young_days=Decimal(14),
    young_max_markets=3,
    young_usdc=Decimal(2000),
    longshot_price=Decimal("0.25"),
    longshot_usdc=Decimal(5000),
    longshot_max_markets=10,
    focus_share=Decimal("0.8"),
    focus_max_markets=3,
    focus_usdc=Decimal(5000),
    thin_max_markets=5,
    thin_usdc=Decimal(25000),
    max_price=Decimal("0.93"),
    min_usdc=Decimal(1000),
)


def feat(**kw: object) -> TradeFeatures:
    base: dict[str, object] = {
        "notional_usdc": Decimal(1000),
        "price": Decimal("0.5"),
        "side": "BUY",
        "wallet_age_hours": 5000.0,
        "prior_markets": 200,
        "market_share": Decimal("0.01"),
        "market_cumulative_usdc": None,
    }
    base.update(kw)
    return TradeFeatures(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        ({"wallet_age_hours": 3.0, "notional_usdc": Decimal(12000)}, "R1_fresh_big_bet"),
        (
            {"wallet_age_hours": 100.0, "prior_markets": 1, "notional_usdc": Decimal(2500)},
            "R2_young_thin_wallet",
        ),
        (
            {"price": Decimal("0.08"), "prior_markets": 4, "notional_usdc": Decimal(6000)},
            "R3_longshot_conviction",
        ),
        (
            {"market_share": Decimal("0.95"), "prior_markets": 2, "notional_usdc": Decimal(6000)},
            "R4_single_purpose_wallet",
        ),
        ({"prior_markets": 3, "notional_usdc": Decimal(30000)}, "R5_whale_thin_history"),
    ],
)
def test_each_rule_fires(kw: dict[str, object], rule: str) -> None:
    assert rule in evaluate(feat(**kw), T)


def test_ordinary_trader_fires_nothing() -> None:
    assert evaluate(feat(notional_usdc=Decimal(40000)), T) == []


def test_sells_and_near_certain_buys_are_ignored() -> None:
    assert evaluate(feat(side="SELL", wallet_age_hours=1.0, notional_usdc=Decimal(50000)), T) == []
    assert (
        evaluate(feat(price=Decimal("0.97"), wallet_age_hours=1.0, notional_usdc=Decimal(50000)), T)
        == []
    )


def test_split_fills_count_by_cumulative_stake() -> None:
    f = feat(
        wallet_age_hours=2.0, notional_usdc=Decimal(1500), market_cumulative_usdc=Decimal(11000)
    )
    assert "R1_fresh_big_bet" in evaluate(f, T)


def test_features_use_only_prior_history() -> None:
    m = "0xmarket"
    hist = WalletHistory(
        "0xw",
        1000,
        (
            HistoryTrade(1000, "0xother", "BUY", Decimal("0.5"), Decimal(100), "a"),
            HistoryTrade(5000, m, "BUY", Decimal("0.1"), Decimal(4000), "b"),
            HistoryTrade(9000, "0xlater", "BUY", Decimal("0.5"), Decimal(100), "c"),
        ),
        False,
    )
    f = features_for(
        hist,
        condition_id=m,
        trade_ts=5000,
        notional_usdc=Decimal(4000),
        price=Decimal("0.1"),
        side="BUY",
    )
    assert f.prior_markets == 1
    assert f.market_cumulative_usdc == Decimal(4000)
    assert f.market_share == Decimal(4000) / Decimal(4100)
    assert f.wallet_age_hours == pytest.approx(4000 / 3600)


def test_whale_longshot_counts_only_cheap_money() -> None:
    big_cheap = feat(
        price=Decimal("0.15"), notional_usdc=Decimal(2000), market_longshot_usdc=Decimal(30000)
    )
    assert "R6_whale_longshot" in evaluate(big_cheap, T)
    whale_adding_cheap_fill = feat(
        price=Decimal("0.29"),
        notional_usdc=Decimal(1124),
        market_cumulative_usdc=Decimal(900000),
        market_longshot_usdc=Decimal(1124),
    )
    assert "R6_whale_longshot" not in evaluate(whale_adding_cheap_fill, T)


def test_proven_longshot_hitter() -> None:
    f = feat(price=Decimal("0.2"), notional_usdc=Decimal(6000), longshot_markets=6, longshot_wins=3)
    assert "R7_proven_longshot_hitter" in evaluate(f, T)
    assert "R7_proven_longshot_hitter" not in evaluate(
        feat(
            price=Decimal("0.2"), notional_usdc=Decimal(6000), longshot_markets=20, longshot_wins=3
        ),
        T,
    )


def test_event_sweep() -> None:
    f = feat(event_cumulative_usdc=Decimal(60000), event_markets=4, event_share=Decimal("0.35"))
    assert "R8_event_sweep" in evaluate(f, T)
    assert "R8_event_sweep" not in evaluate(
        feat(event_cumulative_usdc=Decimal(60000), event_markets=4, event_share=Decimal("0.05")), T
    )
