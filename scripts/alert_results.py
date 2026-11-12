"""Paper book of every SMS alert at $100 each; texts new resolutions plus the running total.

uv run --env-file .env python scripts/alert_results.py [--dry-run]
shortcut: fill assumed at the flagged wallet's price, real fills are worse; fine for a paper week.
"""
import json, os, re, shutil, subprocess, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sms_alerts

STAKE = 200.0
HOME = Path.home() / ".polywatch"
STATE = HOME / "alert_results_state.json"


def sql(q: str) -> str:
    env = os.environ
    # Mac: Postgres in docker. Fly: Postgres local, psql on PATH.
    pre = (["docker", "exec", "-i", "polymarket-postgres", "psql"] if shutil.which("docker")
           else ["psql", "-h", env.get("POSTGRES_HOST", "localhost")])
    r = subprocess.run(pre + ["-U", env.get("POSTGRES_USER", "tracker"), "-d", env.get("POSTGRES_DB", "polymarket_tracker"),
                              "-At", "-F", "\t", "-c", q], capture_output=True, text=True,
                       env={**env, "PGPASSWORD": env.get("POSTGRES_PASSWORD", "")})
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
            yield f[0], w, cid, f[3]


def market(cid: str) -> dict:
    req = urllib.request.Request(f"https://clob.polymarket.com/markets/{cid}", headers={"User-Agent": "pw"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def settle(ts, w, cid, body=""):
    """Returns (key, won, pnl, label) for a resolved alert, else None."""
    if not all(c in "0123456789abcdefx" for c in (w + cid).lower()):
        return None  # ids go into SQL; refuse anything that is not hex
    # The text names the side and price; trust it first. The nearest flagged DB row can be another
    # wallet's later SELL (USC 11-11 graded LOST from a 0.999 sell after the game).
    mt = re.search(r"bought (.+?) @([0-9.]+)", body)
    if mt:
        m = market(cid); toks = m.get("tokens", [])
        tok = next((t for t in toks if t.get("outcome") == mt.group(1).strip()), None)
        if tok:
            if not any(t.get("winner") for t in toks):
                return None
            p = float(mt.group(2))
            if not 0 < p < 1:
                return None
            won = bool(tok["winner"]); pnl = STAKE * (1 / p - 1) if won else -STAKE
            return f"{w}|{cid}", won, pnl, f"{'WON' if won else 'LOST'} {tok['outcome']} @{p:.2f} {pnl:+.0f} ({m.get('question', '')[:40]})"
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
    for ts, w, cid, body in alerts():
        key = f"{w}|{cid}"
        if key in state["done"]:
            continue
        try:
            s = settle(ts, w, cid, body)
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


def _fav_line() -> str:
    import fav_lane
    s = fav_lane.summarize(fav_lane.ledger_rows())
    if not s.get("resolved"):
        return f"0 resolved, {s.get('open', 0)} open"
    won = round(s["win_rate"] * s["resolved"])
    return f"{won}-{s['resolved'] - won}, {s['pnl']:+.0f}, {s['open']} open"


def _research_line() -> str:
    sys.path.insert(0, str(Path(__file__).parent / "edge"))
    import research_desk
    rows = research_desk.ledger()
    done = [r for r in rows if "won" in r]
    return f"{len(rows)} forecasts, {len(done)} resolved"


def _hormuz_line() -> str:
    r = json.loads((Path.home() / ".polywatch/edge/hormuz_log.jsonl").read_text().splitlines()[-1])
    return f"ma7 {r['ma7']:.1f} vs 60, model {r['model_p']:.0%} vs mkt {(r.get('market') or {}).get('yes', 0):.0%}"


def _backfill_line() -> str:
    h = Path.home() / ".polywatch/history"
    if (h / "DONE").exists():  # finished: the one lane with copyable edge, ROI at +2m/+30m
        l = next(l for l in (h / "eval.txt").read_text().splitlines() if l.startswith("fresh + size"))
        p = l.split()
        return f"done, fresh+size {p[p.index('@2m') + 1]} @2m {p[p.index('@30m') + 1]} @30m"
    return (h / "run.log").read_text().splitlines()[-1][:60]


def summary(dry: bool):
    """One nightly text: alert book (every alert, $200) + Kalshi mirror book."""
    import kalshi_copy
    d = list(json.loads(STATE.read_text())["done"].values()) if STATE.exists() else []
    wins = sum(x["won"] for x in d); pnl = sum(x["pnl"] for x in d)
    body = (f"Polywatch nightly (paper)\n- alerts: {wins}-{len(d) - wins}, {pnl:+.0f} on ${STAKE * len(d):.0f}"
            f"\n- kalshi copy: {kalshi_copy.book_line(kalshi_copy.rows())[6:]}")
    for name, fn in (("favorites", _fav_line), ("research", _research_line), ("hormuz", _hormuz_line), ("backfill", _backfill_line)):
        try:
            body += f"\n- {name}: {fn()}"
        except Exception as e:  # one broken lane must not stop the nightly text
            body += f"\n- {name}: error {type(e).__name__}"
    print(body)
    print(sms_alerts.send_sms(body, dry))


if __name__ == "__main__":
    summary("--dry-run" in sys.argv) if "--summary" in sys.argv else main("--dry-run" in sys.argv)
