"""Paper-only nowcast lane: Kalshi weekly/daily markets that settle on daily-published data (TSA screenings,
AAA national gas) priced against a forecast built from the days already published.

uv run --env-file .env python scripts/edge/nowcast.py backtest | scan [--min-edge 0.05]

Never places an order. Kalshi taker fee included, $200 a bet.
shortcut: one bet per market (first snapshot clearing the threshold); quotes are last hourly candle at or before
the snapshot and assumed fillable for $200; gas history is only what Kalshi's own settled rules expose.
"""
import urllib.error, bisect, json, math, random, re, subprocess, sys, time, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STAKE = 200.0
THRESHOLDS = (0.03, 0.05, 0.10)
HOLDOUT_DAYS = 30
MIN_ERRS = 20
HOME = Path.home() / ".polywatch" / "edge"
K = "https://api.elections.kalshi.com/trade-api/v2"
TSA_URL = "https://www.tsa.gov/travel/passenger-volumes"
TSA_ERR_START = date(2023, 3, 6)  # skip the COVID recovery when calibrating errors
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def get(url: str):
    for wait in (2, 5, 10, 20, 40, 60, 60, 0):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pw"}), timeout=30))
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError) as e:
            if not wait or (isinstance(e, urllib.error.HTTPError) and e.code != 429):
                raise
            time.sleep(wait)


def kalshi_fee(contracts: int, p: float) -> float:
    return math.ceil(0.07 * contracts * p * (1 - p) * 100 - 1e-9) / 100


def settles_yes(strike_type: str, floor, cap, value: float) -> bool:
    return {"greater": lambda: value > floor, "greater_or_equal": lambda: value >= floor,
            "less": lambda: value < cap if cap is not None else value < floor,
            "less_or_equal": lambda: value <= cap if cap is not None else value <= floor,
            "between": lambda: floor <= value <= cap}[strike_type]()


def bet_pnl(side_yes: bool, ask: float, result_yes: bool, stake: float = STAKE):
    """Returns (cost, pnl) for buying `stake` dollars of contracts at `ask` (price of the side bought)."""
    c = int(stake / ask + 1e-9)
    if c < 1:
        return 0.0, 0.0
    cost = c * ask + kalshi_fee(c, ask)
    return cost, (c if side_yes == result_yes else 0) - cost


def phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def exceed_prob(errs, t: float) -> float:
    """P(error > t) from an empirical sample smoothed with a Silverman-bandwidth normal kernel."""
    n = len(errs)
    m = sum(errs) / n
    sd = math.sqrt(sum((e - m) ** 2 for e in errs) / max(n - 1, 1)) or 1e-9
    bw = 0.9 * sd * n ** -0.2
    return sum(1 - phi((t - e) / bw) for e in errs) / n


def cond_prob(strike_type: str, floor, cap, cdf) -> float:
    """cdf(x) = P(X <= x); continuous variable so strict and loose inequalities coincide."""
    if strike_type in ("greater", "greater_or_equal"):
        return 1 - cdf(floor)
    if strike_type in ("less", "less_or_equal"):
        return cdf(cap if cap is not None else floor)
    return cdf(cap) - cdf(floor)


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def parse_event_date(ev: str) -> date:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", ev)
    return date(2000 + int(m[1]), MONTHS.index(m[2]) + 1, int(m[3]))


# ---------- TSA ----------

def parse_tsa(html: str) -> dict:
    out = {}
    for d, v in re.findall(r"<td[^>]*>(\d+/\d+/\d{4})</td>\s*<td[^>]*>([\d,]+)</td>", html):
        mo, dd, yy = map(int, d.split("/"))
        out[date(yy, mo, dd)] = int(v.replace(",", ""))
    return out


def tsa_load(refresh: bool = False) -> dict:
    f = HOME / "tsa_daily.json"
    daily = {date.fromisoformat(k): v for k, v in json.load(open(f)).items()} if f.exists() else {}
    if refresh or not daily:
        for y in range(2022, date.today().year + 1):
            url = TSA_URL if y == date.today().year else f"{TSA_URL}/{y}"
            # tsa.gov answers urllib 403; curl with a browser UA gets the page
            daily.update(parse_tsa(subprocess.run(["curl", "-s", "-m", "30", "-A", "Mozilla/5.0", url],
                                                  capture_output=True, text=True, check=True).stdout))
        HOME.mkdir(parents=True, exist_ok=True)
        json.dump({k.isoformat(): v for k, v in sorted(daily.items())}, open(f, "w"))
    return daily


