"""LIVE Kalshi orders for the favorites lane and the SMS alert book. Real money, approved by Joshua 2026-11-11.

Caps (Joshua, 2026-11-11): $200 per bet, favorites lane + alerts (lane off then back on 11-12 by Joshua), stop at $1,000 worst-case loss (cut from $300/$3,000 after the 14-day split).
The loss cap is worst case: realized P&L minus everything still open must stay above -$1,000,
so even if every open bet loses, total loss stays under $1,000.

tick:   each new fav_ledger row (lane signal) and each new kalshi_copy_ledger "open" row (alert already
        matched to Kalshi) bought with an immediate-or-cancel limit at insider price + SLIP (max 0.90).
        One bet per Kalshi game: a second signal on either side of the same game is skipped. Then settles.
report: live book.
Kill switch: touch ~/.polywatch/LIVE_OFF

  uv run --with cryptography --env-file .env python scripts/fav_live.py tick|report [--dry-run]
"""

from __future__ import annotations

import base64
import json
import math
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import copy_trader as ct
import fav_lane
import kalshi_copy as kc
import sms_alerts

STAKE = 200.0
MAX_LOSS = 1000.0
MAX_PRICE = 0.90
LANE_BETS = True  # 11-12 Joshua: bet the full favorites lane (31-5 live) plus alerts; False = alerts only
HOME = Path.home() / ".polywatch"
LEDGER = HOME / "fav_live_ledger.jsonl"
STATE = HOME / "fav_live_state.json"
OFF = HOME / "LIVE_OFF"
CREDS = Path.home() / ".gstack/credentials"
API = "/trade-api/v2"


