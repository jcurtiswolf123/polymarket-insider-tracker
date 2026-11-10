"""Research desk (paper only): claude -p forecasts non-sports markets blind to price, graded vs the market.

pick:   up to N fresh Kalshi + Polymarket markets closing in 7-60 days, liquid, priced 0.05-0.95
run:    pick + forecast each with `claude -p` (WebSearch/WebFetch only), log prob and the market quote
grade:  settle resolved rows; Brier model vs market, paper $200 bets where |edge| > EDGE after Kalshi fee
report: bullets
Ledger ~/.polywatch/edge/research_ledger.jsonl. Polymarket rows are data only (CA geoblock).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

K = "https://api.elections.kalshi.com/trade-api/v2"
G = "https://gamma-api.polymarket.com"
LEDGER = Path.home() / ".polywatch" / "edge" / "research_ledger.jsonl"
SKIP_CATS = {"Sports", "Exotics", "Mentions"}
SKIP_TAGS = {"sports", "esports", "crypto", "mentions"}
MIN_DAYS, MAX_DAYS = 7, 60
STAKE, EDGE = 200.0, 0.10
MODEL = "sonnet"  # usage budget: Sonnet, not Opus

PROMPT = """You are a careful forecaster. Estimate the probability this prediction-market question resolves YES.
Do not look up or use any prediction-market or betting price for this question (no Kalshi, Polymarket, PredictIt,
Manifold, Metaculus community numbers, or bookmaker odds). Use base rates, official sources, news, polls, data.

Question: {q}
Resolution rules: {rules}
Closes: {close}
Today: {today}