def tsa_forecast(daily: dict, monday: date, k: int):
    """Weekly average given days monday..monday+k-1 published: remaining days = same weekday last year (364d back)
    times the mean of the trailing 7 and 14 day YoY ratios. None if any needed day is missing."""
    last = monday + timedelta(days=k - 1)
    ratios = []
    for w in (7, 14):
        days = [last - timedelta(days=i) for i in range(w)]
        if any(d not in daily or d - timedelta(days=364) not in daily for d in days):
            return None
        ratios.append(sum(daily[d] for d in days) / sum(daily[d - timedelta(days=364)] for d in days))
    r = sum(ratios) / 2
    tot = 0.0
    for i in range(7):
        d = monday + timedelta(days=i)
        if i < k:
            if d not in daily:
                return None
            tot += daily[d]
        else:
            if d - timedelta(days=364) not in daily:
                return None
            tot += daily[d - timedelta(days=364)] * r
    return tot / 7


def tsa_errors(daily: dict, before: date, k: int) -> list:
    """ln(actual / forecast) for every full week that ended before `before`, replayed with only k days known."""
    out = []
    m = TSA_ERR_START
    while m + timedelta(days=6) < before:
        if all(m + timedelta(days=i) in daily for i in range(7)):
            f = tsa_forecast(daily, m, k)
            if f:
                out.append(math.log(sum(daily[m + timedelta(days=i)] for i in range(7)) / 7 / f))
        m += timedelta(days=7)
    return out


# ---------- AAA gas ----------

def gas_load(refresh: bool = False) -> dict:
    """Daily US national average from the expiration_value of Kalshi's settled daily and weekly AAA markets."""
    f = HOME / "gas_daily.json"
    gas = {date.fromisoformat(k): v for k, v in json.load(open(f)).items()} if f.exists() else {}
    if refresh or not gas:
        for s in ("KXAAAGASD", "KXAAAGASW"):
            cur = ""
            while True:
                r = get(f"{K}/markets?series_ticker={s}&status=settled&limit=1000" + (f"&cursor={cur}" if cur else ""))
                for m in r["markets"]:
                    if num(m.get("expiration_value")) is not None:
                        gas[parse_event_date(m["event_ticker"])] = num(m["expiration_value"])
                cur = r.get("cursor")
                if not cur:
                    break
        HOME.mkdir(parents=True, exist_ok=True)
        json.dump({k.isoformat(): v for k, v in sorted(gas.items())}, open(f, "w"))
    return gas


def aaa_today():
    try:
        out = subprocess.run([str(Path.home() / ".claude/bin/sfetch"), "https://gasprices.aaa.com/", "--html"],
                             capture_output=True, text=True, timeout=120).stdout
        return float(re.search(r"\$(\d\.\d{3})", out)[1])
    except Exception:
        return None


def gas_forecast(gas: dict, last: date, h: int):
    """Random walk with drift: last value + h * mean daily change over the trailing 14 days."""
    days = [last - timedelta(days=i) for i in range(15)]
    if any(d not in gas for d in days):
        return None
    drift = (gas[last] - gas[last - timedelta(days=14)]) / 14
    return gas[last] + h * drift


def gas_errors(gas: dict, before: date, h: int) -> list:
    """Realised h-day change minus the drift forecast, for origins whose target day is before `before`."""
    out = []
    for o in sorted(gas):
        if o + timedelta(days=h) >= before or o + timedelta(days=h) not in gas:
            continue
        f = gas_forecast(gas, o, h)
        if f is not None:
            out.append(gas[o + timedelta(days=h)] - f)
    return out


# ---------- markets and quotes ----------

def settled(series: str) -> list:
    out, cur = [], ""
    while True:
        r = get(f"{K}/markets?series_ticker={series}&status=settled&limit=1000" + (f"&cursor={cur}" if cur else ""))
        out += [m for m in r["markets"] if m.get("result") in ("yes", "no")]
        cur = r.get("cursor")
        if not cur:
            return out


def ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def candles(series: str, m: dict, cache: dict) -> list:
    t = m["ticker"]
    if t not in cache:
        time.sleep(0.3)
        r = get(f"{K}/series/{series}/markets/{t}/candlesticks?start_ts={ts(m['open_time']) - 3600}"
                f"&end_ts={ts(m['close_time']) + 3600}&period_interval=60")
        cache[t] = [(c["end_period_ts"], c["yes_ask"].get("close_dollars"), c["yes_bid"].get("close_dollars"))
                    for c in r.get("candlesticks", []) if c.get("yes_ask")]
    return cache[t]


def quote(cs: list, snap_ts: int):
    """(yes_ask, no_ask) from the last candle closing at or before snap_ts; None if none or one-sided."""
    i = bisect.bisect_right([c[0] for c in cs], snap_ts) - 1
    if i < 0:
        return None
    ya = float(cs[i][1]) if cs[i][1] else None
    yb = float(cs[i][2]) if cs[i][2] else None
    return (ya if ya and 0 < ya < 1 else None, 1 - yb if yb and 0 < yb < 1 else None)


