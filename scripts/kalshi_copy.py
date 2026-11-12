"""Paper copy lane: every Polymarket SMS alert on a game winner market is mirrored on the
matching Kalshi game market at Kalshi's live ask, $200 a bet, Kalshi taker fee included.
Settled bets are texted with the running book.

uv run --env-file .env python scripts/kalshi_copy.py tick [--dry-run] | report
shortcut: moneyline game markets only (no spreads, totals, futures); add series to LEAGUES to widen.
Paper only: this file never places an order.
"""
import json, math, re, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sms_alerts

STAKE = 200.0
MAX_CHASE = 0.05  # skip if Kalshi ask is this much worse than the leader's price
MAX_PRICE = 0.90
HOME = Path.home() / ".polywatch"
STATE = HOME / "kalshi_copy_state.json"
LEDGER = HOME / "kalshi_copy_ledger.jsonl"
K = "https://api.elections.kalshi.com/trade-api/v2"
# Polymarket slug prefix -> Kalshi series
LEAGUES = {"nba": "KXNBAGAME", "nfl": "KXNFLGAME", "nhl": "KXNHLGAME", "mlb": "KXMLBGAME", "wnba": "KXWNBAGAME",
           "cbb": "KXNCAAMBGAME", "cfb": "KXNCAAFGAME", "lol": "KXLOLGAME", "cs2": "KXCS2GAME",
           "val": "KXVALORANTGAME", "dota2": "KXDOTA2GAME", "epl": "KXEPLGAME", "lal": "KXLALIGAGAME",
           "ucl": "KXUCLGAME", "sea": "KXSERIEAGAME", "bun": "KXBUNDESLIGAGAME", "fl1": "KXLIGUE1GAME",
           "mls": "KXMLSGAME", "atp": "KXATPMATCH", "wta": "KXWTAMATCH"}
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def get(url: str):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pw"}), timeout=20))


def kalshi_fee(contracts: int, p: float) -> float:
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100  # Kalshi taker fee, rounded up to the cent


def parse_slug(slug: str):
    """nba-ind-dal-2026-11-09 -> ('nba', 'IND', 'DAL', '26NOV09'); None if not a game slug."""
    m = re.fullmatch(r"([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-(\d{4})-(\d{2})-(\d{2})", slug or "")
    if not m:
        return None
    lg, a, b, y, mo, d = m.groups()
    return lg, a.upper(), b.upper(), f"{y[2:]}{MONTHS[int(mo) - 1]}{d}"


_EVENTS: dict = {}


def events(series: str, status: str) -> list:
    if (series, status) not in _EVENTS:
        _EVENTS[series, status] = get(f"{K}/events?series_ticker={series}&status={status}&limit=200&with_nested_markets=true").get("events", [])
    return _EVENTS[series, status]


def norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def same_team(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a)))


def near_dates(date: str) -> list[str]:
    from datetime import timedelta
    d = datetime.strptime(date, "%y%b%d")
    return [(d + timedelta(days=k)).strftime("%y%b%d").upper() for k in (0, -1, 1)]


def match_kalshi(pm: dict, outcome: str, status: str = "open"):
    """(Kalshi market, how) for the picked team, or (None, reason). Code match first, team-name match second."""
    if pm.get("sportsMarketType") != "moneyline":
        return None, f"not moneyline ({pm.get('sportsMarketType')})"
    s = parse_slug(pm.get("slug", ""))
    if not s:
        return None, "not a game slug"
    lg, a, b, date = s
    series = LEAGUES.get(lg)
    if not series:
        return None, f"no Kalshi series for {lg}"
    outs = json.loads(pm["outcomes"])
    if outcome not in outs or len(outs) != 2:
        return None, "outcome not in 2-way market"
    i = outs.index(outcome); code, other = (a, b)[i], outs[1 - i]
    evs = events(series, status)
    for ev in evs:  # 1) same team codes and date, as in nba-ind-dal-2026-11-09 vs KXNBAGAME-26NOV09INDDAL
        tail = ev["event_ticker"].split("-", 1)[-1]
        if tail.startswith(date) and a in tail and b in tail:
            mk = next((m for m in ev.get("markets", []) if m["ticker"].endswith("-" + code)), None)
            if mk:
                return mk, "code"
    for d in near_dates(date):  # 2) codes differ: both team names must match the two markets' team labels
        for ev in evs:
            if not ev["event_ticker"].split("-", 1)[-1].startswith(d):
                continue
            mks = ev.get("markets", [])
            mine = [m for m in mks if same_team(m.get("yes_sub_title"), outcome)]
            theirs = [m for m in mks if same_team(m.get("yes_sub_title"), other)]
            if len(mine) == 1 and theirs and mine[0] not in theirs:
                return mine[0], "name"
    return None, f"no Kalshi event {series} {date} {a}/{b}"


def new_alerts(state: dict):
    for line in (HOME / "sms.log").read_text().splitlines():
        f = line.split("\t")
        if len(f) == 4 and f[2].startswith("0x") and f[2] not in state["seen"]:
            yield f[0], f[2], f[3]


