"""fav_lane: lane gate and paper-book summary."""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fav_lane  # noqa: E402

HIT = {"price": "0.62", "usdc": "2300", "age_h": 30, "rules": ["C1_fresh_cluster"]}


def _market(hours: float, closed: bool = False, prices=("1", "0")) -> dict:
    end = datetime.fromtimestamp(time.time() + hours * 3600, tz=UTC).isoformat()
    return {
        "endDate": end.replace("+00:00", "Z"),
        "closed": closed,
        "outcomes": json.dumps(["A", "B"]),
        "outcomePrices": json.dumps(list(prices)),
    }


def test_prefilter():
    assert fav_lane.prefilter(HIT)
    assert not fav_lane.prefilter({**HIT, "price": "0.55"})
    assert not fav_lane.prefilter({**HIT, "price": "0.85"})
    assert not fav_lane.prefilter({**HIT, "age_h": 400})
    assert not fav_lane.prefilter({**HIT, "usdc": "500"})
    assert not fav_lane.prefilter({**HIT, "rules": ["M1_model_edge"]})


def test_in_lane_needs_short_horizon():
    assert fav_lane.in_lane(HIT, _market(20))
    assert not fav_lane.in_lane(HIT, _market(24 * 30))
    assert not fav_lane.in_lane(HIT, _market(-1))
    assert not fav_lane.in_lane(HIT, {})


def test_summarize():
    rows = [
        {"cond": "w", "outcome": "A", "filled": True, "shares": 80, "cost": 50, "avg": 0.625},
        {"cond": "l", "outcome": "A", "filled": True, "shares": 80, "cost": 50, "avg": 0.625},
        {"cond": "o", "outcome": "A", "filled": True, "shares": 80, "cost": 50, "avg": 0.625},
        {"cond": "m", "outcome": "A", "filled": False, "shares": 0, "cost": 0, "avg": None},
    ]
    markets = {
        "w": _market(-5, True, ("1", "0")),
        "l": _market(-5, True, ("0", "1")),
        "o": _market(5),
    }
    s = fav_lane.summarize(rows, resolve=markets.get)
    assert (s["signals"], s["missed"], s["open"], s["resolved"]) == (4, 1, 1, 2)
    assert s["win_rate"] == 0.5 and abs(s["pnl"] - (80 - 100)) < 1e-9
    assert "too early" in fav_lane.report_text(s)


def test_game_hit_takes_old_wallets_and_model_only_hits():
    from fav_lane import game_hit, prefilter
    navi = {"price": 0.58, "usdc": 11600, "age_h": 5472, "hours_to_start": 0.5, "rules": ["M1_model_edge"]}
    assert game_hit(navi) and not prefilter(navi)
    assert not game_hit({**navi, "price": 0.27})          # DK handicap loss
    assert not game_hit({**navi, "hours_to_start": None})  # not a game
    assert not game_hit({**navi, "hours_to_start": 30})
    assert not game_hit({**navi, "usdc": 500})
    assert not game_hit({**navi, "rules": []})
