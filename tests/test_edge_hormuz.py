import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "edge"))
import hormuz


def series(vals, start=date(2026, 1, 1)):
    return [(start + timedelta(i), v) for i, v in enumerate(vals)]


def test_moving_avg_basic():
    ma = hormuz.moving_avg(series([7] * 7 + [14]))
    assert ma[0][1] == 7 and ma[-1][1] == (7 * 6 + 14) / 7
    assert ma[-1][0] == date(2026, 1, 8)


def test_moving_avg_missing_day_counts_zero():
    s = series([7] * 10)
    del s[5]
    assert hormuz.moving_avg(s)[-1][1] == 7 * 6 / 7


def test_moving_avg_short_series():
    assert hormuz.moving_avg(series([1, 2, 3])) == []


def test_hit_prob_already_above():
    assert hormuz.hit_prob([61.0] * 20, 30)["p"] == 1.0


def test_hit_prob_flat_series_is_zero():
    assert hormuz.hit_prob([5.0] * 60, 50)["p"] == 0.0


def test_hit_prob_monotone_in_horizon_and_bounds():
    ma = [4 + 0.5 * math.sin(i / 3) for i in range(120)]
    p10, p50 = hormuz.hit_prob(ma, 10)["p"], hormuz.hit_prob(ma, 50)["p"]
    assert 0 <= p10 <= p50 <= 1


def test_hit_prob_closer_is_likelier():
    noisy = lambda base: [base * (1 + 0.3 * math.sin(i / 2)) for i in range(120)]
    assert hormuz.hit_prob(noisy(40), 50)["p"] > hormuz.hit_prob(noisy(5), 50)["p"]


def test_hit_prob_matches_reflection_formula_zero_drift():
    ma = [10 * math.exp(0.1 * math.sin(i)) for i in range(120)]
    r = hormuz.hit_prob(ma, 40)
    b = math.log(60) - math.log(ma[-1])
    expect = 2 * (1 - hormuz._phi(b / (r["sigma"] * math.sqrt(40))))
    if r["mu"] == 0:
        assert abs(r["p"] - expect) < 1e-9


def test_hit_prob_no_days_left():
    assert hormuz.hit_prob([5.0] * 30, 0)["p"] == 0.0
