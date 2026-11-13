"""Text the high-signal subset of insider alerts to Josh's phone via Twilio (local fork).

Reads new lines of ~/.polywatch/rule_hits.jsonl (live, games already started are never there),
picks the ones worth a text, and sends one SMS each. Telegram stays the firehose.

Worth a text: a hit in the favorites lane (scripts/fav_lane.py: fresh wallet, 0.60-0.85,
resolves within 7 days) with a positive bet and the price still under the card's max price.
Never: the same market twice in 24h, or more than SMS_DAILY_CAP texts a day.

Env: SMS_TO (default +16462752111), SMS_FROM (default +18053087751), SMS_DAILY_CAP (8),
     SMS_BANKROLL (1000), SMS_MAX_BET_PCT (0.05).
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

import fav_lane

HOME = Path.home() / ".polywatch"
HITS = HOME / "rule_hits.jsonl"
STATE = HOME / "sms_state.json"
LOG = HOME / "sms.log"
CREDS = Path.home() / ".gstack" / "credentials"


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


def main() -> None:
    import alert_card  # heavy (sklearn); send_sms callers skip it

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
    for line in lines:
        try:
            hit = json.loads(line)
        except ValueError:
            continue
        key = str(hit.get("market"))  # one text per market a day, however many wallets pile in
        if key in state["sent"] or not fav_lane.prefilter(hit):
            continue
        if state["days"].get(day, 0) >= cap:
            break
        try:
            rec = alert_card.recommend(hit)
            if not fav_lane.in_lane(hit, rec["market"]):
                continue  # outside the favorites lane
            now_px = alert_card._now_price(rec["market"], hit.get("outcome") or "")
            if rec["bet_usd"] <= 0 or (now_px is not None and now_px > rec["max_price"]):
                continue  # no edge left at the price you can get now
            body, _, _ = alert_card.build(hit, publish_page=not args.dry_run)
        except Exception as e:  # no card means the lane check never ran: no text
            print(f"card failed: {e}")
            continue
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
