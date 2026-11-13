"""Favorites lane: paper book for the one slice of rule hits that has beaten the price.

Lane (from resolved trades 2026-11-03..07: +13.5 pts over price across 51 markets, 90% CI +6..+21):
  a fresh wallet (< 7d old) flagged by an insider rule, buying at 0.60-0.85, in a market that
  resolves within 7 days. The model-only rule M1 does not count.

tick:   every new hit in the lane is filled on paper against the live order book, $200 per market,
        limit = insider price + LANE_SLIP. Misses (no ask under the limit) are logged too.
report: resolved win rate vs fill price, bootstrap CI by market, flat-stake P&L.
        --sms texts the summary (weekly launchd job).

  uv run --env-file .env python scripts/fav_lane.py tick|report [--sms] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import copy_trader as ct

HOME = Path(os.environ.get("POLYWATCH_HOME", Path.home() / ".polywatch"))
HITS = HOME / "rule_hits.jsonl"
STATE = HOME / "fav_state.json"
LEDGER = HOME / "fav_ledger.jsonl"

LANE_MIN, LANE_MAX = 0.60, 0.85
MAX_HOURS_TO_END = 168.0
MAX_WALLET_AGE_H = 168.0
MIN_USDC = 1000.0
STAKE = float(os.environ.get("LANE_STAKE", "200"))
SLIP = float(os.environ.get("LANE_SLIP", "0.04"))  # about half the measured edge
SPORT = re.compile(
    r"^(nfl|nba|mlb|nhl|cfb|cbb|epl|ucl|lal|sea|bun|fl1|mls|ufc|atp|wta|cs2|lol|val|dota)-|-vs-| vs\.? "
)


def _rules(hit: dict) -> set[str]:
    return {r.removeprefix("W_") for r in hit.get("rules") or []}


def prefilter(hit: dict) -> bool:
    """Lane checks that need no network."""
    try:
        px, usdc = float(hit.get("price") or 0), float(hit.get("usdc") or 0)
        age = float(hit.get("age_h") if hit.get("age_h") is not None else 1e9)
    except (TypeError, ValueError):
        return False
    return (
        bool(_rules(hit) - {"M1_model_edge"})
        and LANE_MIN <= px < LANE_MAX
        and usdc >= MIN_USDC
        and age < MAX_WALLET_AGE_H
    )


def hours_to_end(market: dict | None, now: float) -> float | None:
    try:
        end = (market or {}).get("endDate") or ""
        return (datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - now) / 3600
    except ValueError:
        return None


GAME_MIN, GAME_MAX, GAME_START_H = 0.30, 0.85, 24.0


def game_hit(hit: dict) -> bool:
    """Second SMS gate (Joshua 11-12): the pre-11-07 gate on games. Any wallet age, model-only (M1) hits
    count, game starts within 24h. It sent Bilibili, BetBoom, NaVi (3-0 on Kalshi); the 0.30 floor drops the
    0.27 DK loss. The card's bet > 0 check still applies."""
    try:
        h, px, usdc = hit.get("hours_to_start"), float(hit.get("price") or 0), float(hit.get("usdc") or 0)
    except (TypeError, ValueError):
        return False
    return (h is not None and 0 < float(h) <= GAME_START_H and GAME_MIN <= px < GAME_MAX
            and usdc >= MIN_USDC and bool(_rules(hit)))


def in_lane(hit: dict, market: dict | None, now: float | None = None) -> bool:
    h = hours_to_end(market, now or time.time())
    return prefilter(hit) and h is not None and 0 < h <= MAX_HOURS_TO_END


def category(row: dict) -> str:
    text = f"{row.get('slug') or ''} {(row.get('title') or '').lower()}"
    return "sports" if SPORT.search(text) else "other"


def ledger_rows() -> list[dict]:
    return [json.loads(x) for x in LEDGER.read_text().splitlines()] if LEDGER.exists() else []


def tick(dry: bool) -> None:
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    lines = HITS.read_text().splitlines() if HITS.exists() else []
    if "offset" not in st:  # first run: start at the tail
        st = {"offset": len(lines)}
    if st["offset"] > len(lines):  # log rotated
        st["offset"] = 0
    new, st["offset"] = lines[st["offset"] :], len(lines)
    done = {(r["cond"], r["outcome"]) for r in ledger_rows()}
    now = time.time()
    for line in new:
        try:
            hit = json.loads(line)
        except ValueError:
            continue
        key = (hit.get("market"), hit.get("outcome"))
        if key in done or not prefilter(hit):
            continue
        try:
            m = ct.market_for(hit["market"])
        except Exception as e:
            print(f"gamma error {e}")
            continue
        if not in_lane(hit, m, now) or m.get("closed"):
            continue
        tok = ct.token_for(m, hit["outcome"])
        limit_px = float(hit["price"]) + SLIP
        try:
            book = ct.get(f"{ct.BOOK}?token_id={tok}")
        except Exception:
            book = {}
        shares, cost = ct.walk_book(book.get("asks", []), STAKE, limit_px)
        row = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "cond": hit["market"],
            "token": tok,
            "outcome": hit["outcome"],
            "title": hit.get("title") or m.get("question"),
            "slug": hit.get("slug"),
            "wallet": hit.get("wallet"),
            "rules": sorted(_rules(hit)),
            "insider_px": float(hit["price"]),
            "usdc": float(hit.get("usdc") or 0),
            "hours_to_end": round(hours_to_end(m, now) or 0, 1),
            "filled": cost >= 1,
            "shares": shares,
            "cost": cost,
            "avg": round(cost / shares, 4) if shares else None,
            "limit": round(limit_px, 4),
        }
        done.add(key)
        print(("FILL" if row["filled"] else "MISS"), row["outcome"], row["avg"], row["title"][:60])
        if not dry:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
    if not dry:
        STATE.write_text(json.dumps(st))