def _sign(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key((CREDS / "kalshi-privkey.pem").read_bytes(), None)
    ts = str(int(time.time() * 1000))
    sig = key.sign((ts + method + API + path.split("?")[0]).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": (CREDS / "kalshi-key-id.txt").read_text().strip(),
            "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json"}


def call(method: str, path: str, body: dict | None = None) -> dict:
    for i in range(4):  # 429: back off and retry (same client_order_id/client_transfer_id, so no double order)
        req = urllib.request.Request(kc.K + path, method=method, headers=_sign(method, path),
                                     data=json.dumps(body).encode() if body else None)
        try:
            return json.load(urllib.request.urlopen(req, timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < 3:
                time.sleep(2 * (i + 1))
                continue
            raise RuntimeError(f"kalshi {e.code}: {e.read().decode()[:200]}") from e


def rows() -> list[dict]:
    return [json.loads(x) for x in LEDGER.read_text().splitlines()] if LEDGER.exists() else []


def headroom(rs: list[dict]) -> float:
    """Dollars that can still be put at risk before the worst case reaches -MAX_LOSS."""
    realized = sum(r["pnl"] for r in rs if r.get("status") in ("won", "lost"))
    at_risk = sum(r["cost"] + r["fee"] for r in rs if r.get("status") == "open")
    return MAX_LOSS + realized - at_risk


assert headroom([]) == 1000
assert headroom([{"status": "lost", "pnl": -51.0}, {"status": "open", "cost": 49.0, "fee": 1.0}]) == 899


def game(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


assert game("KXNBAGAME-26NOV11BOSTOR-BOS") == game("KXNBAGAME-26NOV11BOSTOR-TOR")


def signals(st: dict) -> list[dict]:
    """New signals from both books; first run of each book starts at its tail."""
    fav = fav_lane.ledger_rows()
    al = kc.rows()
    st.setdefault("seen", len(fav)); st.setdefault("alerts_seen", len(al))
    out = [] if not LANE_BETS else [{"src": "fav", "cond": s["cond"], "outcome": s["outcome"], "title": s.get("title"), "insider_px": s["insider_px"], "usdc": s.get("usdc")}
           for s in fav[st["seen"]:]]
    out += [{"src": "alert", "cond": a["key"].split("|")[-1], "outcome": a.get("outcome"), "title": a.get("title"),
             "insider_px": a.get("leader_px") or a["ask"], "kalshi": a["kalshi"], "ask": a["ask"]}
            for a in al[st["alerts_seen"]:] if a.get("status") == "open"]
    st["seen"], st["alerts_seen"] = len(fav), len(al)
    return out


def contracts(price: float) -> int:
    return int(STAKE // price)


def fund_shard(ticker: str, need: float, dry: bool) -> None:
    """Kalshi shards collateral by market group (NBA/tennis = 3) and API orders are not auto-funded: move the shortfall in."""
    shard = int(kc.get(f"{kc.K}/markets/{ticker}")["market"].get("exchange_index") or 0)
    bal = {int(b["exchange_index"]): float(b["balance"]) for b in call("GET", "/portfolio/balance")["balance_breakdown"]}
    short = need - bal.get(shard, 0.0)
    if short <= 0 or dry:
        return
    src = max((i for i in bal if i != shard), key=bal.get)
    call("POST", "/portfolio/intra_exchange_instance_transfer",
         {"source": "event_contract", "destination": "event_contract", "amount": math.ceil(min(short, bal[src]) * 10000),
          "source_exchange_shard": src, "destination_exchange_shard": shard, "client_transfer_id": str(uuid.uuid4())})
    for _ in range(8):  # transfer is async; balance endpoint rate-limits fast polling
        time.sleep(3)
        try:
            b = {int(x["exchange_index"]): float(x["balance"]) for x in call("GET", "/portfolio/balance")["balance_breakdown"]}
        except RuntimeError:
            continue
        if b.get(shard, 0.0) >= min(need, bal.get(shard, 0.0) + bal[src]) - 0.01:
            return


def recover(order: dict) -> dict | None:
    """The order as Kalshi recorded it, in the POST response's shape; None if Kalshi never got it."""
    o = next((o for o in call("GET", f"/portfolio/orders?ticker={order['ticker']}&limit=100")["orders"]
              if o.get("client_order_id") == order["client_order_id"]), None)
    if o is None:
        return None
    n = float(o["fill_count_fp"])
    cost = float(o["taker_fill_cost_dollars"]) + float(o["maker_fill_cost_dollars"])
    fee = float(o["taker_fees_dollars"]) + float(o["maker_fees_dollars"])
    return {"order_id": o["order_id"], "fill_count": n, "average_fill_price": cost / n if n else None,
            "average_fee_paid": fee / n if n else 0}


def place(r: dict, mk: dict, limit: float, dry: bool) -> dict:
    n = contracts(limit)
    fund_shard(mk["ticker"], STAKE + kc.kalshi_fee(n, limit), dry)
    order = {"ticker": mk["ticker"], "side": "bid", "count": f"{n}.00", "price": f"{limit:.4f}",
             "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross", "client_order_id": str(uuid.uuid4())}
    if dry:
        return {**r, "status": "dry", "order": order}
    try:
        res = call("POST", "/portfolio/events/orders", order)
    except Exception:  # timeout or 5xx can still fill: look the order up by client_order_id before calling it a skip
        time.sleep(3)
        res = recover(order)
        if res is None:
            raise
    filled = float(res.get("fill_count") or 0)
    if filled < 1:
        return {**r, "status": "nofill", "order": order}
    px = float(res.get("average_fill_price") or limit); fee = float(res.get("average_fee_paid") or 0) * filled
    return {**r, "status": "open", "order_id": res.get("order_id"), "contracts": filled, "px": px,
            "cost": round(filled * px, 2), "fee": round(fee, 2)}


def tick(dry: bool) -> None:
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    rs = rows(); texts = []
    for s in signals(st):
        r = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "src": s["src"], "cond": s["cond"], "outcome": s["outcome"],
             "title": s.get("title"), "insider_px": s["insider_px"], "insider_usdc": s.get("usdc")}
        try:
            if OFF.exists():
                raise ValueError("LIVE_OFF")
            pm = ct.market_for(s["cond"]) or {}
            start = pm.get("gameStartTime")
            if start and datetime.fromisoformat(start.replace(" ", "T").replace("+00", "+00:00")) < datetime.now(UTC):
                raise ValueError("game started")
            if s.get("kalshi"):  # alert book already matched it
                mk = {"ticker": s["kalshi"], "yes_ask_dollars": s["ask"]}
            else:
                mk, why = kc.match_kalshi(pm, s["outcome"])
                if not mk:
                    raise ValueError(why)
            if any(game(x.get("kalshi") or "") == game(mk["ticker"]) and x.get("status") in ("open", "won", "lost") for x in rs):
                raise ValueError("already bet this game")
            limit = math.floor(min(s["insider_px"] + fav_lane.SLIP, MAX_PRICE) * 100) / 100
            r.update(kalshi=mk["ticker"], ask=float(mk.get("yes_ask_dollars") or 0), limit=limit)
            need = STAKE + kc.kalshi_fee(contracts(limit), limit)
            if headroom(rs) < need:
                raise ValueError(f"loss cap: headroom {headroom(rs):.0f}")
            bal = call("GET", "/portfolio/balance")["balance"] / 100
            if bal < need:
                raise ValueError(f"balance ${bal:.2f}")
            r = place(r, mk, limit, dry)
        except Exception as e:
            r.update(status="skip", reason=str(e)[:160])
        print(r["status"], r.get("kalshi") or r.get("reason"), (r.get("title") or "")[:50], flush=True)
        if r["status"] == "skip" and r["reason"].startswith(("loss cap", "balance", "kalshi 4")):  # only problems that stop betting
            texts.append(f"BOT STOPPED: {r['reason'][:60]}")
        rs.append(r)
        if not dry:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
    texts += settle(rs, dry)
    if texts:
        print(sms_alerts.send_sms("Kalshi\n" + "\n".join(texts[:6]) + "\n" + balance_line(), dry))
    if not dry:
        STATE.write_text(json.dumps(st))


def settle(rs: list[dict], dry: bool) -> list[str]:
    out = []
    for r in rs:
        if r.get("status") != "open":
            continue
        try:
            mk = kc.get(f"{kc.K}/markets/{r['kalshi']}")["market"]
        except Exception as e:  # one bad lookup must not block the other settlements
            print("settle", r["kalshi"], e, flush=True)
            continue
        if mk.get("result") not in ("yes", "no"):
            continue
        won = mk["result"] == "yes"
        r.update(status="won" if won else "lost", pnl=round((r["contracts"] if won else 0) - r["cost"] - r["fee"], 2))
        out.append(f"{'WON' if won else 'LOST'} {r['outcome']} {'+' if won else '-'}${abs(r['pnl']):,.0f}")
    if out and not dry:
        LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rs))
    return out


def balance_line() -> str:
    try:
        b = call("GET", "/portfolio/balance")
        return f"Balance ${(b['balance'] + (b.get('portfolio_value') or 0)) / 100:,.0f}"
    except Exception:
        return "Balance unavailable"


def book_line(rs: list[dict]) -> str:
    done = [r for r in rs if r.get("status") in ("won", "lost")]
    w = sum(r["status"] == "won" for r in done)
    return (f"Live: {w}-{len(done) - w}, {sum(r['pnl'] for r in done):+.0f}, "
            f"{sum(r.get('status') == 'open' for r in rs)} open, cap left ${headroom(rs):.0f}")


def report() -> None:
    rs = rows()
    for r in rs:
        print(r["ts"][:16], r["status"], r.get("kalshi") or r.get("reason"), r.get("px", ""), r.get("pnl", ""), (r.get("title") or "")[:40])
    print(book_line(rs))
    print("balance $%.2f" % (call("GET", "/portfolio/balance")["balance"] / 100))


if __name__ == "__main__":
    report() if sys.argv[1:2] == ["report"] else tick("--dry-run" in sys.argv)
