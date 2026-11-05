"""Local fork: active-trader suppression, Gamma timestamps, learned-model vectors."""

from __future__ import annotations

from decimal import Decimal

from polymarket_insider_tracker.detector.insider_rules import (
    RuleThresholds,
    TradeFeatures,
    evaluate,
    is_active_trader,
)
from polymarket_insider_tracker.detector.learned import FEATURE_NAMES, category, vectorize
from polymarket_insider_tracker.pipeline import _parse_ts
from polymarket_insider_tracker.profiler.wallet_history import (
    HistoryTrade,
    WalletHistory,
    features_for,
)

T = RuleThresholds(active_min_trades=20, active_rate=Decimal(8), active_min_markets=3)


def fresh(**kw: object) -> TradeFeatures:
    base: dict[str, object] = {
        "notional_usdc": Decimal(40000),
        "price": Decimal("0.6"),
        "side": "BUY",
        "wallet_age_hours": 3.0,
        "prior_markets": 1,
        "market_share": Decimal("0.9"),
    }
    base.update(kw)
    return TradeFeatures(**base)  # type: ignore[arg-type]


def test_fresh_wallet_with_few_trades_still_fires() -> None:
    fired = evaluate(fresh(prior_trades=3), T)
    assert "R1_fresh_big_bet" in fired and "R5_whale_thin_history" in fired


def test_rapid_fire_wallet_drops_freshness_rules() -> None:
    f = fresh(prior_trades=60, prior_markets=4)  # 60 tx in 3 hours = 20/h, 4 markets
    assert is_active_trader(f, T)
    fired = evaluate(f, T)
    assert not {
        "R1_fresh_big_bet",
        "R2_young_thin_wallet",
        "R4_single_purpose_wallet",
        "R5_whale_thin_history",
    } & set(fired)


def test_longshot_rules_survive_active_trader() -> None:
    f = fresh(prior_trades=60, prior_markets=4, price=Decimal("0.1"), notional_usdc=Decimal(30000))
    assert "R6_whale_longshot" in evaluate(f, T)


def test_slow_old_wallet_is_not_active() -> None:
    assert not is_active_trader(fresh(prior_trades=200, wallet_age_hours=5000.0), T)


def test_prior_trades_counts_distinct_transactions() -> None:
    fills = [
        HistoryTrade(100 + i // 3, "0xa", "BUY", Decimal("0.5"), Decimal(20), f"tx{i // 3}")
        for i in range(30)
    ] + [
        HistoryTrade(200 + i, "0xa", "BUY", Decimal("0.5"), Decimal(1), f"dust{i}")
        for i in range(50)
    ]
    hist = WalletHistory("0xw", 100, tuple(fills), False)
    f = features_for(
        hist,
        condition_id="0xb",
        trade_ts=1000,
        notional_usdc=Decimal(1),
        price=Decimal("0.5"),
        side="BUY",
    )
    assert f.prior_trades == 10


def test_padded_single_market_wallet_keeps_freshness_rules() -> None:
    # 500 tx in 3h but all in one market: the shape of an insider padding its count
    fired = evaluate(fresh(prior_trades=500, prior_markets=1), T)
    assert "R1_fresh_big_bet" in fired


def test_dropped_rules_are_reported() -> None:
    from polymarket_insider_tracker.detector.insider_rules import dropped_for_activity

    assert "R1_fresh_big_bet" in dropped_for_activity(fresh(prior_trades=60, prior_markets=4), T)


def test_parse_gamma_timestamps() -> None:
    assert _parse_ts("2026-11-05 01:30:00+00") == _parse_ts("2026-11-05T01:30:00Z")
    assert _parse_ts(None) is None and _parse_ts("garbage") is None


def test_vectorize_shape_and_categories() -> None:
    row = {
        "price": 0.3,
        "usdc": 5000,
        "age_h": 2,
        "prior_trades": 40,
        "title": "Heat vs. Clippers",
        "rules": ["W_R3_longshot_conviction"],
    }
    v = vectorize(row)
    assert len(v) == len(FEATURE_NAMES)
    named = dict(zip(FEATURE_NAMES, v, strict=True))
    assert named["cat_sports"] == 1.0 and named["rule_R3_longshot_conviction"] == 1.0
    assert named["trades_per_hour"] == 20.0
    assert category("LoL: Gen.G vs LYON")["cat_esports"] == 1.0
    assert category("LoL: Gen.G vs LYON")["cat_sports"] == 0.0
