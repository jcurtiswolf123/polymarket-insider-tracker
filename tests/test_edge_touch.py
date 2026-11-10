import math
import sys

import numpy as np

sys.path.insert(0, "scripts/edge")
import touch


def mc_touch(S, B, sigma, T, mu, n=20000, steps=250, seed=3):
    """Monte Carlo with an exact Brownian-bridge crossing test per step, so discrete steps add no bias."""
    rng = np.random.default_rng(seed)
    dt = T / steps
    b = math.log(B / S)
    x = np.zeros(n)
    hit = np.zeros(n, bool)
    for _ in range(steps):
        nx = x + mu * dt + sigma * math.sqrt(dt) * rng.standard_normal(n)
        d0, d1 = b - x, b - nx
        cross = (d0 <= 0) | (d1 <= 0) if b > 0 else (d0 >= 0) | (d1 >= 0)
        pc = np.exp(-2 * d0 * d1 / (sigma**2 * dt))
        hit |= cross | (rng.random(n) < np.where(d0 * d1 > 0, pc, 1.0))
        x = nx
    return hit.mean()


def test_touch_formula_matches_monte_carlo():
    for S, B, sig, T, drift in [(100, 110, 0.5, 0.25, 0.0), (100, 110, 0.5, 0.25, None), (100, 85, 0.6, 0.1, None),
                                (100, 130, 0.8, 1.0, 0.3), (100, 70, 0.8, 1.0, -0.2)]:
        mu = -0.5 * sig * sig if drift is None else drift
        assert abs(touch.touch_prob(S, B, sig, T, drift) - mc_touch(S, B, sig, T, mu)) < 0.012


def test_touch_closed_form_driftless_and_edges():
    S, B, sig, T = 100, 120, 0.4, 0.5
    z = math.log(B / S) / (sig * math.sqrt(T))
    assert abs(touch.touch_prob(S, B, sig, T, 0.0) - 2 * touch.ncdf(-z)) < 1e-12
    assert touch.touch_prob(100, 100, 0.3, 0.1) == 1.0 and touch.touch_prob(100, 120, 0.3, 0.0) == 0.0
    assert touch.touch_prob(100, 110, 0.3, 0.1, dt=1 / 252) < touch.touch_prob(100, 110, 0.3, 0.1)


def test_fee():
    assert touch.kalshi_fee(322, 0.62) == 5.32
    assert touch.kalshi_fee(100, 0.5) == 1.75


def test_place_bet_picks_larger_edge_and_respects_threshold():
    assert touch.place_bet(0.30, 0.20, 0.19, 0.05, True)[:2] == ("yes", 0.20)
    assert touch.place_bet(0.10, 0.30, 0.29, 0.05, True)[:2] == ("no", 0.71)
    assert touch.place_bet(0.22, 0.20, 0.19, 0.05, True) is None
    assert touch.place_bet(0.99, 0.995, None, 0.05, True) is None  # outside the tradable band


def test_settle_math():
    w = touch.settle("yes", 0.25, 1, True)
    assert w["n"] == 800 and abs(w["cost"] - (200 + touch.kalshi_fee(800, 0.25))) < 1e-9 and abs(w["pnl"] - (800 - w["cost"])) < 1e-9
    lose = touch.settle("no", 0.75, 1, True)  # bought NO, event happened
    assert lose["win"] == 0 and abs(lose["pnl"] + lose["cost"]) < 1e-9
    assert touch.settle("yes", 0.5, 0, False)["cost"] == 200.0  # polymarket: no fee
    assert touch.settle("yes", 0.5, 1, False)["pnl"] == 200.0


def test_stats_roi_and_event_bootstrap():
    bs = [{"cost": 100.0, "pnl": 50.0, "win": 1, "event": "a"}, {"cost": 100.0, "pnl": -100.0, "win": 0, "event": "b"}]
    s = touch.stats(bs)
    assert s["n"] == 2 and abs(s["roi"] + 0.25) < 1e-12 and s["win"] == 0.5 and s["events"] == 2
    assert touch.stats([])["n"] == 0


def test_event_end_from_ticker():
    assert touch.event_end("KXBTCMINMON-BTC-26NOV30", "BTC") == 1796097599.0  # 2026-12-01 03:59:59Z
    assert touch.event_end("KXWTIMIN-26SEP30", "WTI") == 1790793000.0  # 2026-09-30 18:30Z
    assert touch.event_end("KXWTIMAX", "WTI") is None
