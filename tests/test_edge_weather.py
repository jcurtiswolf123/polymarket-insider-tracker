from datetime import date

import pytest

from scripts.edge import weather as w


def test_brackets_sum_to_one():
    ms = [{"strike_type": "less", "cap_strike": 61}]
    ms += [{"strike_type": "between", "floor_strike": a, "cap_strike": a + 1} for a in (61, 63, 65, 67)]
    ms += [{"strike_type": "greater", "floor_strike": 68}]
    assert sum(w.prob(m, 64.3, 2.1) for m in ms) == pytest.approx(1.0)


def test_greater_is_strict():
    m = {"strike_type": "greater", "floor_strike": 68}
    assert w.prob(m, 68.0, 1.5) < 0.5 < w.prob(m, 69.0, 1.5)


def test_fee_and_settle():
    assert w.kalshi_fee(100, 0.5) == 1.75
    c = int(200 // 0.40)
    assert w.settle("yes", 0.40, True) == pytest.approx(c - c * 0.40 - w.kalshi_fee(c, 0.40))
    assert w.settle("no", 0.40, True) == pytest.approx(-c * 0.40 - w.kalshi_fee(c, 0.40))


def test_bet_sides():
    assert w.bet(0.60, 0.45, 0.43, 0.10) == ("yes", 0.45)
    assert w.bet(0.20, 0.45, 0.40, 0.10) == ("no", 0.60)
    assert w.bet(0.50, 0.45, 0.43, 0.10) is None
    assert w.bet(0.99, 0.99, 0.0, 0.01) is None  # dead book


def test_calibration_uses_only_past():
    d = date(2026, 10, 1)
    days = [date.fromordinal(d.toordinal() - i).isoformat() for i in range(0, 60)]
    fc = {m: {x: 70.0 for x in days} for m in w.MODELS}
    cli = {x: 72.0 for x in days}
    cli[d.isoformat()] = 100.0  # truth on the day itself must not leak
    cli[days[1]] = 100.0  # nor the day before
    mu, sd = w.mu_sd(fc, cli, d)
    assert mu == pytest.approx(72.0)
    assert sd == w.MIN_SD


def test_event_date():
    assert w.event_date("KXHIGHNY-26NOV08") == date(2026, 11, 8)
