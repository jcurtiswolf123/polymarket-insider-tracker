"""Paper research lane: are "will price hit X" ladder markets mispriced vs an options-implied touch probability?

uv run --env-file .env python scripts/edge/touch.py backtest | scan
Kalshi is tradable; Polymarket is data only (geoblocked). Paper only: this file never places an order.
shortcut: driftless-martingale GBM, 30d DVOL / OVX / GVZ as the vol for every horizon, no term structure; upgrade with ATM IV per expiry.
"""
import hashlib, json, math, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

STAKE = 200.0
THRESHOLDS = (0.03, 0.05, 0.10)
OFFSETS = (7, 3, 1)  # days before the scheduled end
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
TEST_DAYS = 30
MIN_PX, MAX_PX = 0.02, 0.98
HOME = Path.home() / ".polywatch" / "edge"
CACHE = HOME / "cache"
OUT = HOME / "touch_backtest.json"
K = "https://api.elections.kalshi.com/trade-api/v2"
G = "https://gamma-api.polymarket.com"
DER = "https://www.deribit.com/api/v2/public"
YH = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = {"User-Agent": "Mozilla/5.0"}
# asset -> spot source, vol source, monitoring step in years (0 = continuous)
ASSETS = {"BTC": ("der", "BTC", 0.0), "ETH": ("der", "ETH", 0.0),
          "WTI": ("yh", "CL=F", "^OVX", 1 / 252), "GOLD": ("yh", "GC=F", "^GVZ", 0.0)}
KALSHI = {"KXBTCMAXMON": "BTC", "KXBTCMINMON": "BTC", "KXETHMAXMON": "ETH", "KXETHMINMON": "ETH",
          "KXWTIMAX": "WTI", "KXWTIMIN": "WTI", "KXWTIMAXM": "WTI", "KXWTIMINM": "WTI"}
PM_NAMES = {"BTC": "bitcoin", "ETH": "ethereum", "WTI": "wti", "GOLD": "xauusd"}
MONTHS = "january february march april may june july august september october november december".split()


def get(url: str, cache: bool = False):
    f = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    err = None
    for i in range(7):
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
            if cache:
                CACHE.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(d))
            return d
        except urllib.error.HTTPError as e:
            err = e
            if e.code in (400, 404):
                return None
            time.sleep(2 * (i + 1))
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    print(f"get failed {url[:120]}: {err}", file=sys.stderr)
    return None


def kalshi_fee(contracts: int, p: float) -> float:
    return math.ceil(round(0.07 * contracts * p * (1 - p) * 100, 6)) / 100  # taker fee, rounded up to the cent


def ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def touch_prob(S: float, B: float, sigma: float, T: float, drift: float | None = None, dt: float = 0.0) -> float:
    """P(GBM touches B within T years). drift is the log drift per year, default -sigma^2/2 (martingale).
    dt > 0 applies the Broadie-Glasserman shift for a barrier checked every dt years."""
    if B == S:
        return 1.0
    up = B > S
    if T <= 0 or sigma <= 0:
        return 0.0
    mu = -0.5 * sigma * sigma if drift is None else drift
    if dt > 0:
        B *= math.exp((0.5826 if up else -0.5826) * sigma * math.sqrt(dt))
        if (B <= S) == up:  # shift crossed the spot
            return 1.0
    b = abs(math.log(B / S))
    if not up:
        mu = -mu
    s = sigma * math.sqrt(T)
    p = ncdf((mu * T - b) / s) + math.exp(min(2 * mu * b / (sigma * sigma), 700)) * ncdf((-b - mu * T) / s)
    return min(1.0, max(0.0, p))


def place_bet(p_model: float, ask_yes: float | None, bid_yes: float | None, thr: float, kalshi: bool):
    """Buy the side with the larger edge if it clears thr. Returns (side, price) or None."""
    best = None
    for side, price, q in (("yes", ask_yes, p_model), ("no", None if bid_yes is None else 1 - bid_yes, 1 - p_model)):
        if price is None or not MIN_PX <= price <= MAX_PX:
            continue
        e = q - price
        if e > thr and (best is None or e > best[0]):
            best = (e, side, price)
    return None if best is None else (best[1], best[2], best[0])


