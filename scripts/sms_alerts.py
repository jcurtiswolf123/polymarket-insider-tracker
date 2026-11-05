"""Text the high-signal subset of insider alerts to Josh's phone via Twilio (local fork).

Reads new lines of ~/.polywatch/rule_hits.jsonl (live, games already started are never there),
picks the ones worth a text, and sends one SMS each. Telegram stays the firehose.

Worth a text:
  - learned model promoted: edge >= SMS_MIN_EDGE and stake >= SMS_MIN_USDC
  - no promoted model yet:  a strong rule (R1 R5 R6 R7 R8 M1) or two+ rules, stake >= SMS_MIN_USDC
Never: the same wallet + market twice in 24h, or more than SMS_DAILY_CAP texts a day.

Env: SMS_TO (default +16462752111), SMS_FROM (default +18053087751), SMS_MIN_EDGE (0.12),
     SMS_MIN_USDC (5000), SMS_MAX_PRICE (0.85), SMS_DAILY_CAP (8),
     SMS_BANKROLL (1000), SMS_MAX_BET_PCT (0.05), SMS_ONLY_BETS (1 = skip "no bet" texts).
Each text carries win chance, bet size and a details link built by scripts/alert_card.py. Twilio creds from ~/.gstack/credentials.
  uv run --env-file .env python scripts/sms_alerts.py [--dry-run] [--test "message"]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import alert_card

HOME = Path.home() / ".polywatch"
HITS = HOME / "rule_hits.jsonl"
STATE = HOME / "sms_state.json"
LOG = HOME / "sms.log"
META = HOME / "model_meta.json"
CREDS = Path.home() / ".gstack" / "credentials"
STRONG = {
    "R1_fresh_big_bet",
    "R5_whale_thin_history",
    "R6_whale_longshot",
    "R7_proven_longshot_hitter",
    "R8_event_sweep",
    "M1_model_edge",
}


def env_f(name: str, default: str) -> float:
    return float(os.environ.get(name, "") or default)


def send_sms(body: str, dry: bool) -> str:
    to = os.environ.get("SMS_TO", "+16462752111")
    frm = os.environ.get("SMS_FROM", "+18053087751")
    if dry:
        return "DRY"
    sid = (CREDS / "twilio-account-sid.txt").read_text().strip()
    tok = (CREDS / "twilio-auth-token.txt").read_text().strip()
    data = urllib.parse.urlencode({"To": to, "From": frm, "Body": body[:900]}).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data
    )
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("sid", "?")
    except urllib.error.HTTPError as e:
        return f"FAIL {e.code} {e.read()[:200]!r}"
    except OSError as e:
        return f"FAIL {e}"


def model_promoted() -> bool:
    try:
        return bool(json.loads(META.read_text()).get("promoted"))
    except (OSError, ValueError):
        return False


def worth_text(hit: dict, promoted: bool) -> bool:
    usdc = float(hit.get("usdc") or 0)
    if usdc < env_f("SMS_MIN_USDC", "5000"):
        return False
    if float(hit.get("price") or 1) > env_f("SMS_MAX_PRICE", "0.85"):
        return False  # buying a near-certain favorite says little
    rules = {r[2:] if r.startswith("W_") else r for r in hit.get("rules") or []}
    edge = hit.get("ml_edge")
    if promoted and edge is not None:
        return float(edge) >= env_f("SMS_MIN_EDGE", "0.12")
    return bool(rules & STRONG) or len(rules) >= 2


def fmt(hit: dict) -> str:
    title = (hit.get("title") or hit.get("slug") or "market")[:90]
    rules = ",".join(r.split("_")[0] for r in hit.get("rules") or [])
    age = float(hit.get("age_h") or 0)
    age_s = f"{age:.0f}h" if age < 48 else f"{age / 24:.0f}d"
    edge = hit.get("ml_edge")
    edge_s = f" | edge {float(edge):+.2f}" if edge is not None else ""
    wallet = hit.get("wallet") or ""
    return (
        f"Polywatch: BUY {hit.get('outcome')} @{float(hit.get('price') or 0):.2f} "
        f"${float(hit.get('usdc') or 0):,.0f}\n{title}\n"
        f"wallet {age_s} old, {hit.get('prior_markets')} mkts | {rules}{edge_s}\n"
        f"polymarket.com/profile/{wallet}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", help="send this text and exit")
    ap.add_argument("--card", action="store_true", help="build the card for the last hit, print it")
    args = ap.parse_args()
    HOME.mkdir(parents=True, exist_ok=True)
    if args.test:
        res = send_sms(args.test, args.dry_run)
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%F %T')}\t{res}\tTEST\t{args.test}\n")
        print(res)
        return
    if args.card:
        hit = json.loads(HITS.read_text().strip().splitlines()[-1])
        body, md, link = alert_card.build(hit, publish_page=not args.dry_run)
        print(md, "\n---SMS---\n" + body, f"\n({len(body)} chars)")
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "offset" not in state:  # first run: start at the end, never text the backlog
        state = {"offset": HITS.stat().st_size if HITS.exists() else 0, "sent": {}, "days": {}}
    if not HITS.exists():
        return
    if state["offset"] > HITS.stat().st_size:  # log was rewritten or rotated
        state["offset"] = HITS.stat().st_size
    with open(HITS) as fh:
        fh.seek(state["offset"])
        lines = fh.readlines()
        state["offset"] = fh.tell()
    now = time.time()
    day = time.strftime("%F")
    state["sent"] = {k: v for k, v in state["sent"].items() if now - v < 86400}
    cap = int(env_f("SMS_DAILY_CAP", "8"))
    promoted = model_promoted()
    for line in lines:
        try:
            hit = json.loads(line)
        except ValueError:
            continue
        key = f"{hit.get('wallet')}|{hit.get('market')}"
        if key in state["sent"] or not worth_text(hit, promoted):
            continue
        if state["days"].get(day, 0) >= cap:
            break
        only_bets = os.environ.get("SMS_ONLY_BETS") == "1"
        try:
            if only_bets and alert_card.recommend(hit)["bet_usd"] <= 0:
                continue  # no edge: no text, no details page
            body, _, _ = alert_card.build(hit, publish_page=not args.dry_run)
        except Exception as e:  # the card is extra; never lose the alert over it
            print(f"card failed: {e}")
            if only_bets:
                continue
            body = fmt(hit)
        res = send_sms(body, args.dry_run)
        state["sent"][key] = now
        state["days"][day] = state["days"].get(day, 0) + 1
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%F %T')}\t{res}\t{key}\t{body!r}\n")
        print(res, body.replace("\n", " | "))
    state["days"] = {
        d: n
        for d, n in state["days"].items()
        if d >= time.strftime("%F", time.localtime(now - 7 * 86400))
    }
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


if __name__ == "__main__":
    main()
