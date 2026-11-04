"""Copy insider-rule hits into a paper (or gated live) book, with Telegram alerts.

Every new line in ~/.polywatch/rule_hits.jsonl is a signal. Each signal:
  1. always pings Telegram (INSIDER ...) once per wallet+market+outcome per 6h,
  2. adds the wallet to watchlist.json so its later trades alert too,
  3. passes filters -> sized -> filled against the live CLOB book (paper by default).

Live orders need ALL of: COPY_LIVE=1, POLY_PRIVATE_KEY, no ~/.polywatch/COPY_HALT,
and https://polymarket.com/api/geoblock answering blocked=false. This module never
routes around the geoblock.

Config: ~/.polywatch/copy_config.json (defaults below, written on first run)
Run:    uv run --env-file .env python scripts/copy_trader.py tick [--dry-run]
        uv run --env-file .env python scripts/copy_trader.py report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HOME = Path(os.environ.get("POLYWATCH_HOME", Path.home() / ".polywatch"))
HITS = HOME / "rule_hits.jsonl"
CONFIG = HOME / "copy_config.json"
STATE = HOME / "copy_state.json"
LEDGER = HOME / "copy_ledger.jsonl"
WATCHLIST = HOME / "watchlist.json"
HALT = HOME / "COPY_HALT"
GAMMA = "https://gamma-api.polymarket.com/markets"
BOOK = "https://clob.polymarket.com/book"
GEO = "https://polymarket.com/api/geoblock"

DEFAULTS = {
    "per_trade_usd": 25,
    "per_market_cap_usd": 75,
    "daily_cap_usd": 250,
    "open_exposure_cap_usd": 1000,
    "min_leader_usdc": 2000,
    "min_price": 0.03,
    "max_price": 0.85,
    "max_slippage": 0.03,
    "min_hours_to_end": 1,
    "strong_rules": ["R1_fresh_big_bet", "C1_fresh_cluster", "R7_proven_longshot_hitter"],
    "min_rules_if_no_strong": 2,
    "exclude_slug_regex": r"(-\d{4}-\d{2}-\d{2}(-|$)|exact-score|^(lol|cs2|val|dota|atp|wta)-)",
    "alert_dedupe_hours": 6,
}


def get(url: str, tries: int = 4) -> object:
    """GET JSON; retries transient DNS/5xx errors with backoff, raises 4xx at once."""
    req = urllib.request.Request(url, headers={"User-Agent": "polywatch-copy"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("unreachable")


def telegram(text: str, dry: bool) -> None:
    if dry:
        print("[tg]", text.replace("\n", " | "))
        return
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram not configured:", text, file=sys.stderr)
        return
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20
        )
    except Exception as e:  # one failed ping must not drop the rest of the tick
        print("telegram failed:", e, file=sys.stderr)


def load_json(p: Path, default: dict) -> dict:
    return json.loads(p.read_text()) if p.exists() else dict(default)


def ledger_rows() -> list[dict]:
    return [json.loads(x) for x in LEDGER.read_text().splitlines()] if LEDGER.exists() else []


# ---------- pure decision logic (tested) ----------


def filter_reason(hit: dict, market: dict | None, cfg: dict, now: float) -> str | None:
    """None if the signal is copyable, else the reason it is skipped."""
    rules = hit.get("rules", [])
    strong = any(r.removeprefix("W_") in cfg["strong_rules"] for r in rules)
    if not strong and len(rules) < cfg["min_rules_if_no_strong"]:
        return f"weak rules {','.join(rules)}"
    if float(hit["usdc"]) < cfg["min_leader_usdc"]:
        return f"leader ${float(hit['usdc']):,.0f} < ${cfg['min_leader_usdc']:,}"
    px = float(hit["price"])
    if not cfg["min_price"] <= px <= cfg["max_price"]:
        return f"price {px:.3f} outside {cfg['min_price']}-{cfg['max_price']}"
    if re.search(cfg["exclude_slug_regex"], hit.get("slug") or ""):
        return "excluded slug (dated match / esports)"
    if market is None:
        return "market not found"
    if market.get("closed") or not market.get("acceptingOrders", True):
        return "market closed"
    end = market.get("endDate")
    if end:
        hrs = (datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - now) / 3600
        if hrs < cfg["min_hours_to_end"]:
            return f"ends in {hrs:.1f}h"
    return None


def stake_for(cond: str, cfg: dict, rows: list[dict], now: float) -> float:
    """Dollars to put on this signal after every cap."""
    day = datetime.fromtimestamp(now, UTC).date().isoformat()
    open_rows = [r for r in rows if not r.get("settled")]
    spent_today = sum(r["cost"] for r in rows if r["ts"][:10] == day)
    in_market = sum(r["cost"] for r in open_rows if r["cond"] == cond)
    exposure = sum(r["cost"] for r in open_rows)
    return max(
        0.0,
        min(
            cfg["per_trade_usd"],
            cfg["per_market_cap_usd"] - in_market,
            cfg["daily_cap_usd"] - spent_today,
            cfg["open_exposure_cap_usd"] - exposure,
        ),
    )


def walk_book(asks: list[dict], usd: float, limit_px: float) -> tuple[float, float]:
    """Fill `usd` against asks up to limit_px. Returns (shares, cost)."""
    shares = cost = 0.0
    for lvl in sorted(asks, key=lambda a: float(a["price"])):
        p, sz = float(lvl["price"]), float(lvl["size"])
        if p > limit_px or cost >= usd:
            break
        take = min(sz, (usd - cost) / p)
        shares += take
        cost += take * p
    return round(shares, 4), round(cost, 2)


# ---------- I/O ----------


def market_for(cond: str) -> dict | None:
    rows = get(f"{GAMMA}?condition_ids={cond}&closed=true") or get(f"{GAMMA}?condition_ids={cond}")
    return rows[0] if rows else None


def token_for(market: dict, outcome: str) -> str | None:
    outs, toks = json.loads(market["outcomes"]), json.loads(market["clobTokenIds"])
    return dict(zip(outs, toks)).get(outcome)


def live_allowed() -> str | None:
    if os.environ.get("COPY_LIVE") != "1":
        return "COPY_LIVE not set"
    if not os.environ.get("POLY_PRIVATE_KEY"):
        return "POLY_PRIVATE_KEY missing"
    if get(GEO).get("blocked", True):
        return "geoblocked region"
    return None


def place_live(token: str, usd: float, limit_px: float) -> dict:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    c = ClobClient(
        "https://clob.polymarket.com",
        key=os.environ["POLY_PRIVATE_KEY"],
        chain_id=137,
        signature_type=int(os.environ.get("POLY_SIG_TYPE", "0")),
        funder=os.environ.get("POLY_FUNDER") or None,
    )
    c.set_api_creds(c.create_or_derive_api_creds())
    order = c.create_market_order(MarketOrderArgs(token_id=token, amount=usd, price=limit_px))
    return c.post_order(order, OrderType.FOK)


def add_to_watchlist(wallet: str, label: str) -> None:
    wl = load_json(WATCHLIST, {"wallets": {}, "markets": {}})
    if wallet.lower() not in {w.lower() for w in wl.setdefault("wallets", {})}:
        wl["wallets"][wallet] = label[:40]
        WATCHLIST.write_text(json.dumps(wl, indent=2))


def tick(dry: bool) -> None:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULTS, indent=2))
    cfg = {**DEFAULTS, **json.loads(CONFIG.read_text())}
    st = load_json(STATE, {"offset": None, "alerted": {}})
    lines = HITS.read_text().splitlines() if HITS.exists() else []
    if st["offset"] is None:  # first run: start at the tail, never replay history into trades
        st["offset"] = len(lines)
    new, st["offset"] = lines[st["offset"] :], len(lines)
    now = time.time()
    live_block = live_allowed()
    rows = ledger_rows()
    for line in new:
        hit = json.loads(line)
        key = f"{hit['wallet']}|{hit['market']}|{hit['outcome']}"
        first_alert = now - st["alerted"].get(key, 0) > cfg["alert_dedupe_hours"] * 3600
        try:
            m = market_for(hit["market"])
        except Exception as e:
            m, why = None, f"gamma error {e}"
        else:
            why = filter_reason(hit, m, cfg, now)
        stake, fill = 0.0, None
        if why is None:
            stake = stake_for(hit["market"], cfg, rows, now)
            if stake <= 0:
                why = "cap reached"
        if why is None:
            tok = token_for(m, hit["outcome"])
            limit_px = float(hit["price"]) + cfg["max_slippage"]
            try:
                book = get(f"{BOOK}?token_id={tok}")
            except Exception:  # resolved markets have no book (404)
                book = {}
            shares, cost = walk_book(book.get("asks", []), stake, limit_px)
            if cost < 1:
                why = f"no ask <= {limit_px:.3f}"
            elif HALT.exists():
                why = "COPY_HALT present"
            else:
                mode = "paper"
                if live_block is None and not dry:
                    resp = place_live(tok, stake, limit_px)
                    mode = "live" if resp.get("success") else f"live-failed {resp}"
                fill = {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "mode": mode,
                    "cond": hit["market"],
                    "token": tok,
                    "outcome": hit["outcome"],
                    "title": hit.get("title") or m.get("question"),
                    "leader": hit["wallet"],
                    "leader_px": float(hit["price"]),
                    "rules": hit["rules"],
                    "shares": shares,
                    "cost": cost,
                    "avg": round(cost / shares, 4),
                }
                if not dry:
                    with LEDGER.open("a") as fh:
                        fh.write(json.dumps(fill) + "\n")
                rows.append(fill)
        if first_alert or fill:
            act = (
                f"COPIED {fill['mode']} ${fill['cost']:.0f} @ {fill['avg']:.3f}"
                if fill
                else f"no copy: {why}"
            )
            telegram(
                f"INSIDER {','.join(hit['rules'])}\n{hit['outcome']} @ {float(hit['price']):.3f} "
                f"${float(hit['usdc']):,.0f} wallet age {hit.get('age_h')}h\n"
                f"{hit.get('title') or hit.get('slug')}\nhttps://polymarket.com/profile/{hit['wallet']}\n{act}",
                dry,
            )
            st["alerted"][key] = now
            # watch only wallets worth following, or the 5-min poller drowns in weak hits
            if not dry and (fill or not (why or "").startswith("weak")):
                add_to_watchlist(
                    hit["wallet"], f"auto {','.join(hit['rules'])[:20]} {hit.get('slug', '')}"
                )
    st["alerted"] = {k: v for k, v in st["alerted"].items() if now - v < 7 * 86400}
    if not dry:
        STATE.write_text(json.dumps(st, indent=1))
    print(
        time.strftime("%F %T"),
        f"ok new={len(new)} live={'on' if live_block is None else 'off: ' + live_block}",
    )


def report() -> None:
    rows = ledger_rows()
    if not rows:
        print("ledger empty")
        return
    cost = value = 0.0
    for r in rows:
        m = market_for(r["cond"])
        prices = dict(zip(json.loads(m["outcomes"]), map(float, json.loads(m["outcomePrices"]))))
        px = prices.get(r["outcome"], 0.0)
        v = r["shares"] * px
        cost, value = cost + r["cost"], value + v
        tag = "RESOLVED" if m.get("closed") else "open"
        print(
            f"{r['ts'][:16]} {r['mode']:5} {tag:8} {r['outcome'][:8]:8} {r['avg']:.3f}->{px:.3f} "
            f"${r['cost']:.0f} -> ${v:.0f} | {r['title'][:60]}"
        )
    print(
        f"\n{len(rows)} copies  cost ${cost:,.0f}  value ${value:,.0f}  pnl ${value - cost:,.0f} "
        f"({(value - cost) / cost:.1%})"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "report"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tick(a.dry_run) if a.cmd == "tick" else report()