def evaluate(p_yes: float, q, thr: float):
    """Best side with edge > thr (model prob minus ask, fee excluded) as (side_yes, ask, edge) or None."""
    best = None
    for side_yes, p, ask in ((True, p_yes, q[0]), (False, 1 - p_yes, q[1])):
        if ask and 0.03 <= ask <= 0.97 and p - ask > thr and (best is None or p - ask > best[2]):
            best = (side_yes, ask, p - ask)
    return best


def snapshots(series: str, m: dict):
    """Yield (snap_datetime, kind, n) per snapshot: TSA n = days published; gas n = horizon in days."""
    ev = parse_event_date(m["event_ticker"])
    if series == "KXTSAW":
        monday = ev - timedelta(days=6)
        for k in range(1, 7):
            yield datetime.combine(monday + timedelta(days=k), datetime.min.time(), timezone.utc) + timedelta(hours=15), k
    elif series == "KXAAAGASW":
        for back in (2, 1):
            yield datetime.combine(ev - timedelta(days=back), datetime.min.time(), timezone.utc) + timedelta(hours=15), back
    else:
        yield datetime.combine(ev - timedelta(days=1), datetime.min.time(), timezone.utc) + timedelta(hours=15), 1


def model_prob(series: str, m: dict, snap: datetime, n: int, tsa: dict, gas: dict):
    ev = parse_event_date(m["event_ticker"])
    st, fl, cp = m["strike_type"], m.get("floor_strike"), m.get("cap_strike")
    if series == "KXTSAW":
        monday = ev - timedelta(days=6)
        f, errs = tsa_forecast(tsa, monday, n), tsa_errors(tsa, monday, n)
        if f is None or len(errs) < MIN_ERRS:
            return None
        return cond_prob(st, fl, cp, lambda x: 1 - exceed_prob(errs, math.log(x / f)))
    last = snap.date()
    f, errs = gas_forecast(gas, last, n), gas_errors(gas, ev, n)
    if f is None or len(errs) < MIN_ERRS:
        return None
    return cond_prob(st, fl, cp, lambda x: 1 - exceed_prob(errs, x - f))


# ---------- backtest ----------

def roi_stats(bets: list, iters: int = 2000, seed: int = 7) -> dict:
    if not bets:
        return {"n": 0}
    cost = sum(b["cost"] for b in bets)
    pnl = sum(b["pnl"] for b in bets)
    wk = {}
    for b in bets:
        c, p = wk.get(b["week"], (0.0, 0.0))
        wk[b["week"]] = (c + b["cost"], p + b["pnl"])
    rng, keys, rois = random.Random(seed), list(wk), []
    for _ in range(iters):
        s = [wk[rng.choice(keys)] for _ in keys]
        c = sum(x[0] for x in s)
        rois.append(sum(x[1] for x in s) / c if c else 0.0)
    rois.sort()
    return {"n": len(bets), "weeks": len(wk), "win": round(sum(b["pnl"] > 0 for b in bets) / len(bets), 3),
            "roi": round(pnl / cost, 4), "pnl": round(pnl, 2),
            "ci90": [round(rois[int(.05 * iters)], 4), round(rois[int(.95 * iters)], 4)]}


def collect(tsa: dict, gas: dict) -> list:
    """One row per (market, snapshot) with model prob, quotes and the realised result."""
    HOME.mkdir(parents=True, exist_ok=True)
    cf = HOME / "nowcast_candles.json"
    cache = json.load(open(cf)) if cf.exists() else {}
    rows, mism = [], 0
    for series in ("KXTSAW", "KXAAAGASW", "KXAAAGASD"):
        for m in settled(series):
            cs = candles(series, m, cache)
            if len(cache) % 50 == 0:
                json.dump(cache, open(cf, "w"))
            yes = m["result"] == "yes"
            v = num(m.get("expiration_value"))
            if v is not None:
                mism += settles_yes(m["strike_type"], m.get("floor_strike"), m.get("cap_strike"), v) != yes
            for snap, n in snapshots(series, m):
                q = quote(cs, int(snap.timestamp()))
                p = model_prob(series, m, snap, n, tsa, gas) if q else None
                if p is not None:
                    rows.append({"series": series, "ticker": m["ticker"], "week": m["event_ticker"], "n": n, "snap": snap.isoformat(),
                                 "close": m["close_time"], "p": p, "q": q, "yes": yes})
        json.dump(cache, open(cf, "w"))
    print(f"- settle() vs Kalshi result: {mism} mismatches", file=sys.stderr)
    return rows


