"""Historical training set: every big BUY on closed Polymarket markets, rebuilt with the same
features the live tracker logs, then an honest test (train on markets that closed earlier,
grade on markets that closed later and were never seen in training).

uv run --env-file .env python scripts/history_backfill.py run --since 2026-05-01 [--min-volume 100000]
uv run --env-file .env python scripts/history_backfill.py status | eval
Every stage appends and skips work already done, so a killed run resumes where it stopped.
Writes only under ~/.polywatch/history/; the live model is never touched.
"""
import argparse, asyncio, bisect, functools, json, os, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
H = Path(os.environ.get("PW_HISTORY_DIR") or Path.home() / ".polywatch" / "history")
H.mkdir(parents=True, exist_ok=True)
MARKETS, TRADES, ROWS, STATE = H / "markets.jsonl", H / "trades.jsonl", H / "rows.jsonl", H / "state.json"
FSTATE = H / "features_state.json"  # own file so features can run while trades still appends
GAMMA = "https://gamma-api.polymarket.com/markets"
TRADES_URL = "https://data-api.polymarket.com/v2/trades"
MIN_EVAL_ROWS = 2000
MIN_USDC, MAX_PRICE, MAX_TRADE_PAGES = 500.0, 0.93, 200  # 200 pages = 100k fills  # same floor and price cap as the live backfill


def get(url: str, tries: int = 4):
    for i in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pw"}), timeout=30))
        except Exception as e:
            if i == tries - 1 or "422" in str(e) or "404" in str(e):
                raise
            time.sleep(2 * (i + 1))


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"days": [], "markets_done": [], "wallets_done": []}


def save(s: dict) -> None:
    STATE.with_suffix(".tmp").write_text(json.dumps(s)); STATE.with_suffix(".tmp").replace(STATE)


def jl(path: Path):
    out = []
    for l in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass  # a line still being appended by another stage
    return out


def trade_key(t: dict) -> str:
    return f'{t["wallet"]}|{t.get("tx")}|{t["cid"]}|{t["outcome_index"]}|{t["ts"]}'


def fstate() -> set:
    return set(json.loads(FSTATE.read_text())) if FSTATE.exists() else set()


def fsave(done: set) -> None:
    FSTATE.with_suffix(".tmp").write_text(json.dumps(sorted(done))); FSTATE.with_suffix(".tmp").replace(FSTATE)


