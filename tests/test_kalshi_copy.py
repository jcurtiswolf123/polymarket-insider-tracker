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

from kalshi_copy import same_team, near_dates, match_kalshi
import kalshi_copy

def test_same_team():
    assert same_team("Gen.G", "GEN.G") and same_team("Bilibili Gaming", "Bilibili")
    assert not same_team("Dallas", "Mavericks") and not same_team("", "x")

def test_near_dates():
    assert near_dates("26NOV01") == ["26NOV01", "26OCT31", "26NOV02"]

def test_name_fallback(monkeypatch):
    ev = {"event_ticker": "KXLOLGAME-26NOV141400GENBLG", "markets": [
        {"ticker": "KXLOLGAME-26NOV141400GENBLG-GEN", "yes_sub_title": "Gen.G"},
        {"ticker": "KXLOLGAME-26NOV141400GENBLG-BLG", "yes_sub_title": "Bilibili Gaming"}]}
    monkeypatch.setattr(kalshi_copy, "events", lambda s, st: [ev])
    pm = {"sportsMarketType": "moneyline", "slug": "lol-bilibili-geng-2026-11-14", "outcomes": '["Bilibili Gaming", "Gen.G"]'}
    mk, how = match_kalshi(pm, "Gen.G")
    assert mk["ticker"].endswith("-GEN") and how == "name"
