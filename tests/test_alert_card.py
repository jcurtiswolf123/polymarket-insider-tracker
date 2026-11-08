"""alert_card.recommend: model edge cap, long-horizon fallback."""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import alert_card  # noqa: E402

META = {
    "promoted": True,
    "edge_ge_0.10": {"groups": 91.5, "roi": 0.027},
    "edge_ge_0.15": {"groups": 11.0, "roi": -0.361},
}


def _end(days: float) -> str:
    t = datetime.fromtimestamp(time.time() + days * 86400, tz=UTC)
    return t.isoformat().replace("+00:00", "Z")


def _setup(monkeypatch, tmp_path, p_win, days, meta=META, track=None):
    mp = tmp_path / "meta.json"
    mp.write_text(json.dumps(meta))
    monkeypatch.setattr(alert_card, "META_PATH", mp)
    monkeypatch.setattr(alert_card, "_market", lambda *_: {"endDate": _end(days)})
    monkeypatch.setattr(alert_card, "_feature_row", lambda *_: {"price": 0.6})
    monkeypatch.setattr(alert_card, "shadow_p", lambda *_: None)
    monkeypatch.setattr(alert_card, "track_record", lambda *_: track)
    model = SimpleNamespace(
        promoted=True, score=lambda *_: SimpleNamespace(p_win=p_win, version="v1")
    )
    monkeypatch.setattr(alert_card, "LearnedModel", lambda: model)
    return {"price": 0.6, "trade_id": "t", "market": "m", "rules": ["M1_model_edge"]}


def test_edge_cap_from_meta():
    assert alert_card.model_edge_cap(META) == 0.10
    assert alert_card.model_edge_cap({"edge_ge_0.10": {"groups": 91, "roi": -0.1}}) == 0.0


def test_model_edge_capped(monkeypatch, tmp_path):
    rec = alert_card.recommend(_setup(monkeypatch, tmp_path, 0.99, days=2))
    assert abs(rec["p"] - 0.70) < 1e-9
    assert "capped" in rec["basis"]


def test_long_horizon_skips_model(monkeypatch, tmp_path):
    track = (
        "all flagged trades at this price",
        {"win_rate": 0.87, "avg_price": 0.73, "groups": 30},
    )
    rec = alert_card.recommend(_setup(monkeypatch, tmp_path, 0.99, days=700, track=track))
    assert rec["basis"].startswith("history")
    assert abs(rec["p"] - (0.6 + 0.14 * 0.5)) < 1e-6


def test_no_paying_edge_means_no_model(monkeypatch, tmp_path):
    bad = {"promoted": True, "edge_ge_0.10": {"groups": 91, "roi": -0.1}}
    rec = alert_card.recommend(_setup(monkeypatch, tmp_path, 0.99, days=2, meta=bad))
    assert rec["p"] == 0.6 and rec["bet_usd"] == 0