Steps: 1) base rate for this kind of event, 2) current evidence with sources, 3) final probability.
Reply with ONLY a JSON object on the last line:
{{"p": <0..1>, "base_rate": <0..1 or null>, "sources": ["url", ...], "why": "<= 40 words"}}"""


def get(url: str):
    for i in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polywatch-edge"})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == 5:
                raise
            time.sleep(2**i)
    return None


def kalshi_fee(contracts: int, p: float) -> float:
    return math.ceil(round(0.07 * contracts * p * (1 - p) * 100, 6)) / 100


def ledger() -> list:
    return [json.loads(x) for x in LEDGER.read_text().splitlines()] if LEDGER.exists() else []


def days_to(iso: str) -> float:
    return (datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds() / 86400


def kalshi_candidates() -> list:
    out, cursor = [], ""
    for _ in range(40):
        q = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            q["cursor"] = cursor
        d = get(f"{K}/events?{urllib.parse.urlencode(q)}")
        for e in d.get("events", []):
            if e.get("category") in SKIP_CATS:
                continue
            ms = [m for m in e.get("markets") or [] if MIN_DAYS <= days_to(m["close_time"]) <= MAX_DAYS
                  and 0.05 <= float(m.get("yes_ask_dollars") or 0) <= 0.95 and float(m.get("volume_fp") or 0) >= 5000]
            if ms:  # one market per event: the most traded
                m = max(ms, key=lambda m: float(m["volume_fp"]))
                out.append({"venue": "kalshi", "id": m["ticker"], "event": e["event_ticker"], "cat": e.get("category"),
                            "q": f"{e['title']}: {m.get('yes_sub_title') or m['title']}", "rules": m.get("rules_primary", ""),
                            "close": m["close_time"], "vol": float(m["volume_fp"])})
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def poly_candidates() -> list:
    out = []
    for off in (0, 500):
        for e in get(f"{G}/events?active=true&closed=false&limit=500&offset={off}&order=volume24hr&ascending=false"):
            tags = {t.get("slug") for t in e.get("tags") or []}
            if tags & SKIP_TAGS or not e.get("endDate") or not MIN_DAYS <= days_to(e["endDate"]) <= MAX_DAYS:
                continue
            ms = [m for m in e.get("markets") or [] if m.get("outcomePrices") and m.get("active") and not m.get("closed")]
            ms = [m for m in ms if 0.05 <= float(json.loads(m["outcomePrices"])[0]) <= 0.95]
            if ms and float(e.get("volume24hr") or 0) >= 10000:
                m = max(ms, key=lambda m: float(m.get("volumeNum") or 0))
                out.append({"venue": "polymarket", "id": m["conditionId"], "event": e["slug"], "cat": next(iter(tags), ""),
                            "q": m["question"], "rules": (m.get("description") or "")[:1500], "close": e["endDate"],
                            "vol": float(e.get("volume24hr") or 0)})
    return out


def pick(n: int) -> list:
    seen = {r["id"] for r in ledger()} | {r["event"] for r in ledger()}
    c = [x for x in kalshi_candidates() + poly_candidates() if x["id"] not in seen and x["event"] not in seen
         and "anthropic" not in x["q"].lower()]  # Claude is built by Anthropic: conflict, skip
    c.sort(key=lambda x: -x["vol"])
    k = [x for x in c if x["venue"] == "kalshi"][: n // 2]  # tradable venue gets half
    return (k + [x for x in c if x not in k])[:n]


def quote(r: dict):
    if r["venue"] == "kalshi":
        m = get(f"{K}/markets/{r['id']}")["market"]
        return float(m["yes_ask_dollars"]), float(m["yes_bid_dollars"])
    m = get(f"{G}/markets?condition_ids={r['id']}")[0]
    p = float(json.loads(m["outcomePrices"])[0])
    return float(m.get("bestAsk") or p), float(m.get("bestBid") or p)


def parse_p(text: str):
    for blob in reversed(re.findall(r"\{[^{}]*\"p\"[^{}]*\}", text, re.S)):
        try:
            d = json.loads(blob)
            if 0 <= float(d["p"]) <= 1:
                return d
        except (ValueError, KeyError, TypeError):
            continue
    return None


def forecast(r: dict) -> dict | None:
    prompt = PROMPT.format(q=r["q"], rules=r["rules"][:1500], close=r["close"][:10], today=datetime.now(UTC).date())
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--allowedTools", "WebSearch", "WebFetch", "--max-budget-usd", "1.50"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd="/tmp")
        res = json.loads(out.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    d = parse_p(res.get("result", ""))
    return {**d, "cost": res.get("total_cost_usd")} if d else None


def cmd_run(n: int) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    for r in pick(n):
        f = forecast(r)
        if not f:
            print("fail", r["id"])
            continue
        ask, bid = quote(r)  # read after the forecast so the model never sees it
        row = {**r, "ts": datetime.now(UTC).isoformat(), "p": float(f["p"]), "base_rate": f.get("base_rate"),
               "sources": f.get("sources", [])[:8], "why": f.get("why", ""), "cost": f.get("cost"), "ask": ask, "bid": bid}
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"{r['venue']:10} p {row['p']:.2f} mkt {(ask + bid) / 2:.2f}  {r['q'][:70]}", flush=True)


def outcome(r: dict):
    if r["venue"] == "kalshi":
        try:
            m = get(f"{K}/markets/{r['id']}")["market"]
        except urllib.error.HTTPError:
            m = get(f"{K}/historical/markets/{r['id']}")["market"]
        return {"yes": 1, "no": 0}.get(m.get("result"))
    m = get(f"{G}/markets?condition_ids={r['id']}&closed=true")
    if not m:
        return None
    p = float(json.loads(m[0]["outcomePrices"])[0])
    return int(p) if p in (0.0, 1.0) else None


def paper(r: dict):
    """(pnl, staked) for a $200 bet on the side with edge > EDGE, else None."""
    if r["p"] - r["ask"] > EDGE:
        side, px = "yes", r["ask"]
    elif (1 - r["p"]) - (1 - r["bid"]) > EDGE:
        side, px = "no", 1 - r["bid"]
    else:
        return None
    c = int(STAKE // px)
    win = r["won"] == (side == "yes")
    return c * win - c * px - kalshi_fee(c, px), c * px


def cmd_grade() -> None:
    rows = ledger()
    for r in rows:
        if "won" not in r and days_to(r["close"]) < 0:
            o = outcome(r)
            if o is not None:
                r["won"] = o
                b = paper(r)
                r["pnl"], r["staked"] = b if b else (None, None)
    LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rows))


def report_text() -> str:
    rows = ledger()
    done = [r for r in rows if "won" in r]
    lines = ["Research desk", f"- forecasts {len(rows)}, resolved {len(done)}",
             f"- spend ${sum(r.get('cost') or 0 for r in rows):.2f}"]
    if done:
        bm = sum((r["p"] - r["won"]) ** 2 for r in done) / len(done)
        bk = sum(((r["ask"] + r["bid"]) / 2 - r["won"]) ** 2 for r in done) / len(done)
        bets = [r for r in done if r.get("pnl") is not None and r["venue"] == "kalshi"]
        lines += [f"- Brier model {bm:.4f} vs market {bk:.4f} ({'model better' if bm < bk else 'market better'})"]
        if bets:
            staked = sum(r["staked"] for r in bets)
            pnl = sum(r["pnl"] for r in bets)
            lines.append(f"- Kalshi paper bets {len(bets)}, pnl ${pnl:+,.0f} ({pnl / staked:+.1%})")
        gate = len(done) >= 100 and bm < bk
        lines.append(f"- gate (100+ resolved, beats market): {'PASS' if gate else 'not yet'}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("-n", type=int, default=20)
    p = sub.add_parser("pick")
    p.add_argument("-n", type=int, default=20)
    sub.add_parser("grade")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run":
        cmd_run(a.n)
    elif a.cmd == "pick":
        for x in pick(a.n):
            print(f"{x['venue']:10} {x['close'][:10]} vol {x['vol']:>10,.0f}  {x['q'][:80]}")
    elif a.cmd == "grade":
        cmd_grade()
        print(report_text())
    else:
        print(report_text())


if __name__ == "__main__":
    main()