def summarize(rows: list[dict], resolve=None) -> dict:
    """Resolved win rate vs fill price, one vote per market. resolve(cond) -> market dict."""
    resolve = resolve or ct.market_for
    filled = [r for r in rows if r.get("filled")]
    out = {"signals": len(rows), "missed": len(rows) - len(filled), "open": 0, "bets": []}
    for r in filled:
        m = resolve(r["cond"]) or {}
        if not m.get("closed"):
            out["open"] += 1
            continue
        prices = dict(
            zip(json.loads(m["outcomes"]), map(float, json.loads(m["outcomePrices"])), strict=False)
        )
        won = prices.get(r["outcome"], 0.0)
        if won not in (0.0, 1.0):
            continue  # voided / split
        out["bets"].append({**r, "won": int(won), "pnl": r["shares"] * won - r["cost"]})
    bets = out["bets"]
    if bets:
        ex = [b["won"] - b["avg"] for b in bets]
        n = len(ex)
        bs = sorted(sum(random.choice(ex) for _ in ex) / n for _ in range(3000))
        cost = sum(b["cost"] for b in bets)
        out.update(
            {
                "resolved": n,
                "win_rate": sum(b["won"] for b in bets) / n,
                "avg_fill": sum(b["avg"] for b in bets) / n,
                "excess": sum(ex) / n,
                "ci90": (bs[150], bs[2850]),
                "pnl": sum(b["pnl"] for b in bets),
                "roi": sum(b["pnl"] for b in bets) / cost if cost else 0.0,
            }
        )
    return out


def report_text(s: dict) -> str:
    lines = [
        "Polywatch favorites lane",
        f"- signals {s['signals']}, missed fill {s['missed']}, open {s['open']}",
    ]
    if not s.get("resolved"):
        return "\n".join([*lines, "- resolved 0"])
    lo, hi = s["ci90"]
    lines += [
        f"- resolved {s['resolved']}: won {s['win_rate']:.0%} at avg fill {s['avg_fill']:.2f}",
        # bootstrap of a few all-win bets gives a fake-narrow CI
        f"- edge {s['excess']:+.1%} "
        + (f"(90% CI {lo:+.0%} to {hi:+.0%})" if s["resolved"] >= 20 else "(CI needs 20+ bets)"),
        f"- pnl ${s['pnl']:+,.0f} ({s['roi']:+.0%}) at ${STAKE:.0f}/bet",
    ]
    if s["resolved"] < 100:
        lines.append(f"- verdict: too early, {100 - s['resolved']} more bets to call it")
    elif lo > 0:
        lines.append("- verdict: edge holding")
    else:
        lines.append("- verdict: no edge, CI includes zero")
    return "\n".join(lines)


def report(sms: bool, dry: bool) -> None:
    rows = ledger_rows()
    s = summarize(rows)
    for b in s["bets"]:
        print(
            f"{b['ts'][:16]} {'WON ' if b['won'] else 'LOST'} {b['outcome'][:14]:14} "
            f"fill {b['avg']:.2f} pnl ${b['pnl']:+.0f} {category(b):6} | {b['title'][:55]}"
        )
    for cat in ("sports", "other"):
        sub = summarize([r for r in rows if category(r) == cat])
        if sub.get("resolved"):
            print(f"{cat}: {sub['resolved']} resolved, edge {sub['excess']:+.1%}")
    text = report_text(s)
    try:  # live replay of every lane hit (not just paper fills), next to the history baseline
        sys.path.insert(0, str(Path(__file__).parent / "edge"))
        import sms_gate_replay
        text += "\nAll lane hits, by week\n" + "\n".join(sms_gate_replay.lane_lines())
    except Exception as e:  # the weekly text must still go out
        text += f"\n- live replay error {type(e).__name__}"
    try:  # forward test of wallets with a proven in-play edge (scripts/edge/wallet_edge.py)
        import skilled_live
        text += "\n" + "\n".join(skilled_live.lines())
    except Exception as e:
        text += f"\n- skilled live error {type(e).__name__}"
    print("\n" + text)
    if sms:
        import sms_alerts

        print(sms_alerts.send_sms(text, dry))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "report"])
    ap.add_argument("--sms", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tick(a.dry_run) if a.cmd == "tick" else report(a.sms, a.dry_run)
