"""Historical training set: every big BUY on closed Polymarket markets, rebuilt with the same
features the live tracker logs, then an honest test (train on markets that closed earlier,
grade on markets that closed later and were never seen in training).

uv run --env-file .env python scripts/history_backfill.py run --since 2026-05-01 [--min-volume 100000]
uv run --env-file .env python scripts/history_backfill.py status | eval
Every stage appends and skips work already done, so a killed run resumes where it stopped.
Writes only under ~/.polywatch/history/; the live model is never touched.
"""
import argparse, asyncio, json, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
H = Path.home() / ".polywatch" / "history"
H.mkdir(parents=True, exist_ok=True)
MARKETS, TRADES, ROWS, STATE = H / "markets.jsonl", H / "trades.jsonl", H / "rows.jsonl", H / "state.json"
GAMMA = "https://gamma-api.polymarket.com/markets"
TRADES_URL = "https://data-api.polymarket.com/v2/trades"
MIN_USDC, MAX_PRICE, MAX_TRADE_PAGES = 500.0, 0.93, 20  # same floor and price cap as the live backfill


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
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


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
                                              "prices": prices, "smt": m.get("sportsMarketType")}) + "\n"); n += 1
                    if len(page) < 100:
                        break
                s["days"].append(day); save(s); print("markets", day, n, flush=True)
            d += timedelta(days=1)


def stage_trades() -> None:
    s = state(); done = set(s["markets_done"])
    ms = {m["cid"]: m for m in jl(MARKETS)}
    with TRADES.open("a") as out:
        for i, cid in enumerate(c for c in ms if c not in done):
            params = {"condition": cid, "limit": 500}; kept = 0
            for _ in range(MAX_TRADE_PAGES):  # shortcut: very busy markets are capped at 10k trades, newest first
                try:
                    body = get(f"{TRADES_URL}?{urllib.parse.urlencode(params)}")
                except Exception as e:
                    print("trades err", cid[:10], e, flush=True); break
                for t in body.get("data") or []:
                    usdc = float(t["size"]) * float(t["price"])
                    if t.get("side") == "BUY" and usdc >= MIN_USDC and float(t["price"]) < MAX_PRICE:
                        out.write(json.dumps({"wallet": t["proxy_wallet"].lower(), "cid": cid, "ts": int(t["timestamp"]), "price": float(t["price"]),
                                              "usdc": round(usdc, 2), "outcome": t.get("outcome"), "outcome_index": t.get("outcome_index"),
                                              "tx": t.get("transaction_hash")}) + "\n"); kept += 1
                cur = (body.get("pagination") or {}).get("next_cursor")
                if not (body.get("pagination") or {}).get("has_more") or not cur:
                    break
                params = {"condition": cid, "limit": 500, "cursor": cur}
            s["markets_done"].append(cid)
            if i % 25 == 0:
                save(s); print("trades", len(s["markets_done"]), "markets, last kept", kept, flush=True)
    save(s)


