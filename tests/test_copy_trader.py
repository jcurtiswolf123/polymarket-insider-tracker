import importlib.util
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "copy_trader", Path(__file__).parents[1] / "scripts" / "copy_trader.py"
)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)
CFG = dict(ct.DEFAULTS)
NOW = time.time()
MKT = {"closed": False, "acceptingOrders": True, "endDate": "2099-01-01T00:00:00Z"}


def hit(**kw):
    base = {
        "rules": ["R1_fresh_big_bet"],
        "usdc": "13319",
        "price": "0.6",
        "slug": "who-will-be-the-next-prime-minister-of-israel",
        "wallet": "0xb5",
        "market": "0xdb",
    }
    return {**base, **kw}


def test_strong_rule_copies():
    assert ct.filter_reason(hit(), MKT, CFG, NOW) is None


def test_single_weak_rule_skipped():
    assert "weak" in ct.filter_reason(hit(rules=["R2_young_thin_wallet"]), MKT, CFG, NOW)


def test_dated_sports_match_skipped():
    assert "excluded" in ct.filter_reason(hit(slug="ucl-psv-bru-2026-11-04-psv"), MKT, CFG, NOW)


def test_priced_in_skipped():
    assert "price" in ct.filter_reason(hit(price="0.946"), MKT, CFG, NOW)


def test_closed_market_skipped():
    assert ct.filter_reason(hit(), {**MKT, "closed": True}, CFG, NOW) == "market closed"


def test_caps():
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    rows = [{"ts": day + "T00:00:00", "cond": "0xdb", "cost": 60.0}]
    assert ct.stake_for("0xdb", CFG, rows, NOW) == 15.0  # per-market cap 75 - 60
    assert ct.stake_for("0xother", CFG, rows, NOW) == 25.0


def test_walk_book_respects_limit():
    asks = [{"price": "0.61", "size": "10"}, {"price": "0.70", "size": "1000"}]
    shares, cost = ct.walk_book(asks, 25, 0.63)
    assert shares == 10 and cost == 6.1
