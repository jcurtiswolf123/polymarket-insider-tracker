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


def test_total_over_matches_line(monkeypatch):
    ev = {"event_ticker": "KXNBATOTAL-26NOV11GSWHOU", "markets": [
        {"ticker": "KXNBATOTAL-26NOV11GSWHOU-205", "floor_strike": 204.5},
        {"ticker": "KXNBATOTAL-26NOV11GSWHOU-207", "floor_strike": 206.5}]}
    monkeypatch.setattr(kalshi_copy, "events", lambda s, st: [ev] if s == "KXNBATOTAL" else [])
    pm = {"sportsMarketType": "totals", "slug": "nba-gsw-hou-2026-11-11-total-206pt5", "line": 206.5, "outcomes": '["Over", "Under"]'}
    assert match_kalshi(pm, "Over")[0]["ticker"].endswith("-207")
    assert match_kalshi(pm, "Under")[0] is None
    assert match_kalshi({**pm, "line": 210.5}, "Over")[0] is None


def test_map_winner_by_name(monkeypatch):
    ev = {"event_ticker": "KXCS2MAP-26NOV110200LGCFUR-1", "markets": [
        {"ticker": "KXCS2MAP-26NOV110200LGCFUR-1-LGC", "yes_sub_title": "Legacy"},
        {"ticker": "KXCS2MAP-26NOV110200LGCFUR-1-FUR", "yes_sub_title": "FURIA"}]}
    ev2 = {**ev, "event_ticker": "KXCS2MAP-26NOV110200LGCFUR-2"}
    monkeypatch.setattr(kalshi_copy, "events", lambda s, st: [ev2, ev] if s == "KXCS2MAP" else [])
    pm = {"sportsMarketType": "child_moneyline", "slug": "cs2-lgc-furia-2026-11-10-game1", "outcomes": '["Legacy", "FURIA"]'}
    mk, how = match_kalshi(pm, "FURIA")
    assert mk["ticker"] == "KXCS2MAP-26NOV110200LGCFUR-1-FUR" and how == "map"
    assert match_kalshi({**pm, "slug": "cs2-lgc-furia-2026-11-10-game3"}, "FURIA")[0] is None
