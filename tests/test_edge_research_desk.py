import pytest

from scripts.edge import research_desk as rd


def test_parse_p_takes_last_valid_json():
    t = 'thinking {"p": 2} more\n{"p": 0.31, "base_rate": 0.2, "sources": ["https://x"], "why": "w"}'
    assert rd.parse_p(t)["p"] == 0.31
    assert rd.parse_p("no json here") is None


def test_paper_sides_and_fee():
    yes = {"p": 0.70, "ask": 0.50, "bid": 0.48, "won": 1}
    pnl, staked = rd.paper(yes)
    c = int(200 // 0.50)
    assert staked == pytest.approx(c * 0.50)
    assert pnl == pytest.approx(c - c * 0.50 - rd.kalshi_fee(c, 0.50))
    no = {"p": 0.20, "ask": 0.45, "bid": 0.43, "won": 1}
    pnl, staked = rd.paper(no)
    c = int(200 // 0.57)
    assert pnl == pytest.approx(-c * 0.57 - rd.kalshi_fee(c, 0.57))
    assert rd.paper({"p": 0.50, "ask": 0.52, "bid": 0.48, "won": 0}) is None


def test_prompt_has_no_price_slot():
    assert "{ask" not in rd.PROMPT and "price for this question" in rd.PROMPT
