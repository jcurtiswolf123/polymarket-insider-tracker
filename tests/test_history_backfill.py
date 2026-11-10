import sys; sys.path.insert(0, "scripts")
from history_backfill import trailing_volume, fresh_conf, size_conf

def test_trailing_volume():
    f = [(0, 100.0), (3600, 50.0), (90000, 10.0)]
    assert trailing_volume(f, False) == [0.0, 100.0, 50.0]  # third fill: first dropped out of 24h
    assert trailing_volume(f, True)[0] is None  # truncated market: window reaches past oldest fill

def test_fresh_conf():
    assert fresh_conf(0, 1.0, 20000) == 0.9   # 0.5 base + brand new + very young + large
    assert fresh_conf(10, 1.0, 20000) == 0.0  # too many prior trades
    assert fresh_conf(0, 100.0, 20000) == 0.0 # older than 48h
    assert fresh_conf(0, 1.0, 600) == 0.0     # under $1,000

def test_size_conf():
    assert size_conf(1000, None) == (0.0, 0)
    c, niche = size_conf(6000, 40000)  # 15% of a $40k day: saturated volume term x niche 1.5
    assert niche == 1 and c == 0.75
    assert size_conf(500, 1_000_000) == (0.0, 0)

from types import SimpleNamespace
from history_backfill import add_clusters, copy_roi, track_record

def test_clusters_count_other_fresh_same_side():
    rows = [{"ts": 0, "market": "m", "outcome_index": 0, "wallet": "a", "age_h": 1},
            {"ts": 100, "market": "m", "outcome_index": 0, "wallet": "b", "age_h": 1},
            {"ts": 200, "market": "m", "outcome_index": 1, "wallet": "c", "age_h": 1},   # other side
            {"ts": 300, "market": "m", "outcome_index": 0, "wallet": "d", "age_h": 900}, # old wallet
            {"ts": 30000, "market": "m", "outcome_index": 0, "wallet": "e", "age_h": 1}] # >6h later
    add_clusters(rows)
    assert [r["cluster6h"] for r in rows] == [0, 1, 0, 2, 0]

def test_copy_roi():
    assert abs(copy_roi(1, 0.5) - (1.0 - 0.035)) < 1e-9 and abs(copy_roi(0, 0.5) - (-1.035)) < 1e-9

def test_track_record_uses_only_markets_closed_before_trade():
    T = lambda ts, c, side, usdc: SimpleNamespace(timestamp=ts, condition_id=c, side=side, usdc=usdc)
    hist = SimpleNamespace(trades=[T(10, "won", "BUY", 100), T(10, "lost", "BUY", 100), T(10, "later", "BUY", 100)],
                           redeems=[(50, "won", 250)])
    ms = {"won": {"end": "1970-01-01T00:01:00Z"}, "lost": {"end": "1970-01-01T00:01:00Z"},
          "later": {"end": "1970-01-01T05:00:00Z"}, "now": {"question": "x"}}
    r = track_record(hist, 1000, "now", ms)
    assert r["prior_settled"] == 2 and r["prior_win_rate"] == 0.5 and r["prior_roi"] == 0.25


def test_repair_replaces_errored_markets(tmp_path, monkeypatch):
    import json as _j
    from scripts import history_backfill as hb
    a, b = "0xaaaaaaaa" + "1" * 56, "0xbbbbbbbb" + "2" * 56
    monkeypatch.setattr(hb, "MARKETS", tmp_path / "m.jsonl")
    monkeypatch.setattr(hb, "TRADES", tmp_path / "t.jsonl")
    hb.MARKETS.write_text("\n".join(_j.dumps({"cid": c, "n_out": 2}) for c in (a, b)) + "\n")
    hb.TRADES.write_text(_j.dumps({"cid": a, "old": 1}) + "\n" + _j.dumps({"cid": b, "keep": 1}) + "\n")
    log = tmp_path / "run.log"
    log.write_text("trades 120 / 2\ntrades err 0xaaaaaaaa urlopen error\n")
    monkeypatch.setattr(hb, "market_lines", lambda c, m: [_j.dumps({"cid": c, "new": 1}) + "\n"])
    hb.stage_repair(log)
    rows = [_j.loads(x) for x in hb.TRADES.read_text().splitlines()]
    assert {"cid": b, "keep": 1} in rows and {"cid": a, "new": 1} in rows and not any(r.get("old") for r in rows)
