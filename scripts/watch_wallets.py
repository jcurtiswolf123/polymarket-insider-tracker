"""Poll a wallet and market watchlist; send new activity to Telegram.

Config: ~/.polywatch/watchlist.json
  {"wallets": {"0x..": "label"}, "markets": {"0xcondition": "label"},
   "min_usdc": 200, "price_move": 0.03}
State:  ~/.polywatch/watch_state.json
Run:    uv run --env-file .env python scripts/watch_wallets.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home() / ".polywatch"
CONFIG = HOME / "watchlist.json"
STATE = HOME / "watch_state.json"
TRADES = "https://data-api.polymarket.com/v2/trades"
GAMMA = "https://gamma-api.polymarket.com/markets"


def get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "polywatch-watch"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def telegram(text: str, dry: bool) -> None:
    if dry:
        print("[dry-run]", text)
        return
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram not configured; message:", text, file=sys.stderr)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)


def check_wallets(cfg: dict, state: dict, dry: bool) -> None:
    seen = state.setdefault("wallets", {})
    for wallet, label in cfg.get("wallets", {}).items():
        try:
            rows = get(f"{TRADES}?user={wallet}&limit=50").get("data", [])
        except Exception as e:  # network blips must not kill the loop
            print(f"wallet {wallet}: {e}", file=sys.stderr)
            continue
        last = seen.get(wallet)
        fresh = [r for r in rows if last is not None and r["timestamp"] > last]
        if rows:
            seen[wallet] = max(r["timestamp"] for r in rows)
        for r in sorted(fresh, key=lambda r: r["timestamp"]):
            usdc = r["size"] * r["price"]
            if usdc < cfg.get("min_usdc", 200):
                continue
            telegram(
                f"WATCH {label} ({wallet[:8]})\n{r['side']} {r['outcome']} @ {r['price']:.3f} ${usdc:,.0f}\n{r['title']}",
                dry,
            )


def check_markets(cfg: dict, state: dict, dry: bool) -> None:
    prices = state.setdefault("markets", {})
    move = cfg.get("price_move", 0.03)
    for cid, label in cfg.get("markets", {}).items():
        try:
            m = get(f"{GAMMA}?condition_ids={cid}")[0]
        except Exception as e:
            print(f"market {cid}: {e}", file=sys.stderr)
            continue
        yes = float(json.loads(m["outcomePrices"])[0])
        prev = prices.get(cid)
        closed = bool(m.get("closed"))
        if prev is None:
            prices[cid] = {"yes": yes, "closed": closed}
            continue
        if closed and not prev.get("closed"):
            telegram(f"RESOLVED {label}: Yes={yes:.3f}\n{m['question']}", dry)
        elif abs(yes - prev["yes"]) >= move:
            telegram(f"MOVE {label}: Yes {prev['yes']:.3f} -> {yes:.3f}\n{m['question']}", dry)
        else:
            continue
        prices[cid] = {"yes": yes, "closed": closed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    check_wallets(cfg, state, args.dry_run)
    check_markets(cfg, state, args.dry_run)
    STATE.write_text(json.dumps(state, indent=1))
    print(time.strftime("%F %T"), "ok", len(cfg.get("wallets", {})), "wallets", len(cfg.get("markets", {})), "markets")


if __name__ == "__main__":
    main()
