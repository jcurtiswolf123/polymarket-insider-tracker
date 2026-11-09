import sys; sys.path.insert(0, "scripts")
from kalshi_copy import parse_slug, kalshi_fee, leader

def test_parse_slug():
    assert parse_slug("nba-ind-dal-2026-11-09") == ("nba", "IND", "DAL", "26NOV09")
    assert parse_slug("will-x-win-2028") is None

def test_fee():
    assert kalshi_fee(322, 0.62) == 5.32  # 0.07*322*0.62*0.38 = 5.31 -> rounded up

def test_leader():
    assert leader("0xabc", "Polywatch: Pacers vs. Mavericks\\nWallet bought Mavericks @0.60 $1,344") == ("0xabc", "Mavericks", 0.60)
    assert leader("0xw|0xc", "Polywatch: BUY Yes @0.35 $23,259")[:2] == ("0xc", "Yes")
