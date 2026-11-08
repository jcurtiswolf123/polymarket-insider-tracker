"""kalshi_arb ladder: only same-subject legs pair up."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kalshi_arb as k  # noqa: E402


def leg(name, yes_ask, no_ask):
    return {"name": name, "yes_ask": yes_ask, "no_ask": no_ask, "yes_ask_sz": 10, "yes_bid_sz": 10}


def test_cross_team_legs_never_pair():
    ls = [
        leg("Las Vegas wins by over 3.5 points", 0.10, 0.9),
        leg("San Francisco wins by over 4.5 points", 0.7, 0.33),
    ]
    assert k.check_ladder({}, ls) == []


def test_same_subject_mispriced_ladder_found():
    # Yes(>3.5) + No(>4.5) pays >= 1 in every outcome; priced at 0.30 + 0.60 + fees
    ls = [
        leg("Seattle wins by over 3.5 points", 0.30, 0.71),
        leg("Seattle wins by over 4.5 points", 0.45, 0.60),
    ]
    out = k.check_ladder({}, ls)
    assert len(out) == 1 and out[0]["edge"] > 0
