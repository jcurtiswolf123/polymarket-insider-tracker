"""fav_live tick end to end with Kalshi, Polymarket and Twilio faked: buy, dedupe, caps, settle, texts, timeout recovery."""
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import fav_live as fl  # noqa: E402

T = "KXNBAGAME-26NOV20BOSNYK-BOS"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    for k, v in {"LEDGER": tmp_path / "l.jsonl", "STATE": tmp_path / "s.json", "OFF": tmp_path / "OFF"}.items():
        monkeypatch.setattr(fl, k, v)
    fl.STATE.write_text(json.dumps({"seen": 0, "alerts_seen": 0}))
    w = {"fav": [], "alerts": [], "texts": [], "orders": [], "result": None, "post": "fill"}
    monkeypatch.setattr(fl.fav_lane, "ledger_rows", lambda: w["fav"])
    monkeypatch.setattr(fl.kc, "rows", lambda: w["alerts"])
    monkeypatch.setattr(fl.ct, "market_for", lambda c: {"gameStartTime": "2099-01-01 00:00:00+00"})
    monkeypatch.setattr(fl.kc, "match_kalshi", lambda pm, o: ({"ticker": T, "yes_ask_dollars": 0.6}, ""))
    monkeypatch.setattr(fl.kc, "get", lambda url: {"market": {"exchange_index": 3, "result": w["result"]}})
    monkeypatch.setattr(fl.sms_alerts, "send_sms", lambda body, dry: w["texts"].append(body) or "SMfake")
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl, "LANE_BETS", True)

    def call(method, path, body=None):
        if path == "/portfolio/balance":
            return {"balance": 240000, "portfolio_value": 0, "balance_breakdown": [{"exchange_index": 3, "balance": "2400"}]}
        if method == "POST":
            w["orders"].append(body)
            if w["post"] == "timeout":
                raise urllib.error.URLError("timed out")
            return {"order_id": "o1", "fill_count": body["count"], "average_fill_price": "0.62", "average_fee_paid": "0.0164"}
        if path.startswith("/portfolio/orders"):
            o = w["orders"][-1]
            return {"orders": [{"client_order_id": o["client_order_id"], "order_id": "o2", "fill_count_fp": "312.00",
                                "taker_fill_cost_dollars": "193.44", "maker_fill_cost_dollars": "0",
                                "taker_fees_dollars": "5.10", "maker_fees_dollars": "0"}]}
        raise AssertionError(path)

    monkeypatch.setattr(fl, "call", call)
    return w


def sig(outcome="Boston Celtics", px=0.6):
    return {"cond": "0xabc", "outcome": outcome, "title": "Celtics vs. Knicks", "insider_px": px, "usdc": 5000}


def ledger():
    return fl.rows()


def test_buys_then_dedupes_game_then_settles_and_texts(bot):
    bot["fav"] = [sig()]
    fl.tick(False)
    (r,) = ledger()
    assert r["status"] == "open" and r["kalshi"] == T and r["limit"] == 0.64
    assert bot["orders"][0]["count"] == "312.00" and bot["orders"][0]["price"] == "0.6400"
    assert r["cost"] == round(312 * 0.62, 2) and bot["texts"] == []  # no buy text

    bot["fav"].append(sig())  # same game again: no second order
    fl.tick(False)
    assert len(bot["orders"]) == 1 and ledger()[-1]["reason"] == "already bet this game"

    bot["result"] = "yes"
    fl.tick(False)
    won = ledger()[0]
    assert won["status"] == "won" and won["pnl"] == round(312 - won["cost"] - won["fee"], 2)
    assert bot["texts"] and bot["texts"][-1].startswith("Kalshi\nWON Boston Celtics +$") and "Balance $2,400" in bot["texts"][-1]


def test_timeout_that_filled_is_recovered_not_skipped(bot):
    bot["post"] = "timeout"
    bot["fav"] = [sig()]
    fl.tick(False)
    (r,) = ledger()
    assert r["status"] == "open" and r["contracts"] == 312 and r["px"] == pytest.approx(0.62) and r["fee"] == 5.1


def test_loss_cap_stops_and_texts(bot):
    fl.LEDGER.write_text(json.dumps({"status": "lost", "pnl": -900.0}) + "\n")
    bot["fav"] = [sig()]
    fl.tick(False)
    assert ledger()[-1]["reason"].startswith("loss cap") and bot["orders"] == []
    assert "BOT STOPPED: loss cap" in bot["texts"][-1]


def test_kill_switch(bot):
    fl.OFF.touch()
    bot["fav"] = [sig()]
    fl.tick(False)
    assert ledger()[-1]["reason"] == "LIVE_OFF" and bot["orders"] == [] and bot["texts"] == []


def test_alert_signal_uses_matched_ticker_and_price_cap(bot):
    bot["alerts"] = [{"key": "x|0xdef", "status": "open", "outcome": "Boston Celtics", "title": "t",
                      "leader_px": 0.88, "kalshi": T, "ask": 0.87}]
    fl.tick(False)
    assert ledger()[-1]["limit"] == 0.90 and bot["orders"][0]["count"] == "222.00"


def test_lane_rows_ignored_when_alerts_only(bot, monkeypatch):
    monkeypatch.setattr(fl, "LANE_BETS", False)
    bot["fav"] = [sig()]
    fl.tick(False)
    assert ledger() == [] and bot["orders"] == [] and json.loads(fl.STATE.read_text())["seen"] == 1
