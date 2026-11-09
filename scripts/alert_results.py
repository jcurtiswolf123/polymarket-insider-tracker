"""Paper book of every SMS alert at $100 each; texts new resolutions plus the running total.

uv run --env-file .env python scripts/alert_results.py [--dry-run]
shortcut: fill assumed at the flagged wallet's price, real fills are worse; fine for a paper week.
"""
import json, subprocess, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sms_alerts

STAKE = 100.0
HOME = Path.home() / ".polywatch"
STATE = HOME / "alert_results_state.json"


def sql(q: str) -> str:
    r = subprocess.run(["docker", "exec", "-i", "polymarket-postgres", "psql", "-U", "tracker", "-d",
                        "polymarket_tracker", "-At", "-F", "\t", "-c", q], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.stderr)
    return r.stdout


def alerts():
    seen = set()
    for line in (HOME / "sms.log").read_text().splitlines():
        f = line.split("\t")
        if len(f) == 4 and f[2].startswith("0x") and f[2] not in seen:
            seen.add(f[2])
            w, cid = f[2].split("|", 1) if "|" in f[2] else ("", f[2])
            yield f[0], w, cid


def market(cid: str) -> dict:
    req = urllib.request.Request(f"https://clob.polymarket.com/markets/{cid}", headers={"User-Agent": "pw"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def settle(ts, w, cid):
    """Returns (key, won, pnl, label) for a resolved alert, else None."""
    if not all(c in "0123456789abcdefx" for c in (w + cid).lower()):
        return None  # ids go into SQL; refuse anything that is not hex
    wq = f"and wallet_address ilike '{w}'" if w else ""
    row = sql(f"select asset_id, side, price from risk_assessments where market_id='{cid}' and should_alert {wq} "
              f"order by abs(extract(epoch from created_at - '{ts}-08'::timestamptz)) limit 1").strip("\n")
    if not row:
        return None
    asset, side, price = row.split("\t"); price = float(price)
    m = market(cid); toks = m.get("tokens", [])
    tok = next((t for t in toks if t.get("token_id") == asset), None)
    if not tok or not any(t.get("winner") for t in toks):
        return None
    p = price if side == "BUY" else 1 - price
    if not 0 < p < 1:
        return None
    won = tok["winner"] if side == "BUY" else not tok["winner"]
    pnl = STAKE * (1 / p - 1) if won else -STAKE
    pick = tok["outcome"] if side == "BUY" else f"not {tok['outcome']}"
    return f"{w}|{cid}", won, pnl, f"{'WON' if won else 'LOST'} {pick} @{p:.2f} {pnl:+.0f} ({m.get('question', '')[:40]})"


def main(dry: bool):
    state = json.loads(STATE.read_text()) if STATE.exists() else {"done": {}}
    new = []
    for ts, w, cid in alerts():
        key = f"{w}|{cid}"
        if key in state["done"]:
            continue
        try:
            s = settle(ts, w, cid)
        except Exception as e:  # one bad market must not stop the rest
            print("skip", cid[:12], e)
            continue
        if s:
            state["done"][key] = {"won": s[1], "pnl": s[2], "label": s[3]}
            new.append(s[3])
    d = state["done"].values()
    wins = sum(x["won"] for x in d); pnl = sum(x["pnl"] for x in d)
    print(f"new={len(new)} book {wins}-{len(d) - wins} {pnl:+.0f}")
    if new:
        # first run would text the whole backlog; mark it sent silently instead
        if state.get("primed"):
            body = "Polywatch paper results\n" + "\n".join(new[:6]) + f"\nBook: {wins}-{len(d) - wins}, {pnl:+.0f} on ${STAKE * len(d):.0f}"
            print(sms_alerts.send_sms(body, dry))
    state["primed"] = True
    if not dry:
        STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
