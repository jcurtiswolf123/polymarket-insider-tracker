"""Alert-only Hormuz lane. Polymarket "Strait of Hormuz traffic returns to normal by December 31?" resolves Yes
if IMF PortWatch shows a 7-day moving average of daily transit calls >= 60 for any date through 2026-12-31.
This file only reads data and logs a signal; it never trades.

uv run --env-file .env python scripts/edge/hormuz.py status | tick | sar
shortcut: first-passage model is a zero-or-trend GBM on the MA, not a traffic model; replace once AIS counts exist.
shortcut: AIS counting needs AISSTREAM_API_KEY (not set here), so `status` reports it as blocked.
"""
import json, math, os, sys, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PW = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query?"
PORTID = "chokepoint6"  # Strait of Hormuz
GAMMA = "https://gamma-api.polymarket.com/public-search?"
SLUG = "strait-of-hormuz-traffic-returns-to-normal-by-december-31"
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
HORMUZ_BBOX = [56.0, 26.0, 56.9, 26.8]
THRESHOLD = 60.0
DEADLINE = date(2026, 12, 31)
WINDOW = 7
TREND_DAYS = 30
VOL_DAYS = 90
LOG = Path.home() / ".polywatch" / "edge" / "hormuz_log.jsonl"


def get(url: str, body: dict | None = None):
    req = urllib.request.Request(url, json.dumps(body).encode() if body else None,
                                 {"User-Agent": "pw", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def fetch_series() -> list[tuple[date, int]]:
    rows, off = [], 0
    while True:
        r = get(PW + urllib.parse.urlencode(dict(where=f"portid='{PORTID}'", outFields="date,n_total",
                                                  orderByFields="date ASC", resultOffset=off,
                                                  resultRecordCount=1000, f="json")))
        if "error" in r:
            raise RuntimeError(f"PortWatch: {r['error']}")
        rows += [(date.fromisoformat(f["attributes"]["date"]), f["attributes"]["n_total"]) for f in r["features"]]
        off += len(r["features"])
        if not r.get("exceededTransferLimit") or not r["features"]:
            return rows


def moving_avg(series: list[tuple[date, int]], n: int = WINDOW) -> list[tuple[date, float]]:
    """Trailing n-day mean over calendar days; a missing day counts as 0 calls. Needs n days of history."""
    by = dict(series)
    if not by:
        return []
    d0, d1 = min(by), max(by)
    days = [d0 + timedelta(k) for k in range((d1 - d0).days + 1)]
    vals = [by.get(d, 0) for d in days]
    return [(days[i], sum(vals[i - n + 1:i + 1]) / n) for i in range(n - 1, len(days))]


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def hit_prob(ma: list[float], days_left: int, level: float = THRESHOLD) -> dict:
    """P(MA touches `level` within days_left days) for log-MA as Brownian motion with drift.
    drift = mean daily log change over the last TREND_DAYS (floored at 0 so a fall never adds probability by symmetry,
    and a rise is not extrapolated past the observed window); sigma = std of 7-day log changes / sqrt(7), last VOL_DAYS.
    Uses the reflection formula P = N((-b+mT)/(s sqrtT)) + exp(2mb/s^2) N((-b-mT)/(s sqrtT)), b = ln(level/MA)."""
    lg = [math.log(max(x, 0.5)) for x in ma]  # floor at 0.5 so a zero MA stays finite
    if len(lg) < WINDOW + 2 or days_left <= 0:
        return {"p": 0.0, "mu": 0.0, "sigma": 0.0}
    if lg[-1] >= math.log(level):
        return {"p": 1.0, "mu": 0.0, "sigma": 0.0}
    t = lg[-TREND_DAYS:]
    mu = max(0.0, (t[-1] - t[0]) / (len(t) - 1))
    d7 = [lg[i] - lg[i - WINDOW] for i in range(max(WINDOW, len(lg) - VOL_DAYS), len(lg))]
    mean = sum(d7) / len(d7)
    sigma = math.sqrt(sum((x - mean) ** 2 for x in d7) / max(1, len(d7) - 1)) / math.sqrt(WINDOW)
    b = math.log(level) - lg[-1]
    if sigma <= 0:
        return {"p": 1.0 if mu * days_left >= b else 0.0, "mu": mu, "sigma": 0.0}
    s = sigma * math.sqrt(days_left)
    a = _phi((-b + mu * days_left) / s)
    c = math.exp(min(700, 2 * mu * b / sigma ** 2)) * _phi((-b - mu * days_left) / s)
    return {"p": min(1.0, a + c), "mu": mu, "sigma": sigma}


def market() -> dict | None:
    try:
        r = get(GAMMA + urllib.parse.urlencode({"q": "Strait of Hormuz"}))
        ev = next(e for e in r["events"] if e["slug"] == SLUG)
        m = ev["markets"][0]
        return {"yes": float(json.loads(m["outcomePrices"])[0]), "vol24h": m.get("volume24hr"), "slug": SLUG}
    except Exception as e:  # network or schema drift: status still prints the PortWatch half
        return {"error": str(e)}


def snapshot() -> dict:
    s = fetch_series()
    ma = moving_avg(s)
    last_d, last_ma = ma[-1]
    today = datetime.now(timezone.utc).date()
    left = (DEADLINE - last_d).days
    mdl = hit_prob([v for _, v in ma], left)
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "last_date": last_d.isoformat(),
            "lag_days": (today - last_d).days, "daily": s[-1][1], "ma7": round(last_ma, 3),
            "max_ma7_ever": round(max(v for _, v in ma), 2), "to_60": round(THRESHOLD - last_ma, 3),
            "days_left": left, "model_p": round(mdl["p"], 5), "mu": round(mdl["mu"], 5),
            "sigma": round(mdl["sigma"], 5), "market": market(),
            "ais": "blocked: AISSTREAM_API_KEY unset" if not os.environ.get("AISSTREAM_API_KEY") else "key set, counter not built"}


def status(sn: dict) -> str:
    m = sn["market"] or {}
    mk = f"{m['yes']:.3f}" if "yes" in m else f"n/a ({m.get('error')})"
    return "\n".join([
        f"- ma7: {sn['ma7']} ({sn['last_date']}, daily {sn['daily']})",
        f"- lag: {sn['lag_days']}d",
        f"- to 60: {sn['to_60']} (best ma7 since 2019: {sn['max_ma7_ever']})",
        f"- market yes: {mk}",
        f"- model p: {sn['model_p']:.4f} ({sn['days_left']}d, mu {sn['mu']:.4f}, sigma {sn['sigma']:.4f})",
        f"- ais: {sn['ais']}",
    ])


def sar() -> str:
    """Feasibility probe only: Sentinel-1 GRD scenes over the strait box in the last 40 days."""
    t1 = datetime.now(timezone.utc)
    r = get(STAC, {"collections": ["sentinel-1-grd"], "bbox": HORMUZ_BBOX, "limit": 100,
                   "datetime": f"{(t1 - timedelta(days=40)).strftime('%Y-%m-%dT%H:%M:%SZ')}/{t1.strftime('%Y-%m-%dT%H:%M:%SZ')}"})
    fs = sorted(r["features"], key=lambda f: f["properties"]["datetime"])
    lines = [f"- {f['properties']['datetime'][:16]} {f['properties'].get('platform')} "
             f"{f['properties'].get('sar:instrument_mode')} {f['properties'].get('sat:orbit_state')}" for f in fs]
    return "\n".join(lines + [f"- scenes in 40d: {len(fs)}"])


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "sar":
        print(sar())
        return 0
    sn = snapshot()
    if cmd == "status":
        print(status(sn))
    elif cmd == "tick":
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(sn) + "\n")
        print(status(sn))
    else:
        print("usage: hormuz.py status | tick | sar")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
