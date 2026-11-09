"""Continuous learner for the Polymarket insider tracker (local fork).

Every run:
  1. labels logged trades whose market has resolved (Gamma outcomePrices),
  2. retrains a gradient-boosted P(win) model on every labelled trade,
  3. validates on the most recent 25% of trades (time split, so no peeking ahead),
  4. promotes the model only when it beats the market price as a forecaster (Brier score)
     on that held-out slice; otherwise the live gate stays on the previous promoted model.

The live pipeline reloads ~/.polywatch/model.pkl on change, gates rule alerts on
edge = P(win) - price, and raises M1_model_edge alerts on its own.

  uv run --env-file .env python scripts/learner.py            # label + train (launchd)
  uv run --env-file .env python scripts/learner.py --backfill # rebuild features from the DB
  uv run --env-file .env python scripts/learner.py --report   # print the latest metrics
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pickle
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_insider_tracker.detector.learned import (  # noqa: E402
    FEATURE_NAMES,
    FEATURES_PATH,
    HOME,
    META_PATH,
    MODEL_PATH,
    PriceAnchoredModel,
    vectorize,
)

BACKFILL_PATH = HOME / "features_backfill.jsonl"
LABELS_PATH = HOME / "labels.json"
HISTORY_PATH = HOME / "learner_history.jsonl"
GAMMA = "https://gamma-api.polymarket.com/markets"
MIN_TEST = int(os.environ.get("ML_MIN_TEST", "300"))
MIN_GAIN = float(os.environ.get("ML_MIN_BRIER_GAIN", "0.001"))
# The model's own picks (edge >= 0.10) must have made money on the held-out slice, over at least
# this many distinct wallet+market groups. Beating price on Brier alone can come from fixing the
# favourite-longshot bias, which says nothing about who is informed.
MIN_PICK_GROUPS = float(os.environ.get("ML_MIN_PICK_GROUPS", "30"))
RECHECK_OPEN_S = 2 * 3600
STATS_PATH = HOME / "rule_stats.json"
SHADOW_PATH = HOME / "model_shadow.pkl"
SHADOW_META_PATH = HOME / "model_shadow_meta.json"
PRICE_EDGES = (0.2, 0.4, 0.6, 0.85)


def price_bucket(price: float) -> int:
    return next((i for i, e in enumerate(PRICE_EDGES) if price < e), len(PRICE_EDGES))


def _atomic_json(path: Path, obj: object) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def rule_stats(data: list) -> dict:
    """Track record of resolved trades by rule and price bucket, one vote per wallet+market.

    The SMS card reads this to say how trades like the one it is texting about have done.
    """
    groups = Counter((r.get("wallet"), r.get("market")) for r, _ in data)
    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    for r, y in data:
        price = float(r.get("price") or 0)
        w = 1.0 / groups[(r.get("wallet"), r.get("market"))]
        roi = (y - price) / max(price, 0.01)
        b = price_bucket(price)
        rules = {x[2:] if x.startswith("W_") else x for x in r.get("rules") or []}
        keys = ["all", f"all|{b}"]
        if rules:
            keys += ["flagged", f"flagged|{b}"]
        for ru in rules:
            keys += [ru, f"{ru}|{b}"]
        for k in keys:
            a = acc[k]
            a[0] += w
            a[1] += w * y
            a[2] += w * price
            a[3] += w * roi
            a[4] += 1
    return {
        "price_edges": PRICE_EDGES,
        "updated": int(time.time()),
        "stats": {
            k: {
                "groups": round(a[0], 1),
                "n": int(a[4]),
                "win_rate": round(a[1] / a[0], 4),
                "avg_price": round(a[2] / a[0], 4),
                "roi": round(a[3] / a[0], 4),
            }
            for k, a in acc.items()
        },
    }


def tg(text: str) -> None:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    with contextlib.suppress(OSError):
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=15)


def get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "polywatch-learner"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except OSError:
            time.sleep(2 * (attempt + 1))
    return []


def read_rows() -> list[dict]:
    rows: list[dict] = []
    for path in (BACKFILL_PATH, FEATURES_PATH):
        if not path.exists():
            continue
        with open(path) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    seen: set[str] = set()
    out = []
    for r in rows:  # backfill and live can overlap: one row per trade id + market
        key = f"{r.get('trade_id')}|{r.get('market')}|{r.get('usdc')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def label_markets(rows: list[dict]) -> dict[str, dict]:
    labels: dict[str, dict] = json.loads(LABELS_PATH.read_text()) if LABELS_PATH.exists() else {}
    now = time.time()
    todo = sorted(
        {
            r["market"]
            for r in rows
            if r.get("market")
            and not labels.get(r["market"], {}).get("closed")
            and now - labels.get(r["market"], {}).get("checked", 0) > RECHECK_OPEN_S
        }
    )
    for i in range(0, len(todo), 40):
        batch = todo[i : i + 40]
        qs = "&".join(f"condition_ids={c}" for c in batch)
        found: dict[str, dict] = {}
        for extra in ("&closed=true", ""):
            data = get_json(f"{GAMMA}?{qs}&limit=100{extra}")
            for m in data if isinstance(data, list) else []:
                cid = str(m.get("conditionId") or "").lower()
                try:
                    prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                except (ValueError, TypeError):
                    prices = []
                found[cid] = {"closed": bool(m.get("closed")), "prices": prices, "checked": now}
        for c in batch:
            labels[c] = found.get(c, {"closed": False, "prices": [], "checked": now})
    tmp = LABELS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(labels))
    tmp.replace(LABELS_PATH)
    return labels


def dataset(rows: list[dict], labels: dict[str, dict]):
    data = []
    for r in rows:
        lab = labels.get(r.get("market") or "")
        if not lab or not lab.get("closed"):
            continue
        idx = r.get("outcome_index")
        prices = lab.get("prices") or []
        if idx is None or idx >= len(prices):
            continue
        p = prices[idx]
        if p not in (0.0, 1.0):  # voided / 50-50 resolutions carry no signal
            continue
        data.append((r, int(p == 1.0)))
    data.sort(key=lambda d: d[0].get("ts") or 0)
    # One bot's hundred fills on one market must not outvote a hundred wallets: weight each
    # (wallet, market) group to a total of 1.
    groups = Counter((r.get("wallet"), r.get("market")) for r, _ in data)
    x = np.array([vectorize(r) for r, _ in data], dtype=float)
    y = np.array([lab for _, lab in data], dtype=int)
    w = np.array([1.0 / groups[(r.get("wallet"), r.get("market"))] for r, _ in data])
    price = np.array([float(r.get("price") or 0) for r, _ in data])
    return data, x, y, w, price


def brier(p: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    return float(np.sum(w * (p - y) ** 2) / np.sum(w))


CANDIDATES = {
    "anchored_lr_c0.01": lambda: PriceAnchoredModel(0.01),
    "anchored_lr_c0.03": lambda: PriceAnchoredModel(0.03),
    "hgb_reg": lambda: HistGradientBoostingClassifier(
        max_iter=150,
        learning_rate=0.03,
        max_leaf_nodes=7,
        min_samples_leaf=200,
        l2_regularization=5.0,
        random_state=7,
    ),
}


def edge_bucket(
    p: np.ndarray, y: np.ndarray, price: np.ndarray, w: np.ndarray, lo: float, below: bool = False
):
    m = (p - price) < lo if below else (p - price) >= lo
    if not m.any():
        return {"n": 0, "groups": 0.0, "roi": None, "win_rate": None}
    roi = (y[m] - price[m]) / np.maximum(price[m], 0.01)
    return {
        "n": int(m.sum()),
        "groups": round(float(w[m].sum()), 1),
        "roi": round(float(np.sum(w[m] * roi) / np.sum(w[m])), 3),
        "win_rate": round(float(np.sum(w[m] * y[m]) / np.sum(w[m])), 3),
        "avg_price": round(float(np.sum(w[m] * price[m]) / np.sum(w[m])), 3),
    }


def train(rows: list[dict], labels: dict[str, dict]) -> dict:
    data, x, y, w, price = dataset(rows, labels)
    n = len(data)
    if data:
        _atomic_json(STATS_PATH, rule_stats(data))
    meta: dict = {"trained_at": int(time.time()), "n_rows": len(rows), "n_labelled": n}
    prev = json.loads(META_PATH.read_text()) if META_PATH.exists() else {}
    if n < MIN_TEST * 4 or len(set(y.tolist())) < 2:
        meta.update({"promoted": bool(prev.get("promoted")), "status": "not enough labels"})
        return meta
    cut = int(n * 0.75)
    # Grade only on markets absent from training: a market seen in both lets the trees memorise
    # its outcome through market-level features (measured 2026-11-09: +24.7% ROI fell to -1.8%).
    seen = {r.get("market") for r, _ in data[:cut]}
    keep = np.array([r.get("market") not in seen for r, _ in data[cut:]])
    if keep.sum() < MIN_TEST:
        meta.update({"promoted": bool(prev.get("promoted")), "status": "not enough unseen-market test rows"})
        return meta
    yt, wt, pt = y[cut:][keep], w[cut:][keep], price[cut:][keep]
    b_price = brier(pt, yt, wt)
    scored = {}
    for name, make in CANDIDATES.items():
        p = make().fit(x[:cut], y[:cut], sample_weight=w[:cut]).predict_proba(x[cut:][keep])[:, 1]
        picks = edge_bucket(p, yt, pt, wt, 0.10)
        scored[name] = (brier(p, yt, wt), picks, p)
    name = min(scored, key=lambda k: scored[k][0])
    b_model, picks, p = scored[name]
    beats_brier = b_model < b_price - MIN_GAIN
    picks_pay = picks["groups"] >= MIN_PICK_GROUPS and (picks["roi"] or -1) > 0
    beats = beats_brier and picks_pay
    meta.update(
        {
            "model": name,
            "candidates": {k: round(v[0], 5) for k, v in scored.items()},
            "beats_brier": beats_brier,
            "picks_pay": picks_pay,
            "n_train": cut,
            "n_test": int(keep.sum()),
            "test_wallet_markets": round(float(wt.sum()), 1),
            "brier_model": round(b_model, 5),
            "brier_price": round(b_price, 5),
            "beats_price": beats,
            "edge_ge_0.10": edge_bucket(p, yt, pt, wt, 0.10),
            "edge_ge_0.15": edge_bucket(p, yt, pt, wt, 0.15),
            "edge_lt_0": edge_bucket(p, yt, pt, wt, 0.0, below=True),  # model: no edge
        }
    )
    # Shadow model: best candidate refit on everything, saved even when not promoted, so the SMS
    # card can show its read clearly labelled as unvalidated. The live gate never uses it.
    shadow = CANDIDATES[name]().fit(x, y, sample_weight=w)
    with open(SHADOW_PATH.with_suffix(".tmp"), "wb") as fh:
        pickle.dump(shadow, fh)
    SHADOW_PATH.with_suffix(".tmp").replace(SHADOW_PATH)
    _atomic_json(SHADOW_META_PATH, {**meta, "version": time.strftime("%Y%m%d-%H%M")})
    if not beats:
        meta.update(
            {
                "promoted": bool(prev.get("promoted")),
                "status": "kept previous model" if prev.get("promoted") else "no model live yet",
            }
        )
        return meta
    final = CANDIDATES[name]().fit(x, y, sample_weight=w)  # validated; refit on everything
    version = time.strftime("%Y%m%d-%H%M")
    meta.update({"promoted": True, "status": "promoted", "version": version})
    tmp = MODEL_PATH.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(final, fh)
    META_PATH.with_suffix(".tmp").write_text(json.dumps(meta, indent=1))
    META_PATH.with_suffix(".tmp").replace(META_PATH)
    tmp.replace(MODEL_PATH)  # model last: the pipeline reloads on the model file's mtime
    if not prev.get("promoted"):
        tg(
            f"polywatch learner: first model live ({n} labelled trades). "
            f"Brier {b_model:.4f} vs price {b_price:.4f}."
        )
    return meta


async def backfill() -> None:
    """Rebuild feature rows for every BUY the tracker scored, from its Postgres table."""
    import httpx
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from polymarket_insider_tracker.detector.insider_rules import (
        RuleThresholds,
        evaluate_tiered,
        is_active_trader,
        is_watch_market,
    )
    from polymarket_insider_tracker.pipeline import _parse_ts
    from polymarket_insider_tracker.profiler.wallet_history import (
        features_for,
        fetch_wallet_history,
    )

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "select distinct on (trade_id, market_id) trade_id, wallet_address, market_id,"
                " outcome, outcome_index, price, notional_usdc, trade_timestamp,"
                " coalesce(is_niche_market,false), coalesce(size_anomaly_confidence,0),"
                " coalesce(fresh_wallet_confidence,0)"
                " from risk_assessments where side='BUY' and notional_usdc >= 500"
                " and price < 0.93 order by trade_id, market_id"
            )
        )
        trades = res.fetchall()
    await engine.dispose()
    print(f"backfill: {len(trades)} trades", flush=True)
    by_wallet: dict[str, list] = defaultdict(list)
    for t in trades:
        by_wallet[t[1].lower()].append(t)
    markets = sorted({t[2].lower() for t in trades})
    info: dict[str, dict] = {}
    for i in range(0, len(markets), 40):
        qs = "&".join(f"condition_ids={c}" for c in markets[i : i + 40])
        for extra in ("&closed=true", ""):
            data = get_json(f"{GAMMA}?{qs}&limit=100{extra}")
            for m in data if isinstance(data, list) else []:
                info[str(m.get("conditionId")).lower()] = {
                    "game_start": _parse_ts(m.get("gameStartTime")),
                    "end": _parse_ts(m.get("endDate")),
                    "volume": float(m.get("volume") or 0),
                    "question": m.get("question") or "",
                    "slug": m.get("slug") or "",
                }
    print(f"backfill: {len(info)} markets resolved in gamma", flush=True)
    base = RuleThresholds()
    sem = asyncio.Semaphore(6)
    out = open(BACKFILL_PATH.with_suffix(".tmp"), "w")  # noqa: SIM115 closed after the gather
    done = 0

    async with httpx.AsyncClient(timeout=20.0) as client:

        async def one(wallet: str, items: list) -> list[dict]:
            async with sem:
                try:
                    hist = await fetch_wallet_history(wallet, client=client, max_pages=4)
                except Exception:
                    return []
            rows = []
            for t in items:
                cid = t[2].lower()
                m = info.get(cid, {})
                ts = int(t[7].timestamp())
                f = features_for(
                    hist,
                    condition_id=cid,
                    trade_ts=ts,
                    notional_usdc=Decimal(t[6]),
                    price=Decimal(t[5]),
                    side="BUY",
                )
                watch = is_watch_market(m.get("question"), m.get("slug"))
                fired = evaluate_tiered(f, watch, base)
                active = is_active_trader(f, base)
                start, end = m.get("game_start"), m.get("end")
                rows.append(
                    {
                        "ts": ts,
                        "trade_id": t[0],
                        "wallet": wallet,
                        "market": cid,
                        "outcome": t[3],
                        "outcome_index": t[4],
                        "title": m.get("question"),
                        "slug": m.get("slug"),
                        "price": float(t[5]),
                        "usdc": round(float(t[6]), 2),
                        "age_h": round(f.wallet_age_hours or 0.0, 2),
                        "prior_markets": f.prior_markets,
                        "prior_trades": f.prior_trades,
                        "market_share": float(f.market_share) if f.market_share else None,
                        "market_cum": float(f.market_cumulative_usdc or 0),
                        "longshot_markets": f.longshot_markets,
                        "longshot_wins": f.longshot_wins,
                        "event_markets": f.event_markets,
                        "event_share": None,
                        "in_play": int(isinstance(start, float) and ts >= start),
                        "hours_to_start": round((start - ts) / 3600, 2)
                        if isinstance(start, float)
                        else None,
                        "hours_to_end": round((end - ts) / 3600, 2)
                        if isinstance(end, float)
                        else None,
                        "market_volume": m.get("volume"),
                        "niche": int(bool(t[8])),
                        "size_conf": float(t[9]),
                        "fresh_conf": float(t[10]),
                        "watch": int(watch),
                        "active": int(active),
                        "rules": fired,
                        "source": "backfill",
                    }
                )
            return rows

        tasks = [one(w, items) for w, items in by_wallet.items()]
        for coro in asyncio.as_completed(tasks):
            for r in await coro:
                out.write(json.dumps(r) + "\n")
            done += 1
            if done % 250 == 0:
                print(f"backfill: {done}/{len(tasks)} wallets", flush=True)
    out.close()
    BACKFILL_PATH.with_suffix(".tmp").replace(BACKFILL_PATH)
    print(f"backfill: wrote {BACKFILL_PATH}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    HOME.mkdir(parents=True, exist_ok=True)
    if args.report:
        print(META_PATH.read_text() if META_PATH.exists() else "no promoted model yet")
        if HISTORY_PATH.exists():
            print("last run:", HISTORY_PATH.read_text().strip().splitlines()[-1])
        return
    if args.backfill:
        asyncio.run(backfill())
    rows = read_rows()
    labels = label_markets(rows)
    meta = train(rows, labels)
    meta["features"] = len(FEATURE_NAMES)
    with open(HISTORY_PATH, "a") as fh:
        fh.write(json.dumps(meta) + "\n")
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