def leader(key: str, body: str):
    """(condition_id, outcome, leader_price) from the alert text."""
    cid = key.split("|")[-1]
    m = re.search(r"(?:bought|BUY) (.+?) @([0-9.]+)", body)
    return cid, (m.group(1).strip() if m else ""), (float(m.group(2)) if m else None)


def db_outcome(key: str, ts: str, pm: dict) -> str:
    import alert_results
    w, cid = key.split("|", 1) if "|" in key else ("", key)
    if not all(c in "0123456789abcdefx" for c in (w + cid).lower()):
        return ""
    wq = f"and wallet_address ilike '{w}'" if w else ""
    row = alert_results.sql(f"select asset_id, side from risk_assessments where market_id='{cid}' and should_alert {wq} "
                            f"order by abs(extract(epoch from created_at - '{ts}-08'::timestamptz)) limit 1").strip()
    if not row:
        return ""
    asset, side = row.split("\t")
    toks, outs = json.loads(pm.get("clobTokenIds") or "[]"), json.loads(pm.get("outcomes") or "[]")
    if asset not in toks or len(outs) != 2:
        return ""
    i = toks.index(asset)
    return outs[i if side == "BUY" else 1 - i]


def tick(dry: bool):
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": [], "texted": []}
    for ts, key, body in list(new_alerts(state)):
        state["seen"].append(key)
        cid, outcome, lpx = leader(key, body)
        row = {"ts": ts, "key": key, "outcome": outcome, "leader_px": lpx}
        try:
            pms = get(f"https://gamma-api.polymarket.com/markets?condition_ids={cid}")
            if not pms:  # one empty reply must not drop a live alert; closed markets only show with closed=true
                time.sleep(3)
                pms = get(f"https://gamma-api.polymarket.com/markets?condition_ids={cid}")
                if not pms and get(f"https://gamma-api.polymarket.com/markets?condition_ids={cid}&closed=true"):
                    raise ValueError("market closed")
            if not pms:
                raise ValueError("polymarket market not found")
            pm = pms[0]
            row["title"] = pm.get("question")
            outs = json.loads(pm.get("outcomes") or "[]")
            if outcome not in outs:  # alert text lost the side; recover it from the flagged trade
                outcome = db_outcome(key, ts, pm)
                row["outcome"] = outcome
            if not outcome:
                raise ValueError("alert has no outcome")
            start = pm.get("gameStartTime")
            if start and datetime.fromisoformat(start.replace(" ", "T").replace("+00", "+00:00")) < datetime.now(timezone.utc):
                raise ValueError("game already started")
            mk, why = match_kalshi(pm, outcome)
            row["match"] = why
            if not mk:
                raise ValueError(why)
            ask = float(mk.get("yes_ask_dollars") or 0)
            row.update(kalshi=mk["ticker"], ask=ask)
            if not 0 < ask <= MAX_PRICE:
                raise ValueError(f"ask {ask} out of range")
            if lpx and ask > lpx + MAX_CHASE:
                raise ValueError(f"ask {ask} chased past leader {lpx}")
            n = int(STAKE // ask)
            row.update(status="open", contracts=n, cost=round(n * ask, 2), fee=kalshi_fee(n, ask))
        except Exception as e:
            row.update(status="skip", reason=str(e)[:120])
        print(row.get("status"), row.get("kalshi") or row.get("reason"), row.get("title"))
        if not dry:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
    settle(state, dry)
    if not dry:
        STATE.write_text(json.dumps(state))


def rows():
    return [json.loads(l) for l in LEDGER.read_text().splitlines()] if LEDGER.exists() else []


def settle(state: dict, dry: bool):
    rs = rows(); changed = False; new = []
    for r in rs:
        if r.get("status") != "open":
            continue
        mk = get(f"{K}/markets/{r['kalshi']}")["market"]
        if mk.get("result") not in ("yes", "no"):
            continue
        won = mk["result"] == "yes"
        r.update(status="won" if won else "lost",
                 pnl=round((r["contracts"] - r["cost"] if won else -r["cost"]) - r["fee"], 2))
        changed = True
        new.append(f"{'WON' if won else 'LOST'} {r['kalshi'].split('-')[-1]} @{r['ask']:.2f} {r['pnl']:+.0f} ({(r.get('title') or '')[:30]})")
    if changed and not dry:
        LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rs))
    if new:
        print(sms_alerts.send_sms("Kalshi copy (paper)\n" + "\n".join(new[:6]) + "\n" + book_line(rs), dry))


def book_line(rs) -> str:
    done = [r for r in rs if r.get("status") in ("won", "lost")]
    w = sum(r["status"] == "won" for r in done); pnl = sum(r["pnl"] for r in done)
    opn = sum(r.get("status") == "open" for r in rs)
    return f"Book: {w}-{len(done) - w}, {pnl:+.0f} on ${sum(r['cost'] for r in done):.0f}, {opn} open"


def report():
    rs = rows()
    for r in rs:
        print(r["ts"][:16], r["status"], r.get("kalshi") or r.get("reason"), r.get("ask", ""), r.get("pnl", ""), (r.get("title") or "")[:40])
    print(book_line(rs))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    report() if cmd == "report" else tick("--dry-run" in sys.argv)