def run_bets(rows: list, thr: float) -> list:
    seen, bets = set(), []
    for r in sorted(rows, key=lambda r: r["snap"]):
        if r["ticker"] in seen:
            continue
        e = evaluate(r["p"], r["q"], thr)
        if e:
            seen.add(r["ticker"])
            cost, pnl = bet_pnl(e[0], e[1], r["yes"])
            bets.append({"series": r["series"], "ticker": r["ticker"], "week": r["week"], "close": r["close"], "side_yes": e[0],
                         "ask": e[1], "edge": round(e[2], 4), "cost": cost, "pnl": pnl})
    return bets


def cmd_backtest():
    tsa, gas = tsa_load(True), gas_load(True)
    rows = collect(tsa, gas)
    cut = (max(datetime.fromisoformat(r["close"].replace("Z", "+00:00")) for r in rows) - timedelta(days=HOLDOUT_DAYS)).isoformat()
    split = lambda b, hold: [x for x in b if (x["close"] >= cut.replace("+00:00", "Z")) == hold]
    res = {"gas_days": len(gas), "gas_range": [min(gas).isoformat(), max(gas).isoformat()], "tsa_days": len(tsa),
           "holdout_from": cut, "snapshots": len(rows), "table": {}}
    pick = None
    for thr in THRESHOLDS:
        bets = run_bets(rows, thr)
        res["table"][str(thr)] = {"train": roi_stats(split(bets, False)), "holdout": roi_stats(split(bets, True)),
                                  "by_series_holdout": {s: roi_stats([b for b in split(bets, True) if b["series"] == s])
                                                        for s in ("KXTSAW", "KXAAAGASW", "KXAAAGASD")}}
        t = res["table"][str(thr)]["train"]
        if t["n"] >= 30 and (pick is None or t["roi"] > res["table"][str(pick)]["train"]["roi"]):
            pick = thr
    res["chosen_threshold"] = pick
    h = res["table"][str(pick)]["holdout"] if pick else {"n": 0}
    res["gate_pass"] = bool(pick and h["n"] >= 200 and h["roi"] > 0.05)
    json.dump(res, open(HOME / "nowcast_backtest.json", "w"), indent=1)
    print(f"- history: TSA {res['tsa_days']} days (2022 on), AAA {res['gas_days']} days {res['gas_range'][0]}..{res['gas_range'][1]}")
    print(f"- holdout: closes on/after {cut[:10]}; threshold chosen on train: {pick}")
    print("thr   set      n   weeks  win    roi      ci90")
    for thr, t in res["table"].items():
        for k in ("train", "holdout"):
            s = t[k]
            print(f"{thr:<5} {k:<8} {s['n']:<4} {s.get('weeks', 0):<5} {s.get('win', '-'):<6} {s.get('roi', '-'):<8} {s.get('ci90', '-')}")
    print(f"- gate (ROI > +5% on 200+ holdout bets): {'PASS' if res['gate_pass'] else 'FAIL'}; holdout n={h['n']}")


def cmd_scan(thr: float):
    tsa, gas = tsa_load(True), gas_load(True)
    today = date.today()
    v = aaa_today()
    if v:
        gas[today] = v
    found = 0
    for series in ("KXTSAW", "KXAAAGASW", "KXAAAGASD"):
        for m in get(f"{K}/markets?series_ticker={series}&status=open&limit=1000")["markets"]:
            ev = parse_event_date(m["event_ticker"])
            if series == "KXTSAW":
                monday = ev - timedelta(days=6)
                n = max((i for i in range(1, 7) if all(monday + timedelta(days=j) in tsa for j in range(i))), default=0)
                snap = datetime.now(timezone.utc)
            else:
                n, snap = (ev - today).days, datetime.combine(today, datetime.min.time(), timezone.utc)
                n = max(n, 1)
            if n < 1:
                continue
            p = model_prob(series, m, snap, n, tsa, gas)
            ya, yb = float(m["yes_ask_dollars"]), float(m["yes_bid_dollars"])
            e = evaluate(p, (ya, 1 - yb), thr) if p is not None else None
            if e:
                found += 1
                print(f"- {m['ticker']} {'YES' if e[0] else 'NO'} ask {e[1]:.2f} model {p if e[0] else 1 - p:.2f} edge {e[2]:+.2f}")
    print(f"- {found} markets with edge > {thr}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["backtest"]:
        cmd_backtest()
    elif a[:1] == ["scan"]:
        cmd_scan(float(a[a.index("--min-edge") + 1]) if "--min-edge" in a else 0.05)
    else:
        print(__doc__)