def settle(side: str, price: float, outcome: int, kalshi: bool) -> dict:
    n = int(STAKE // price)
    if n < 1:
        return {"n": 0, "cost": 0.0, "pnl": 0.0, "win": 0}
    cost = n * price + (kalshi_fee(n, price) if kalshi else 0.0)
    win = int(outcome == (1 if side == "yes" else 0))
    return {"n": n, "cost": cost, "pnl": n * win - cost, "win": win}


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def cdl(d: dict | None, k: str):
    if not d:
        return None
    v = d.get(k + "_dollars", d.get(k))
    return None if v in (None, "") else float(v)


# ---------- underlying data ----------
class Asset:
    def __init__(self, name: str, t0: float, t1: float):
        self.name, self.cfg = name, ASSETS[name]
        self.dt = self.cfg[-1]
        if self.cfg[0] == "der":
            self.px = self._deribit("get_tradingview_chart_data", {"instrument_name": f"{self.cfg[1]}-PERPETUAL", "resolution": 60}, t0, t1)
            self.vol = self._deribit("get_volatility_index_data", {"currency": self.cfg[1], "resolution": 3600}, t0, t1)
            self.vol = self.vol[:, [0, 4]] if len(self.vol) else self.vol
            self.vol_lag = 3600
        else:
            self.px = self._yahoo(self.cfg[1], "1h", t0, t1)
            self.vol = self._yahoo(self.cfg[2], "1d", t0, t1)[:, [0, 4]]
            self.vol_lag = 86400
        self.vol = self.vol.copy()
        self.vol[:, 1] /= 100

    def _deribit(self, ep: str, q: dict, t0: float, t1: float) -> np.ndarray:
        ch, rows = 30 * 86400, []
        for a in range(int(t0 // ch * ch), int(t1), ch):
            b = a + ch
            qs = "&".join(f"{k}={v}" for k, v in q.items())
            d = get(f"{DER}/{ep}?{qs}&start_timestamp={a * 1000}&end_timestamp={b * 1000}", cache=b < time.time() - 7200)
            r = (d or {}).get("result") or {}
            if "ticks" in r:
                rows += [[t / 1000, o, h, lo, c] for t, o, h, lo, c in zip(r["ticks"], r["open"], r["high"], r["low"], r["close"])]
            else:
                rows += [[x[0] / 1000, *x[1:5]] for x in r.get("data", [])]
        return np.array(sorted({r[0]: r for r in rows}.values())) if rows else np.zeros((0, 5))

    def _yahoo(self, sym: str, iv: str, t0: float, t1: float) -> np.ndarray:
        d = get(f"{YH}{urllib.request.quote(sym)}?period1={int(t0)}&period2={int(t1)}&interval={iv}")
        r = d["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        rows = [[t, o, h, lo, c] for t, o, h, lo, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, lo, c)]
        return np.array(rows)

    def spot(self, t: float):
        i = int(np.searchsorted(self.px[:, 0] + 3600, t, "right")) - 1
        return float(self.px[i, 4]) if i >= 0 and t - self.px[i, 0] - 3600 < 4 * 86400 else None

    def sigma(self, t: float):
        i = int(np.searchsorted(self.vol[:, 0] + self.vol_lag, t, "right")) - 1
        return float(self.vol[i, 1]) if i >= 0 and t - self.vol[i, 0] - self.vol_lag < 4 * 86400 else None

    def touched(self, a: float, t: float, up: bool, B: float, closes: bool = False) -> bool:
        """Has the path already crossed B since a? closes=True checks hourly closes (a stand-in for daily settles)."""
        m = (self.px[:, 0] >= a - 3600) & (self.px[:, 0] + 3600 <= t)
        if not m.any():
            return False
        hi, lo = (self.px[m, 4].max(), self.px[m, 4].min()) if closes else (self.px[m, 2].max(), self.px[m, 3].min())
        return bool(hi >= B if up else lo <= B)


def make_row(venue, asset, event, ticker, up, B, t, end, A, ask, bid, outcome, snap, extra=None):
    S, sg = A.spot(t), A.sigma(t)
    if S is None or sg is None:
        return None
    p = touch_prob(S, B, sg, (end - t) / (365 * 86400), dt=A.dt)
    return {"venue": venue, "asset": asset, "event": event, "ticker": ticker, "up": up, "B": B, "snap": snap, "t": t, "S": S,
            "sigma": sg, "p": p, "ask": ask, "bid": bid, "outcome": outcome, **(extra or {})}


# ---------- Kalshi ----------
def event_end(et: str, asset: str) -> float | None:
    """Scheduled end from the event ticker (26NOV30). Settled markets in a still-running event close early, so max(close_time) lies."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", et)
    if not m:
        return None
    d = datetime(2000 + int(m[1]), [x[:3].upper() for x in MONTHS].index(m[2]) + 1, int(m[3]), tzinfo=timezone.utc)
    return (d + (timedelta(days=1, hours=3, minutes=59, seconds=59) if asset != "WTI" else timedelta(hours=18, minutes=30))).timestamp()


def kalshi_markets(statuses=("settled",)):
    out = {}
    for s in KALSHI:
        for ep, st in (("markets", True), ("historical/markets", False)):
            cur = ""
            while True:
                d = get(f"{K}/{ep}?series_ticker={s}&limit=1000" + (f"&status={statuses[0]}" if st else "") + (f"&cursor={cur}" if cur else ""))
                for m in (d or {}).get("markets", []):
                    out[m["ticker"]] = {**m, "_series": s}
                cur = (d or {}).get("cursor", "")
                if not cur or not d.get("markets"):
                    break
    return out


def kalshi_quote(m: dict, end: float):
    a, b = end - 8 * 86400, end - 86400 + 3600
    base = f"{K}/series/{m['_series']}/markets/{m['ticker']}/candlesticks?start_ts={int(a)}&end_ts={int(b)}&period_interval=60"
    d = get(base, cache=True) or get(f"{K}/historical/markets/{m['ticker']}/candlesticks?start_ts={int(a)}&end_ts={int(b)}&period_interval=60", cache=True)
    return (d or {}).get("candlesticks", [])


def pick_quote(cs: list, t: float):
    prev = [c for c in cs if c["end_period_ts"] <= t and t - c["end_period_ts"] < 86400]
    if not prev:
        return None, None
    c = prev[-1]
    ask, bid = cdl(c.get("yes_ask"), "close"), cdl(c.get("yes_bid"), "close")
    return ask, bid


def kalshi_rows(A: dict):
    mk = kalshi_markets()
    ev = {}
    for m in mk.values():
        ev.setdefault(m["event_ticker"], []).append(m)
    jobs = []
    for et, ms in ev.items():
        end = event_end(et, KALSHI[ms[0]["_series"]])
        if end is None or end < SINCE or end > time.time():
            continue
        for m in ms:
            if m.get("result") not in ("yes", "no") or m["_series"] not in KALSHI:
                continue
            up = m["strike_type"] == "greater"
            B = m["floor_strike"] if up else m["cap_strike"]
            if B is None:
                continue
            for off in OFFSETS:
                t = end - off * 86400
                if ts(m["open_time"]) <= t < ts(m["close_time"]):
                    jobs.append((m, et, up, float(B), t, end, off, ts(m["open_time"])))
    print(f"kalshi: {len(mk)} settled markets, {len(jobs)} snapshots to price", file=sys.stderr)

    def one(j):
        m, et, up, B, t, end, off, op = j
        if KALSHI[m["_series"]] == "WTI" and A["WTI"].touched(op, t, up, B, True):
            return None  # settle path already crossed: market is a pending yes, not a bet
        ask, bid = pick_quote(kalshi_quote(m, end), t)
        if ask is None and bid is None:
            return None
        return make_row("kalshi", KALSHI[m["_series"]], et, m["ticker"], up, B, t, end, A[KALSHI[m["_series"]]], ask, bid, int(m["result"] == "yes"), off, {"close": ts(m["close_time"])})

    with ThreadPoolExecutor(4) as ex:
        rows = [r for r in ex.map(one, jobs) if r]
    return rows, len(jobs)


# ---------- Polymarket (data only) ----------
def pm_events(name: str, since: float):
    now = datetime.now(timezone.utc)
    y, mo = 2026, 1
    while (y, mo) <= (now.year, now.month):
        e = get(f"{G}/events?slug=what-price-will-{name}-hit-in-{MONTHS[mo - 1]}-{y}", cache=(y, mo) < (now.year, now.month))
        if e:
            yield e[0]
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)


def pm_rows(A: dict):
    jobs = []
    for asset, name in PM_NAMES.items():
        for e in pm_events(name, SINCE):
            end = ts(e["endDate"])
            for m in e["markets"]:
                try:
                    pr = json.loads(m["outcomePrices"])
                    yes_tok = json.loads(m["clobTokenIds"])[0]
                except Exception:
                    continue
                if not m.get("closed") or sorted(pr) != ["0", "1"]:
                    continue
                g = m.get("groupItemTitle") or ""
                num = "".join(ch for ch in g if ch.isdigit() or ch == ".")
                if not num or g[:1] not in "↑↓":
                    continue
                up, B = g[0] == "↑", float(num)
                yes = pr[0] == "1"
                start = ts(m["startDate"])
                mstart = datetime.fromtimestamp(end - 3600, timezone.utc).replace(day=1, hour=0, minute=0, second=0).timestamp()
                closed = ts(m["closedTime"]) if m.get("closedTime") else end + 1
                for off in OFFSETS:
                    t = end - off * 86400
                    if t >= start and t < min(end, closed if yes else end):
                        jobs.append((asset, e["slug"], m["slug"], yes_tok, up, B, t, end, off, int(yes), mstart if asset in ("BTC", "ETH") else start))
    print(f"polymarket: {len(jobs)} snapshots to price", file=sys.stderr)

    def one(j):
        asset, ev, mslug, tok, up, B, t, end, off, yes, a0 = j
        d = get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={int(end - 8 * 86400)}&endTs={int(end - 86400 + 3600)}&fidelity=60", cache=True)
        h = [x for x in (d or {}).get("history", []) if x["t"] <= t and t - x["t"] < 6 * 3600]
        if not h or A[asset].touched(a0, t, up, B):
            return None
        p = h[-1]["p"]
        return make_row("polymarket", asset, ev, mslug, up, B, t, end, A[asset], min(p + 0.005, 0.99), max(p - 0.005, 0.01), yes, off, {"close": end})

    with ThreadPoolExecutor(4) as ex:
        rows = [r for r in ex.map(one, jobs) if r]
    return rows, len(jobs)


# ---------- evaluation ----------
def bets(rows: list, thr: float) -> list:
    out = []
    for r in rows:
        b = place_bet(r["p"], r["ask"], r["bid"], thr, r["venue"] == "kalshi")
        if b:
            s = settle(b[0], b[1], r["outcome"], r["venue"] == "kalshi")
            if s["n"]:
                out.append({**s, "event": r["event"], "side": b[0], "price": b[1], "edge": b[2], "ticker": r["ticker"], "snap": r["snap"]})
    return out


def stats(bs: list, boot: int = 2000, seed: int = 7) -> dict:
    if not bs:
        return {"n": 0, "win": None, "roi": None, "ci90": None, "pnl": 0.0, "events": 0}
    cost, pnl = sum(b["cost"] for b in bs), sum(b["pnl"] for b in bs)
    ev = {}
    for b in bs:
        c = ev.setdefault(b["event"], [0.0, 0.0])
        c[0] += b["cost"]
        c[1] += b["pnl"]
    a = np.array(list(ev.values()))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a), (boot, len(a)))
    roi = a[idx, 1].sum(1) / a[idx, 0].sum(1)
    return {"n": len(bs), "win": sum(b["win"] for b in bs) / len(bs), "roi": pnl / cost, "pnl": pnl, "events": len(ev),
            "ci90": [float(np.percentile(roi, 5)), float(np.percentile(roi, 95))]}


def evaluate(rows: list, cutoff: float) -> dict:
    res = {}
    for venue in ("kalshi", "polymarket"):
        vr = [r for r in rows if r["venue"] == venue]
        for asset in [None] + sorted({r["asset"] for r in vr}):
            ar = [r for r in vr if asset in (None, r["asset"])]
            train = [r for r in ar if r["t"] < cutoff and r["close"] <= cutoff]
            test = [r for r in ar if r["t"] >= cutoff]
            for thr in THRESHOLDS:
                res[f"{venue}|{asset or 'ALL'}|{thr}"] = {"train": stats(bets(train, thr)), "test": stats(bets(test, thr))}
    return res


def choose(res: dict, venue: str) -> float:
    ok = [(res[f"{venue}|ALL|{t}"]["train"], t) for t in THRESHOLDS if res[f"{venue}|ALL|{t}"]["train"]["n"] >= 30]
    return max(ok, key=lambda x: x[0]["roi"])[1] if ok else 0.05


def fmt(s: dict) -> str:
    if not s["n"]:
        return "n=0"
    return f"n={s['n']} ev={s['events']} win={s['win']:.0%} roi={s['roi']:+.1%} ci90=[{s['ci90'][0]:+.1%},{s['ci90'][1]:+.1%}]"


def backtest():
    HOME.mkdir(parents=True, exist_ok=True)
    now = time.time()
    t0, t1 = SINCE - 10 * 86400, now
    with ThreadPoolExecutor(4) as ex:
        A = dict(zip(ASSETS, ex.map(lambda n: Asset(n, t0, t1), ASSETS)))
    krows, kj = kalshi_rows(A)
    prows, pj = pm_rows(A)
    rows = krows + prows
    cutoff = max(r["t"] for r in rows) - TEST_DAYS * 86400
    res = evaluate(rows, cutoff)
    chosen = {v: choose(res, v) for v in ("kalshi", "polymarket")}
    gate = {}
    for v in chosen:
        s = res[f"{v}|ALL|{chosen[v]}"]["test"]
        gate[v] = {"thr": chosen[v], "n": s["n"], "roi": s["roi"], "pass": bool(s["n"] >= 200 and s["roi"] is not None and s["roi"] > 0.05)}
    doc = {"asof": datetime.now(timezone.utc).isoformat(), "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
           "snapshots": {"kalshi_priced": len(krows), "kalshi_candidates": kj, "pm_priced": len(prows), "pm_candidates": pj},
           "results": res, "chosen_threshold": chosen, "gate": gate,
           "gate_rule": "ROI > +5% after fees on 200+ out-of-sample bets, threshold chosen on train only"}
    OUT.write_text(json.dumps(doc, indent=1))
    print(f"- cutoff {doc['cutoff'][:10]} (test = last {TEST_DAYS}d)  priced: kalshi {len(krows)}/{kj}, polymarket {len(prows)}/{pj}")
    for k, v in res.items():
        print(f"- {k} train {fmt(v['train'])} | test {fmt(v['test'])}")
    for v, g in gate.items():
        print(f"- gate {v}: thr {g['thr']} test n={g['n']} roi={g['roi']} -> {'PASS' if g['pass'] else 'FAIL'}")
    print(f"- wrote {OUT}")


# ---------- live scan ----------
def scan():
    now = time.time()
    A = {n: Asset(n, now - 12 * 86400, now) for n in ASSETS}
    out = []
    for s, asset in KALSHI.items():
        d = get(f"{K}/markets?series_ticker={s}&status=open&limit=1000")
        for m in (d or {}).get("markets", []):
            up = m.get("strike_type") == "greater"
            B = m.get("floor_strike") if up else m.get("cap_strike")
            ask, bid = cdl(m, "yes_ask"), cdl(m, "yes_bid")
            end = ts(m["close_time"])
            r = make_row("kalshi", asset, m["event_ticker"], m["ticker"], up, B, now, end, A[asset], ask, bid, 0, 0) if B else None
            if r and end > now:
                out.append(r)
    for asset, name in PM_NAMES.items():
        mo = datetime.now(timezone.utc)
        e = get(f"{G}/events?slug=what-price-will-{name}-hit-in-{MONTHS[mo.month - 1]}-{mo.year}")
        for m in (e[0]["markets"] if e else []):
            g = m.get("groupItemTitle") or ""
            num = "".join(ch for ch in g if ch.isdigit() or ch == ".")
            if m.get("closed") or not num or g[:1] not in "↑↓":
                continue
            up, B = g[0] == "↑", float(num)
            ask, bid = m.get("bestAsk"), m.get("bestBid")
            r = make_row("polymarket", asset, e[0]["slug"], m["slug"], up, B, now, ts(e[0]["endDate"]), A[asset], ask, bid, 0, 0)
            if r:
                out.append(r)
    print("- venue asset ticker side barrier spot sigma model ask bid edge  (polymarket = data only)")
    for r in sorted(out, key=lambda r: -max((r["p"] - (r["ask"] or 9)), ((1 - r["p"]) - (1 - (r["bid"] or 0)) if r["bid"] is not None else -9))):
        b = place_bet(r["p"], r["ask"], r["bid"], 0.03, r["venue"] == "kalshi")
        if b:
            tag = "TRADABLE" if r["venue"] == "kalshi" else "DATA-ONLY"
            print(f"- {tag} {r['asset']} {r['ticker']} buy {b[0]} @{b[1]:.2f} barrier {r['B']:g} spot {r['S']:.4g} sigma {r['sigma']:.0%} model {r['p']:.2f} edge {b[2]:+.2f}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"backtest": backtest, "scan": scan}.get(cmd, lambda: print(__doc__))()
