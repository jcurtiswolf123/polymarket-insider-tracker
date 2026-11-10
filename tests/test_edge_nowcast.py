import math
from datetime import date, timedelta

import pytest

from scripts.edge import nowcast as nc


def test_fee_rounds_up_to_the_cent():
    assert nc.kalshi_fee(100, 0.5) == 1.75
    assert nc.kalshi_fee(10, 0.5) == 0.18  # 0.175 rounds up
    assert nc.kalshi_fee(100, 0.01) == 0.07


def test_settles_yes_strictness_and_types():
    assert not nc.settles_yes("greater", 2.8e6, None, 2.8e6)
    assert nc.settles_yes("greater_or_equal", 2.8e6, None, 2.8e6)
    assert nc.settles_yes("less", None, 4.0, 3.99)
    assert nc.settles_yes("between", 3.0, 3.5, 3.2) and not nc.settles_yes("between", 3.0, 3.5, 3.6)


def test_bet_pnl_win_and_loss():
    cost, pnl = nc.bet_pnl(True, 0.40, True)  # 500 contracts
    assert cost == pytest.approx(500 * 0.4 + nc.kalshi_fee(500, 0.4))
    assert pnl == pytest.approx(500 - cost)
    assert nc.bet_pnl(False, 0.40, True)[1] == pytest.approx(-cost)


def _tsa(ratio=1.1):
    ly0 = date(2024, 1, 1)
    base = {ly0 + timedelta(days=i): 1000 + 10 * (i % 7) for i in range(800)}
    return {**base, **{d + timedelta(days=364): v * ratio for d, v in base.items()}}


def test_tsa_forecast_scales_prior_year_by_trailing_ratio():
    d = _tsa(1.1)
    monday = date(2025, 6, 2)
    ly = sum(d[monday - timedelta(days=364) + timedelta(days=i)] for i in range(7)) / 7
    assert nc.tsa_forecast(d, monday, 3) == pytest.approx(ly * 1.1)


def test_tsa_forecast_uses_published_days_and_missing_data_is_none():
    d = _tsa(1.0)
    monday = date(2025, 6, 2)
    d[monday] = 5000
    ly = lambda x: d[x - timedelta(days=364)]
    r7 = sum(d[monday - timedelta(days=i)] for i in range(7)) / sum(ly(monday - timedelta(days=i)) for i in range(7))
    r14 = sum(d[monday - timedelta(days=i)] for i in range(14)) / sum(ly(monday - timedelta(days=i)) for i in range(14))
    want = (5000 + sum(ly(monday + timedelta(days=i)) * (r7 + r14) / 2 for i in range(1, 7))) / 7
    assert nc.tsa_forecast(d, monday, 1) == pytest.approx(want)
    del d[monday - timedelta(days=364) + timedelta(days=3)]
    assert nc.tsa_forecast(d, monday, 1) is None


def test_parse_tsa_and_event_date():
    html = "<td>11/8/2026</td><td>2,749,897</td><tr><td class='x'>11/7/2026</td><td class='x'>2,005,258</td>"
    assert nc.parse_tsa(html)[date(2026, 11, 8)] == 2749897
    assert nc.parse_event_date("KXTSAW-26NOV08") == date(2026, 11, 8)


def test_exceed_prob_and_cond_prob():
    errs = [-0.02, -0.01, 0.0, 0.01, 0.02] * 6
    assert nc.exceed_prob(errs, 0.0) == pytest.approx(0.5, abs=0.02)
    assert nc.exceed_prob(errs, 1.0) < 1e-6 and nc.exceed_prob(errs, -1.0) > 0.999
    cdf = lambda x: 1 - nc.exceed_prob(errs, x)
    assert nc.cond_prob("greater", 0.0, None, cdf) + nc.cond_prob("less", None, 0.0, cdf) == pytest.approx(1.0)


def test_gas_forecast_drift_and_errors():
    g = {date(2026, 1, 1) + timedelta(days=i): 4.0 + 0.01 * i for i in range(60)}
    assert nc.gas_forecast(g, date(2026, 1, 30), 2) == pytest.approx(g[date(2026, 1, 30)] + 0.02)
    assert all(abs(e) < 1e-9 for e in nc.gas_errors(g, date(2026, 3, 1), 2))


def test_evaluate_picks_side_with_edge_and_respects_threshold():
    assert nc.evaluate(0.80, (0.60, 0.45), 0.05) == (True, 0.60, pytest.approx(0.20))
    assert nc.evaluate(0.20, (0.60, 0.45), 0.05)[0] is False
    assert nc.evaluate(0.62, (0.60, 0.45), 0.05) is None
    assert nc.evaluate(0.99, (0.99, None), 0.05) is None