async def stage_features() -> None:
    import httpx
    from polymarket_insider_tracker.detector.insider_rules import RuleThresholds, evaluate_tiered, is_active_trader, is_watch_market
    from polymarket_insider_tracker.pipeline import _parse_ts
    from polymarket_insider_tracker.profiler.wallet_history import features_for, fetch_wallet_history
    s = state(); done = set(s["wallets_done"])
    ms = {m["cid"]: m for m in jl(MARKETS)}
    by_wallet = defaultdict(list)
    for t in jl(TRADES):
        by_wallet[t["wallet"]].append(t)
    todo = [w for w in by_wallet if w not in done]
    print("features: wallets", len(by_wallet), "todo", len(todo), flush=True)
    base, sem, out, n = RuleThresholds(), asyncio.Semaphore(6), ROWS.open("a"), 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        async def one(w: str):
            nonlocal n
            items = by_wallet[w]
            async with sem:
                try:  # history only up to this wallet's last sampled trade: nothing after it can leak in
                    hist = await fetch_wallet_history(w, client=client, until_ts=max(t["ts"] for t in items), max_pages=6)
                except Exception:
                    return
            for t in items:
                m = ms.get(t["cid"], {})
                f = features_for(hist, condition_id=t["cid"], trade_ts=t["ts"], notional_usdc=Decimal(str(t["usdc"])),
                                 price=Decimal(str(t["price"])), side="BUY")
                watch = is_watch_market(m.get("question"), m.get("slug"))
                start, end = _parse_ts(m.get("game_start")), _parse_ts(m.get("end"))
                out.write(json.dumps({
                    "ts": t["ts"], "trade_id": t.get("tx"), "wallet": w, "market": t["cid"], "outcome": t["outcome"],
                    "outcome_index": t["outcome_index"], "title": m.get("question"), "slug": m.get("slug"), "price": t["price"],
                    "usdc": t["usdc"], "age_h": round(f.wallet_age_hours or 0.0, 2), "prior_markets": f.prior_markets,
                    "prior_trades": f.prior_trades, "market_share": float(f.market_share) if f.market_share else None,
                    "market_cum": float(f.market_cumulative_usdc or 0), "longshot_markets": f.longshot_markets,
                    "longshot_wins": f.longshot_wins, "event_markets": f.event_markets, "event_share": None,
                    "in_play": int(isinstance(start, float) and t["ts"] >= start),
                    "hours_to_start": round((start - t["ts"]) / 3600, 2) if isinstance(start, float) else None,
                    "hours_to_end": round((end - t["ts"]) / 3600, 2) if isinstance(end, float) else None,
                    "market_volume": m.get("volume"), "niche": 0, "size_conf": 0.0, "fresh_conf": 0.0,  # live-only scores, absent in history
                    "watch": int(watch), "active": int(is_active_trader(f, base)), "rules": evaluate_tiered(f, watch, base),
                    "source": "history"}) + "\n")
            s["wallets_done"].append(w); n += 1
            if n % 200 == 0:
                out.flush(); save(s); print("features", n, "/", len(todo), flush=True)
        await asyncio.gather(*(one(w) for w in todo))
    out.close(); save(s)


def stage_eval() -> None:
    import numpy as np
    import learner
    ms = {m["cid"]: m for m in jl(MARKETS)}
    labels = {c: {"closed": True, "prices": m["prices"]} for c, m in ms.items()}
    data, x, y, w, price = learner.dataset(jl(ROWS), labels)
    if len(data) < 2000:
        sys.exit(f"only {len(data)} labelled rows; let the backfill run longer")
    end = {c: learner_ts(m.get("end")) for c, m in ms.items()}
    ends = np.array([end.get(r["market"]) or 0 for r, _ in data])
    cut = np.quantile(ends, 0.75)
    tr = (ends < cut) & (np.array([r["ts"] for r, _ in data]) < cut)  # trained markets all closed before the test window
    te = ends >= cut
    te &= ~np.isin(np.array([r["market"] for r, _ in data]), list({data[i][0]["market"] for i in np.where(tr)[0]}))
    print(f"rows {len(data)} train {tr.sum()} test {te.sum()} test markets {len({data[i][0]['market'] for i in np.where(te)[0]})}")
    print("test window starts", datetime.fromtimestamp(cut, timezone.utc).strftime("%Y-%m-%d"))
    b_price = learner.brier(price[te], y[te], w[te])
    for name, make in learner.CANDIDATES.items():
        p = make().fit(x[tr], y[tr], sample_weight=w[tr]).predict_proba(x[te])[:, 1]
        print(f"{name:<20} brier {learner.brier(p, y[te], w[te]):.5f} vs price {b_price:.5f}")
        for lo in (0.05, 0.10, 0.15):
            print("   edge >=", lo, learner.edge_bucket(p, y[te], price[te], w[te], lo))
        print("   no edge", learner.edge_bucket(p, y[te], price[te], w[te], 0.0, below=True))


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
    ap.add_argument("cmd", choices=["run", "markets", "trades", "features", "eval", "status"])
    ap.add_argument("--since", default="2026-05-01")
    ap.add_argument("--min-volume", type=float, default=100000)
    a = ap.parse_args()
    if a.cmd in ("run", "markets"): stage_markets(a.since, a.min_volume)
    if a.cmd in ("run", "trades"): stage_trades()
    if a.cmd in ("run", "features"): asyncio.run(stage_features())
    if a.cmd in ("run", "eval"): stage_eval()
    if a.cmd == "status": status()
