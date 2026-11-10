"""Kalshi daily-high weather lane (paper only).

fetch:    settled KXHIGH* events + hourly candles, Open-Meteo day-before forecasts, NWS CLI highs -> cache
backtest: model prob vs ask at two snapshots, $200/bet after taker fee, rolling (past-only) bias/sd,
          threshold picked on train period, graded on the last 30 days
scan:     open markets with edge now (current forecast + same rolling calibration)
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist, mean, pstdev
from zoneinfo import ZoneInfo

K = "https://api.elections.kalshi.com/trade-api/v2"
OM_PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"
OM_FC = "https://api.open-meteo.com/v1/forecast"
IEM = "https://mesonet.agron.iastate.edu/json/cli.py"
CACHE = Path.home() / ".polywatch" / "edge" / "weather"
STAKE = 200.0
MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless"]
WINDOW, MIN_HIST, MIN_SD = 45, 15, 1.2
THRESHOLDS = [0.03, 0.05, 0.08, 0.10, 0.15]
TEST_DAYS = 30
N = NormalDist()

# series -> (CLI station, lat, lon, tz)
STATIONS = {
    "KXHIGHNY": ("KNYC", 40.7790, -73.9693, "America/New_York"),
    "KXHIGHCHI": ("KMDW", 41.7860, -87.7524, "America/Chicago"),
    "KXHIGHLAX": ("KLAX", 33.9382, -118.3866, "America/Los_Angeles"),
    "KXHIGHMIA": ("KMIA", 25.7881, -80.3169, "America/New_York"),
    "KXHIGHAUS": ("KAUS", 30.1945, -97.6699, "America/Chicago"),
    "KXHIGHDEN": ("KDEN", 39.8466, -104.6562, "America/Denver"),
    "KXHIGHPHIL": ("KPHL", 39.8733, -75.2268, "America/New_York"),
    "KXHIGHTBOS": ("KBOS", 42.3606, -71.0097, "America/New_York"),
    "KXHIGHTSEA": ("KSEA", 47.4447, -122.3144, "America/Los_Angeles"),
    "KXHIGHTATL": ("KATL", 33.6301, -84.4418, "America/New_York"),
    "KXHIGHTDC": ("KDCA", 38.8483, -77.0341, "America/New_York"),
    "KXHIGHTPHX": ("KPHX", 33.4277, -112.0038, "America/Phoenix"),
    "KXHIGHTLV": ("KLAS", 36.0719, -115.1634, "America/Los_Angeles"),
    "KXHIGHTDAL": ("KDFW", 32.8998, -97.0403, "America/Chicago"),
    "KXHIGHTSFO": ("KSFO", 37.6197, -122.3647, "America/Los_Angeles"),
}
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def get(url: str, tries: int = 9):
    for i in range(tries):
        time.sleep(0.12)  # stay under Kalshi's public rate limit
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polywatch-edge"})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            time.sleep(min(30, 2**i))
    return None


def kalshi_fee(contracts: int, p: float) -> float:
    return math.ceil(round(0.07 * contracts * p * (1 - p) * 100, 6)) / 100


def event_date(event_ticker: str) -> date:
    s = event_ticker.split("-")[1]  # 26NOV08
    return date(2000 + int(s[:2]), MONTHS.index(s[2:5]) + 1, int(s[5:7]))


def prob(m: dict, mu: float, sd: float) -> float:
    """P(Yes) for a bracket on an integer-degree high, normal predictive."""
    c = lambda x: N.cdf((x - mu) / sd)  # noqa: E731
    t = m["strike_type"]
    if t == "greater":
        return 1 - c(m["floor_strike"] + 0.5)
    if t == "less":
        return c(m["cap_strike"] - 0.5)
    return c(m["cap_strike"] + 0.5) - c(m["floor_strike"] - 0.5)


def bet(p: float, yes_ask: float, yes_bid: float, thr: float):
    """Pick the side with edge > thr. Returns (side, price) or None."""
    if 0.02 < yes_ask < 0.98 and p - yes_ask > thr:
        return "yes", yes_ask
    no_ask = round(1 - yes_bid, 4)
    if 0.02 < no_ask < 0.98 and (1 - p) - no_ask > thr:
        return "no", no_ask
    return None


def settle(side: str, price: float, won_yes: bool) -> float:
    c = int(STAKE // price)
    win = won_yes if side == "yes" else not won_yes
    return c * (1.0 if win else 0.0) - c * price - kalshi_fee(c, price)


# ---------- fetch ----------


def fetch_events(series: str, since: date) -> list:
    out, cursor = [], ""
    while True:
        q = {"series_ticker": series, "status": "settled", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            q["cursor"] = cursor
        d = get(f"{K}/events?{urllib.parse.urlencode(q)}")
        evs = d.get("events", [])
        for e in evs:
            if event_date(e["event_ticker"]) >= since:
                if not e.get("markets"):  # older events come back without nested markets
                    e["markets"] = get(f"{K}/historical/markets?event_ticker={e['event_ticker']}&limit=50")["markets"]
                    e["historical"] = True
                out.append(e)
        cursor = d.get("cursor")
        if not cursor or not evs or event_date(evs[-1]["event_ticker"]) < since:
            return out


def fetch_candles(e: dict) -> dict:
    d = event_date(e["event_ticker"])
    start = int(datetime(d.year, d.month, d.day).timestamp()) - 3 * 86400
    end = start + 5 * 86400
    if e.get("historical"):  # past the live cutoff: per-market endpoint, no batch
        return {m["ticker"]: get(f"{K}/historical/markets/{m['ticker']}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60")
                .get("candlesticks", []) for m in e["markets"]}
    tick = ",".join(m["ticker"] for m in e["markets"])
    r = get(f"{K}/markets/candlesticks?market_tickers={tick}&start_ts={start}&end_ts={end}&period_interval=60")
    return {x["market_ticker"]: x["candlesticks"] for x in r.get("markets", [])}


def daily_max(hourly: dict, key: str) -> dict:
    out: dict = {}
    for t, v in zip(hourly["time"], hourly.get(key) or [], strict=False):
        if v is not None:
            out[t[:10]] = max(out.get(t[:10], -999.0), v)
    return out


def fetch_forecasts(series: str, start: date, end: date) -> dict:
    _, lat, lon, tz = STATIONS[series]
    q = {
        "latitude": lat, "longitude": lon, "timezone": tz, "temperature_unit": "fahrenheit",
        "hourly": "temperature_2m_previous_day1", "models": ",".join(MODELS),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }
    h = get(f"{OM_PREV}?{urllib.parse.urlencode(q)}")["hourly"]
    return {m: daily_max(h, f"temperature_2m_previous_day1_{m}") for m in MODELS}


def fetch_cli(series: str, years: list[int]) -> dict:
    st = STATIONS[series][0]
    out = {}
    for y in years:
        for r in get(f"{IEM}?station={st}&year={y}")["results"]:
            if isinstance(r.get("high"), int | float):
                out[r["valid"]] = r["high"]
    return out


def cmd_fetch(days: int) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    since = date.today() - timedelta(days=days)
    for s in STATIONS:
        p = CACHE / f"{s}.json"
        if p.exists() and time.time() - p.stat().st_mtime < 12 * 3600:
            continue  # resume: fresh cache
        evs = fetch_events(s, since)
        with ThreadPoolExecutor(2) as ex:
            candles = list(ex.map(fetch_candles, evs))
        fc = fetch_forecasts(s, since - timedelta(days=WINDOW + 5), date.today() - timedelta(days=1))
        cli = fetch_cli(s, sorted({since.year - (1 if since.month < 3 else 0), date.today().year}))
        for e, c in zip(evs, candles, strict=True):
            for m in e["markets"]:
                m["candles"] = c.get(m["ticker"], [])
        p.write_text(json.dumps({"events": evs, "fc": fc, "cli": cli}))
        print(f"{s}: {len(evs)} events, {sum(len(c) for c in candles)} markets w/ candles, cli {len(cli)}", flush=True)


# ---------- model ----------


def calib(fc: dict, cli: dict, d: date):
    """Rolling past-only bias per model and blend sd from the WINDOW days before d."""
    days = [(d - timedelta(days=i)).isoformat() for i in range(2, WINDOW + 2)]  # d-1 truth not final at snapshot
    bias, res = {}, []
    for m in MODELS:
        r = [cli[x] - fc[m][x] for x in days if x in cli and x in fc.get(m, {})]
        if len(r) >= MIN_HIST:
            bias[m] = mean(r)
    if not bias:
        return None
    for x in days:
        vals = [fc[m][x] + bias[m] for m in bias if x in fc[m]]
        if x in cli and vals:
            res.append(cli[x] - mean(vals))
    if len(res) < MIN_HIST:
        return None
    return bias, max(MIN_SD, pstdev(res))


def mu_sd(fc: dict, cli: dict, d: date):
    c = calib(fc, cli, d)
    if not c:
        return None
    bias, sd = c
    vals = [fc[m][d.isoformat()] + bias[m] for m in bias if d.isoformat() in fc[m]]
    return (mean(vals), sd) if vals else None


def quote_at(candles: list, ts: int):
    """Last hourly yes bid/ask at or before ts."""
    best = None
    for c in candles:
        if c["end_period_ts"] <= ts:
            best = c
    if not best:
        return None
    px = lambda k: best.get(k, {}).get("close_dollars", best.get(k, {}).get("close"))  # noqa: E731  historical uses "close"
    a, b = px("yes_ask"), px("yes_bid")
    if a is None or b is None:
        return None
    return float(a), float(b)


def snapshots(d: date, tz: str) -> dict:
    z = ZoneInfo(tz)
    return {
        "eve": int(datetime(d.year, d.month, d.day, 23, tzinfo=z).timestamp()) - 86400,
        "morn": int(datetime(d.year, d.month, d.day, 9, tzinfo=z).timestamp()),
    }


def rows_for(series: str, blob: dict) -> list:
    tz = STATIONS[series][3]
    out = []
    for e in blob["events"]:
        d = event_date(e["event_ticker"])
        ms = mu_sd(blob["fc"], blob["cli"], d)
        if not ms:
            continue
        mu, sd = ms
        for m in e["markets"]:
            if m.get("result") not in ("yes", "no"):
                continue
            p = prob(m, mu, sd)
            for snap, ts in snapshots(d, tz).items():
                q = quote_at(m.get("candles", []), ts)
                if q:
                    out.append({"series": series, "event": e["event_ticker"], "date": d.isoformat(), "snap": snap,
                                "ticker": m["ticker"], "p": p, "ask": q[0], "bid": q[1], "won": m["result"] == "yes"})
    return out


def grade(rows: list, thr: float) -> list:
    bets = []
    for r in rows:
        b = bet(r["p"], r["ask"], r["bid"], thr)
        if b:
            bets.append({**r, "side": b[0], "price": b[1], "pnl": settle(b[0], b[1], r["won"])})
    return bets


def summary(bets: list) -> dict:
    if not bets:
        return {"n": 0}
    staked = sum(int(STAKE // b["price"]) * b["price"] for b in bets)
    by_ev: dict = {}
    for b in bets:
        by_ev.setdefault(b["event"], []).append(b)
    groups = list(by_ev.values())
    rng = random.Random(7)
    boots = []
    for _ in range(2000):
        s = [rng.choice(groups) for _ in groups]
        pnl = sum(b["pnl"] for g in s for b in g)
        st = sum(int(STAKE // b["price"]) * b["price"] for g in s for b in g)
        boots.append(pnl / st)
    boots.sort()
    pnl = sum(b["pnl"] for b in bets)
    return {"n": len(bets), "events": len(groups), "win": sum((b["won"] == (b["side"] == "yes")) for b in bets) / len(bets),
            "pnl": round(pnl), "roi": pnl / staked, "ci90": (boots[100], boots[1900])}


def cmd_backtest() -> None:
    rows = []
    for s in STATIONS:
        p = CACHE / f"{s}.json"
        if p.exists():
            rows += rows_for(s, json.loads(p.read_text()))
    if not rows:
        sys.exit("no cache; run fetch first")
    cut = (max(date.fromisoformat(r["date"]) for r in rows) - timedelta(days=TEST_DAYS)).isoformat()
    train = [r for r in rows if r["date"] <= cut]
    test = [r for r in rows if r["date"] > cut]
    # Brier: model vs mid on the same quotes
    brier = lambda rs, f: mean((f(r) - r["won"]) ** 2 for r in rs)  # noqa: E731
    out = {"cut": cut, "rows": len(rows), "brier_model": brier(rows, lambda r: r["p"]),
           "brier_mid": brier(rows, lambda r: (r["ask"] + r["bid"]) / 2), "grid": {}}
    for snap in ("eve", "morn", "all"):
        for thr in THRESHOLDS:
            f = (lambda r: True) if snap == "all" else (lambda r, s=snap: r["snap"] == s)
            out["grid"][f"{snap}@{thr}"] = {"train": summary(grade([r for r in train if f(r)], thr)),
                                            "test": summary(grade([r for r in test if f(r)], thr))}
    best = max(out["grid"], key=lambda k: out["grid"][k]["train"].get("roi", -9) if out["grid"][k]["train"].get("n", 0) >= 100 else -9)
    out["pick"] = best
    t = out["grid"][best]["test"]
    out["gate"] = bool(t.get("n", 0) >= 200 and t.get("roi", -1) > 0.05)
    (CACHE / "backtest.json").write_text(json.dumps(out, indent=1, default=str))
    print(f"- rows {len(rows)}, train <= {cut}, test after")
    print(f"- Brier model {out['brier_model']:.4f} vs market mid {out['brier_mid']:.4f}")
    for k, v in out["grid"].items():
        tr, te = v["train"], v["test"]
        fmt = lambda s: f"n {s['n']:4d} roi {s['roi']:+6.1%} ci {s['ci90'][0]:+.0%}..{s['ci90'][1]:+.0%}" if s["n"] else "n    0"  # noqa: E731
        print(f"  {k:10} train {fmt(tr)} | test {fmt(te)}")
    print(f"- pick (best train ROI, n>=100): {best}")
    print(f"- gate (test ROI > +5% on 200+ bets): {'PASS' if out['gate'] else 'FAIL'}")
    tb = grade([r for r in test if best.startswith('all') or r['snap'] == best.split('@')[0]], float(best.split('@')[1]))
    for s in STATIONS:
        sm = summary([b for b in tb if b["series"] == s])
        if sm["n"]:
            print(f"  {s:12} n {sm['n']:3d} roi {sm['roi']:+.1%}")


# ---------- scan ----------


def cmd_scan(thr: float) -> None:
    for s, (_, lat, lon, tz) in STATIONS.items():
        p = CACHE / f"{s}.json"
        if not p.exists():
            continue
        blob = json.loads(p.read_text())
        q = {"latitude": lat, "longitude": lon, "timezone": tz, "temperature_unit": "fahrenheit",
             "hourly": "temperature_2m", "models": ",".join(MODELS), "forecast_days": 3}
        h = get(f"{OM_FC}?{urllib.parse.urlencode(q)}")["hourly"]
        fc = {m: {**blob["fc"].get(m, {}), **daily_max(h, f"temperature_2m_{m}")} for m in MODELS}
        evs = get(f"{K}/events?series_ticker={s}&status=open&with_nested_markets=true")["events"]
        for e in evs:
            d = event_date(e["event_ticker"])
            ms = mu_sd(fc, blob["cli"], d)
            if not ms:
                continue
            for m in e["markets"]:
                pr = prob(m, *ms)
                b = bet(pr, float(m["yes_ask_dollars"]), float(m["yes_bid_dollars"]), thr)
                if b:
                    print(f"{m['ticker']:28} mu {ms[0]:5.1f} sd {ms[1]:.1f} p {pr:.2f} buy {b[0]} @{b[1]:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--days", type=int, default=120)
    sub.add_parser("backtest")
    sc = sub.add_parser("scan")
    sc.add_argument("--thr", type=float, default=0.10)
    a = ap.parse_args()
    {"fetch": lambda: cmd_fetch(a.days), "backtest": cmd_backtest, "scan": lambda: cmd_scan(a.thr)}[a.cmd]()


if __name__ == "__main__":
    main()