def stage_markets(since: str, min_vol: float) -> None:
    s = state(); d = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with MARKETS.open("a") as out:
        while d < today:
            day = d.strftime("%Y-%m-%d")
            if day not in s["days"]:
                n = 0
                for off in range(0, 2500, 100):  # gamma rejects offset >= 2500
                    q = urllib.parse.urlencode({"closed": "true", "limit": 100, "offset": off, "volume_num_min": min_vol,
                                                "end_date_min": f"{day}T00:00:00Z", "end_date_max": (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
                    try:
                        page = get(f"{GAMMA}?{q}")
                    except Exception:
                        break
                    for m in page:
                        try:
                            prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                        except ValueError:
                            prices = []
                        out.write(json.dumps({"cid": str(m.get("conditionId")).lower(), "slug": m.get("slug"), "question": m.get("question"),
                                              "end": m.get("endDate"), "game_start": m.get("gameStartTime"), "volume": float(m.get("volume") or 0),
                                              "prices": prices, "smt": m.get("sportsMarketType"),
                                              "event": ((m.get("events") or [{}])[0].get("slug") or m.get("slug")),
                                              "n_out": len(json.loads(m.get("outcomes") or "[]"))}) + "\n"); n += 1
                    if len(page) < 100:
                        break
                s["days"].append(day); save(s); print("markets", day, n, flush=True)
            d += timedelta(days=1)


def trailing_volume(fills: list, truncated: bool) -> list:
    """For fills [(ts, usdc), ...] sorted by ts, the market's USDC volume in the 24h before each fill.
    None where the window reaches past the oldest fetched fill of a truncated market."""
    out, lo, run = [], 0, 0.0
    oldest = fills[0][0] if fills else 0
    for ts, usdc in fills:
        while fills[lo][0] < ts - 86400:
            run -= fills[lo][1]; lo += 1
        out.append(None if truncated and ts - 86400 < oldest else run)
        run += usdc
    return out


def market_lines(cid: str, m: dict) -> list:
    """Big trades of one market as JSON lines (empty for non-binary markets)."""
    params = {"condition": cid, "limit": 500}; raw = []; truncated = False
    for page_no in range(MAX_TRADE_PAGES):  # shortcut: markets past 100k fills lose their oldest fills (API is newest-first only)
        try:
            body = get(f"{TRADES_URL}?{urllib.parse.urlencode(params)}")
        except Exception as e:
            print("trades err", cid[:10], e, flush=True); truncated = True; break
        raw += body.get("data") or []
        cur = (body.get("pagination") or {}).get("next_cursor")
        if not (body.get("pagination") or {}).get("has_more") or not cur:
            break
        truncated = page_no == MAX_TRADE_PAGES - 1
        params = {"condition": cid, "limit": 500, "cursor": cur}
    if m.get("n_out") != 2:  # copy prices and sell mirroring assume a yes/no pair
        return []
    raw.sort(key=lambda t: int(t["timestamp"]))
    vols = trailing_volume([(int(t["timestamp"]), float(t["size"]) * float(t["price"])) for t in raw], truncated)
    fts = [int(t["timestamp"]) for t in raw]
    p0 = [float(t["price"]) if t.get("outcome_index") == 0 else 1 - float(t["price"]) for t in raw]  # price of outcome 0
    lines = []
    for t, v24 in zip(raw, vols):
        usdc = float(t["size"]) * float(t["price"])
        if usdc < MIN_USDC or t.get("outcome_index") not in (0, 1):
            continue
        sell = t.get("side") == "SELL"
        idx = 1 - t["outcome_index"] if sell else t["outcome_index"]  # selling Yes = buying No
        px = 1 - float(t["price"]) if sell else float(t["price"])
        if not 0 < px < MAX_PRICE:
            continue
        after = {}
        for mins in (2, 10, 30, 60):
            j = bisect.bisect_left(fts, int(t["timestamp"]) + 60 * mins)
            after[f"px{mins}"] = None if j >= len(raw) else round(p0[j] if idx == 0 else 1 - p0[j], 4)
        lines.append(json.dumps({"wallet": t["proxy_wallet"].lower(), "cid": cid, "ts": int(t["timestamp"]), "price": round(px, 4),
                                 "orig_price": float(t["price"]), "side": t["side"], "sell": int(sell), "usdc": round(usdc, 2),
                                 "vol24": None if v24 is None else round(v24, 2), "outcome_index": idx, **after,
                                 "tx": t.get("transaction_hash")}) + "\n")
    return lines


def stage_trades(workers: int = 6) -> None:
    from concurrent.futures import ThreadPoolExecutor
    s = state(); done = set(s["markets_done"])
    ms = {m["cid"]: m for m in jl(MARKETS)}
    todo = [c for c in ms if c not in done]
    print("trades: markets todo", len(todo), flush=True)
    with TRADES.open("a") as out, ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(todo), 120):
            chunk = todo[i:i + 120]
            for cid, lines in zip(chunk, pool.map(lambda c: market_lines(c, ms[c]), chunk)):
                out.writelines(lines)
            out.flush()
            s["markets_done"] += chunk; save(s)  # a chunk counts as done only after its lines are on disk
            print("trades", len(s["markets_done"]), "/", len(ms), flush=True)


def stage_repair(log: Path) -> None:
    """Refetch markets whose trade pull errored (e.g. a network outage) and replace their lines in TRADES."""
    pre = set(re.findall(r"^trades err (0x[0-9a-f]{8})", log.read_text(), re.M))
    ms = {m["cid"]: m for m in jl(MARKETS)}
    bad = [c for c in ms if c[:10] in pre]
    print("repair: markets", len(bad), flush=True)
    fresh = []
    for i, c in enumerate(bad):
        fresh.append(market_lines(c, ms[c]))
        if i % 100 == 99:
            print("repair", i + 1, "/", len(bad), flush=True)
    still = sum(1 for c, ls in zip(bad, fresh) if not ls and ms[c].get("n_out") == 2)
    badset, tmp = set(bad), TRADES.with_suffix(".repair")
    with TRADES.open() as src, tmp.open("w") as out:
        for line in src:
            try:
                if json.loads(line)["cid"] in badset:
                    continue
            except json.JSONDecodeError:
                continue
            out.write(line)
        for ls in fresh:
            out.writelines(ls)
    tmp.replace(TRADES)
    print("repair done; markets still empty", still, flush=True)


@functools.lru_cache(maxsize=200_000)
def cat_of(question) -> str:
    from polymarket_insider_tracker.detector.learned import category
    c = category(question or "")
    return max(c, key=c.get) if c and max(c.values()) > 0 else "other"


def track_record(hist, trade_ts: int, cid: str, ms: dict) -> dict:
    """Settled-market record before trade_ts: markets in the universe that closed before the trade.
    PnL is realized cash (sells + redeems - buys), so it needs no outcome side."""
    flows = defaultdict(lambda: [0.0, 0.0])  # cid -> [spent, got back]
    for h in hist.trades:
        if h.timestamp < trade_ts and h.condition_id != cid:
            flows[h.condition_id][0 if h.side == "BUY" else 1] += float(h.usdc)
    for ts, c, usdc in hist.redeems:
        if ts < trade_ts and c in flows:
            flows[c][1] += float(usdc)
    cat = cat_of(ms.get(cid, {}).get("question"))
    n = wins = cn = cw = 0; spent = got = 0.0
    for c, (sp, back) in flows.items():
        m = ms.get(c); end = learner_ts(m.get("end")) if m else None
        if not end or end >= trade_ts or sp <= 0:
            continue
        n += 1; wins += back > sp; spent += sp; got += back
        if cat != "other" and cat_of(m.get("question")) == cat:
            cn += 1; cw += back > sp
    return {"prior_settled": n, "prior_win_rate": round(wins / n, 3) if n else None,
            "prior_roi": round((got - spent) / spent, 3) if spent else None,
            "cat_settled": cn, "cat_win_rate": round(cw / cn, 3) if cn else None}


def fresh_conf(prior_trades: int, age_h, usdc: float) -> float:
    """Live FreshWalletDetector score, with prior Polymarket trades standing in for the wallet nonce."""
    from types import SimpleNamespace
    from polymarket_insider_tracker.detector import fresh_wallet as fw
    if usdc < float(fw.DEFAULT_MIN_TRADE_SIZE) or prior_trades > fw.DEFAULT_MAX_NONCE:
        return 0.0
    if age_h is not None and age_h > fw.DEFAULT_MAX_AGE_HOURS:
        return 0.0
    c, _ = fw.FreshWalletDetector.calculate_confidence(
        None, SimpleNamespace(nonce=prior_trades, age_hours=age_h), SimpleNamespace(notional_value=Decimal(str(usdc))))
    return round(c, 3)


def size_conf(usdc: float, vol24):
    """Live SizeAnomalyDetector score from trade / trailing 24h volume; no historical order book, so book impact is 0."""
    from polymarket_insider_tracker.detector import size_anomaly as sa
    if not vol24:
        return 0.0, 0
    d = object.__new__(sa.SizeAnomalyDetector)
    d._volume_threshold, d._book_threshold = sa.DEFAULT_VOLUME_THRESHOLD, sa.DEFAULT_BOOK_THRESHOLD
    niche = int(vol24 < float(sa.DEFAULT_NICHE_VOLUME_THRESHOLD))
    c, _ = d.calculate_confidence(volume_impact=usdc / vol24, book_impact=0.0, is_niche=bool(niche))
    return (round(c, 3) if c >= 0.1 else 0.0), niche


async def stage_features() -> None:
    import httpx
    from polymarket_insider_tracker.detector.insider_rules import RuleThresholds, evaluate_tiered, is_active_trader, is_watch_market
    from polymarket_insider_tracker.pipeline import _parse_ts
    from polymarket_insider_tracker.profiler.wallet_history import features_for, fetch_wallet_history
    done = fstate()  # per trade, so a wallet with trades pulled later gets those rows on the next pass
    ms = {m["cid"]: m for m in jl(MARKETS)}
    by_wallet = defaultdict(dict)
    for t in jl(TRADES):
        if (k := trade_key(t)) not in done:
            by_wallet[t["wallet"]][k] = t
    todo = list(by_wallet)
    print("features: wallets todo", len(todo), "trades", sum(map(len, by_wallet.values())), flush=True)
    base, sem, out, n = RuleThresholds(), asyncio.Semaphore(6), ROWS.open("a"), 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        async def one(w: str):
            nonlocal n
            items = list(by_wallet[w].values())
            async with sem:
                try:  # history only up to this wallet's last sampled trade: nothing after it can leak in
                    hist = await fetch_wallet_history(w, client=client, until_ts=max(t["ts"] for t in items), max_pages=6)
                except Exception:
                    return
            for t in items:
                m = ms.get(t["cid"], {})
                f = features_for(hist, condition_id=t["cid"], trade_ts=t["ts"], notional_usdc=Decimal(str(t["usdc"])),
                                 price=Decimal(str(t.get("orig_price", t["price"]))), side=t.get("side", "BUY"))
                watch = is_watch_market(m.get("question"), m.get("slug"))
                start, end = _parse_ts(m.get("game_start")), _parse_ts(m.get("end"))
                sc = size_conf(t["usdc"], t.get("vol24"))
                out.write(json.dumps({
                    "ts": t["ts"], "trade_id": t.get("tx"), "wallet": w, "market": t["cid"], "outcome": None,
                    "outcome_index": t["outcome_index"], "title": m.get("question"), "slug": m.get("slug"), "price": t["price"],
                    "usdc": t["usdc"], "age_h": round(f.wallet_age_hours or 0.0, 2), "prior_markets": f.prior_markets,
                    "prior_trades": f.prior_trades, "market_share": float(f.market_share) if f.market_share else None,
                    "market_cum": float(f.market_cumulative_usdc or 0), "longshot_markets": f.longshot_markets,
                    "longshot_wins": f.longshot_wins, "event_markets": f.event_markets, "event_share": None,
                    "in_play": int(isinstance(start, float) and t["ts"] >= start),
                    "hours_to_start": round((start - t["ts"]) / 3600, 2) if isinstance(start, float) else None,
                    "hours_to_end": round((end - t["ts"]) / 3600, 2) if isinstance(end, float) else None,
                    "market_volume": m.get("volume"), "niche": sc[1], "size_conf": sc[0],
                    "fresh_conf": fresh_conf(f.prior_trades, f.wallet_age_hours, t["usdc"]), "vol24": t.get("vol24"),
                    "sell": t.get("sell", 0), "px2": t.get("px2"), "px10": t.get("px10"), "px30": t.get("px30"), "px60": t.get("px60"),
                    "event": m.get("event"), **track_record(hist, t["ts"], t["cid"], ms),
                    "watch": int(watch), "active": int(is_active_trader(f, base)), "rules": evaluate_tiered(f, watch, base),
                    "source": "history"}) + "\n")
            done.update(by_wallet[w]); n += 1
            if n % 200 == 0:
                out.flush(); fsave(done); print("features", n, "/", len(todo), flush=True)
        await asyncio.gather(*(one(w) for w in todo))
    out.close(); fsave(done)


EXTRA = ("sell", "prior_settled", "prior_win_rate", "prior_roi", "cat_settled", "cat_win_rate", "cluster6h")


def add_clusters(rows: list) -> None:
    """cluster6h: distinct OTHER fresh wallets (<48h old) that bought the same side of the same market in the 6h before."""
    recent = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["ts"]):
        k = (r["market"], r["outcome_index"])
        q = [e for e in recent[k] if e[0] >= r["ts"] - 6 * 3600]
        r["cluster6h"] = len({w for _, w in q if w != r["wallet"]})
        if (r.get("age_h") or 1e9) < 48 and not r.get("sell"):
            q.append((r["ts"], r["wallet"]))
        recent[k] = q


def copy_roi(y, entry):
    """Per-dollar return buying at entry, minus a Kalshi-sized taker fee (0.07 * p * (1 - p) per contract)."""
    return (y - entry) / entry - 0.07 * (1 - entry)


def slice_report(label, m, y, w, rows, price):
    import numpy as np
    out = [label.ljust(34), f"n {int(m.sum()):>6}", f"grp {w[m].sum():>7.1f}"]
    if m.sum() == 0:
        print(" ".join(out)); return
    out.append(f"win {np.average(y[m], weights=w[m]):.3f} px {np.average(price[m], weights=w[m]):.3f}")
    out.append(f"roi@fill {np.average(copy_roi(y[m], price[m]), weights=w[m]):+.3f}")
    for d in (2, 10, 30):
        e = np.array([rows[i].get(f"px{d}") or np.nan for i in np.where(m)[0]], dtype=float)
        ok = ~np.isnan(e) & (e > 0.01) & (e < 0.97)
        if ok.sum():
            idx = np.where(m)[0][ok]
            out.append(f"@{d}m {np.average(copy_roi(y[idx], e[ok]), weights=w[idx]):+.3f}")
    print(" ".join(out))


def stage_eval() -> None:
    import numpy as np
    import learner
    ms = {m["cid"]: m for m in jl(MARKETS)}
    labels = {c: {"closed": True, "prices": m["prices"]} for c, m in ms.items()}
    seen, all_rows = set(), []
    for r in jl(ROWS):  # a killed-and-resumed features pass can write a wallet's rows twice
        k = (r["wallet"], r.get("trade_id"), r["market"], r["outcome_index"], r["ts"])
        if k not in seen:
            seen.add(k); all_rows.append(r)
    add_clusters(all_rows)
    data, x, y, w, price = learner.dataset(all_rows, labels)
    if len(data) < MIN_EVAL_ROWS:
        sys.exit(f"only {len(data)} labelled rows; let the backfill run longer")
    rows = [r for r, _ in data]
    ex = np.array([[float(r.get(k)) if r.get(k) is not None else -1.0 for k in EXTRA] for r in rows])
    X = np.hstack([x, ex])
    # event-level purge: every market of one event lands on one side of the cut
    ev_end = defaultdict(float)
    for c, m in ms.items():
        ev_end[m.get("event") or c] = max(ev_end[m.get("event") or c], learner_ts(m.get("end")) or 0)
    ends = np.array([ev_end[ms.get(r["market"], {}).get("event") or r["market"]] for r in rows])
    cut = np.quantile(ends, 0.75)
    tr, te = ends < cut, ends >= cut
    print(f"rows {len(rows)} train {tr.sum()} test {te.sum()} | test events close after {datetime.fromtimestamp(cut, timezone.utc):%Y-%m-%d}")
    print(f"brier: price {learner.brier(price[te], y[te], w[te]):.5f}")
    preds = {}
    for name, feats in (("base features", x), ("+ record/cluster/sell", X)):
        mdl = learner.CANDIDATES["hgb_reg"]().fit(feats[tr], y[tr], sample_weight=w[tr])
        preds[name] = mdl.predict_proba(feats[te])[:, 1]
        print(f"brier: model {name:<22} {learner.brier(preds[name], y[te], w[te]):.5f}")
    ti = np.where(te)[0]; yt, wt, pt = y[ti], w[ti], price[ti]; rt = [rows[i] for i in ti]
    col = lambda k, d=-1.0: np.array([float(r.get(k)) if r.get(k) is not None else d for r in rt])
    edge = preds["+ record/cluster/sell"] - pt
    cats = [cat_of(ms.get(r["market"], {}).get("question")) for r in rt]
    print("\n-- test set, ROI per $1 after fee: at the wallet's fill, then at the price 2/10/30 min later --")
    S = lambda lab, m: slice_report(lab, m, yt, wt, rt, pt)
    S("all big trades", np.ones(len(ti), bool))
    for lo in (0.05, 0.10, 0.15):
        S(f"model edge >= {lo}", edge >= lo)
    S("model edge >= 0.10 and moved their way @10m", (edge >= 0.10) & (col("px10") > pt))
    S("fresh wallet (fresh_conf > 0)", col("fresh_conf", 0) > 0)
    S("size anomaly (size_conf > 0)", col("size_conf", 0) > 0)
    S("fresh + size", (col("fresh_conf", 0) > 0) & (col("size_conf", 0) > 0))
    S("cluster: 2+ other fresh wallets same side", col("cluster6h", 0) >= 2)
    S("proven: 20+ settled, win rate >= 0.6", (col("prior_settled", 0) >= 20) & (col("prior_win_rate") >= 0.6))
    S("proven: 20+ settled, roi >= 0.2", (col("prior_settled", 0) >= 20) & (col("prior_roi") >= 0.2))
    S("any insider rule fired", np.array([bool(r.get("rules")) for r in rt]))
    S("sells (mirrored as buys)", col("sell", 0) > 0)
    S("pre-game / not in play", col("in_play", 0) == 0)
    S("in play", col("in_play", 0) > 0)
    for lo, hi in ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.93)):
        S(f"price {lo}-{hi}", (pt >= lo) & (pt < hi))
    for c in sorted(set(cats)):
        S(f"category {c}", np.array([k == c for k in cats]))
        S(f"category {c}, edge >= 0.10", np.array([k == c for k in cats]) & (edge >= 0.10))


def learner_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def status() -> None:
    s = state()
    print("days", len(s["days"]), "markets", sum(1 for _ in MARKETS.open()) if MARKETS.exists() else 0,
          "markets traded", len(s["markets_done"]), "trades", sum(1 for _ in TRADES.open()) if TRADES.exists() else 0,
          "wallets done", len(s["wallets_done"]), "rows", sum(1 for _ in ROWS.open()) if ROWS.exists() else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "markets", "trades", "repair", "features", "eval", "status"])
    ap.add_argument("--since", default="2026-05-01")
    ap.add_argument("--min-volume", type=float, default=10000)
    a = ap.parse_args()
    if a.cmd in ("run", "markets"): stage_markets(a.since, a.min_volume)
    if a.cmd in ("run", "trades"): stage_trades()
    if a.cmd in ("run", "features"): asyncio.run(stage_features())
    if a.cmd in ("run", "eval"): stage_eval()
    if a.cmd == "repair": stage_repair(H / "run.log")
    if a.cmd == "status": status()
