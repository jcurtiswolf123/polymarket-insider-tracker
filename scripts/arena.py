"""Strategy arena: a population of paper-trading agents on Polymarket and Kalshi that learns for 30 days.

Each agent is a genome (strategy kind + filters + sizing) with a $1,000 paper bankroll. Every tick it buys
the outcomes its strategy picks at the live ask plus slippage and a Kalshi-style taker fee. Settled bets
teach two memories: the agent's own losing buckets (it stops buying them) and a shared pattern table every
agent can read. Once a day the bottom quarter is retired and replaced by mutated children of the best
agents, plus some fresh random ones so the search keeps exploring. Paper only: nothing here places orders.

    arena.py tick            fetch markets, settle, open bets, evolve once a day
    arena.py report [--sms]  leaderboard, shared patterns, winner check
    arena.py selftest        assert-based checks, no network
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("POLYWATCH_HOME", Path.home() / ".polywatch"))
DB = HOME / "arena.db"
HITS = HOME / "rule_hits.jsonl"
START_BANK = 1000.0
POP = int(os.environ.get("ARENA_POP", "24"))
END = os.environ.get("ARENA_END", "2026-12-24")  # 30 days from launch; after this only settles
SLIP = 0.01
WIN_N, WIN_ROI, WIN_Z = 200, 0.05, 2.5  # z 2.5 because ~50 genomes get tried: one lucky one is expected at 1.65
KINDS = ["band", "momentum", "reversion", "volume", "insider", "random"]
SPORT = re.compile(
    r"^(nfl|nba|mlb|nhl|cfb|cbb|epl|ucl|lal|sea|bun|fl1|mls|ufc|atp|wta|cs2|lol|val|dota)-|-vs-| vs\.? "
    r"|GAME|MATCH|KXNBA|KXNFL|KXNHL|KXMLB|KXNCAA|KXUFC|KXATP|KXWTA|KXEPL|KXUCL|SOCCER|ESPORT|KXLOL|KXCS2|KXVAL|KXDOTA",
    re.I,
)
UA = {"User-Agent": "Mozilla/5.0 arena"}


def get(url: str, timeout: int = 20):
    for i in range(3):  # Kalshi rate-limits: back off on 429 and timeouts
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.HTTPError, TimeoutError) as e:
            if i == 2 or (isinstance(e, urllib.error.HTTPError) and e.code != 429):
                raise
            time.sleep(2 * (i + 1))


def ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- market data ----------

def pm_rows(pages: int = 4) -> list[dict]:
    rows = []
    for p in range(pages):
        try:
            ms = get(f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500&offset={p*500}"
                     "&order=volume24hr&ascending=false")
        except OSError:
            break
        for m in ms:
            try:
                outs, ask, bid = json.loads(m["outcomes"]), float(m["bestAsk"]), float(m["bestBid"])
            except (KeyError, TypeError, ValueError):
                continue
            if len(outs) != 2 or not m.get("acceptingOrders") or not 0 < bid < ask < 1:
                continue
            chg = float(m.get("oneDayPriceChange") or 0)
            base = dict(venue="pm", mkt=m["id"], cond=m.get("conditionId"), title=m.get("question", "")[:120],
                        end=ts(m.get("endDate")), vol=float(m.get("volume24hr") or 0),
                        liq=float(m.get("liquidityNum") or 0),
                        sport=bool(SPORT.search(f"{m.get('slug','')} {m.get('question','')}")))
            rows.append({**base, "side": 0, "outcome": outs[0], "ask": ask, "chg": chg})
            rows.append({**base, "side": 1, "outcome": outs[1], "ask": round(1 - bid, 4), "chg": -chg})
    return rows


def kalshi_rows(pages: int = 8) -> list[dict]:
    rows, cursor = [], ""
    for _ in range(pages):
        try:
            d = get("https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000"
                    f"&mve_filter=exclude&cursor={cursor}")
        except OSError:
            break
        for m in d.get("markets", []):
            vol = float(m.get("volume_24h_fp") or 0)
            ya, na = float(m.get("yes_ask_dollars") or 0), float(m.get("no_ask_dollars") or 0)
            if vol <= 0 or not (0 < ya < 1 and 0 < na < 1):
                continue
            last, prev = float(m.get("last_price_dollars") or 0), float(m.get("previous_price_dollars") or 0)
            chg = last - prev if prev else 0.0
            base = dict(venue="kalshi", mkt=m["ticker"], cond=None,
                        title=f"{m.get('title','')} {m.get('yes_sub_title','')}"[:120],
                        end=ts(m.get("expected_expiration_time") or m.get("close_time")), vol=vol * max(last, 0.01),
                        liq=float(m.get("open_interest_fp") or 0), sport=bool(SPORT.search(m["ticker"])))
            rows.append({**base, "side": 0, "outcome": "Yes", "ask": ya, "chg": chg})
            rows.append({**base, "side": 1, "outcome": "No", "ask": na, "chg": -chg})
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    return rows


def insider_keys(since: float) -> set[tuple[str, str]]:
    """(condition id, outcome) pairs that polywatch flagged since `since`."""
    out = set()
    if not HITS.exists():
        return out
    with HITS.open() as f:
        for line in f:
            try:
                h = json.loads(line)
            except ValueError:
                continue
            if (ts(h.get("ts")) or 0) >= since and h.get("outcome"):
                out.add((h.get("market"), h["outcome"]))
    return out


# ---------- genomes ----------

def rand_genome(rng: random.Random) -> dict:
    kind = rng.choice(KINDS)
    lo = rng.choice([0.03, 0.10, 0.25, 0.40, 0.55, 0.65, 0.75, 0.85])
    return dict(kind=kind, lo=lo, hi=round(min(0.97, lo + rng.choice([0.1, 0.15, 0.25, 0.4])), 2),
                max_days=rng.choice([0.5, 1, 2, 3, 7, 14, 30]), min_vol=rng.choice([0, 1e3, 1e4, 1e5]),
                sport=rng.choice(["any", "sports", "other"]), venue=rng.choice(["any", "pm", "kalshi"]),
                t=rng.choice([0.03, 0.05, 0.1, 0.2]), stake=rng.choice([0.01, 0.02, 0.03]),
                max_open=rng.choice([10, 20, 40]), shared=rng.random() < 0.5)


def mutate(g: dict, rng: random.Random) -> dict:
    c, fresh = dict(g), rand_genome(rng)
    for k in rng.sample(list(fresh), k=rng.randint(1, 3)):
        if k in ("lo", "hi"):
            c["lo"] = round(min(0.95, max(0.02, c["lo"] + rng.choice([-0.1, -0.05, 0.05, 0.1]))), 2)
            c["hi"] = round(min(0.97, max(c["lo"] + 0.05, c["hi"] + rng.choice([-0.1, -0.05, 0.05, 0.1]))), 2)
        else:
            c[k] = fresh[k]
    return c


# ---------- learning memory ----------

def bucket(r: dict) -> str:
    days = ((r["end"] or 0) - time.time()) / 86400
    dband = "<1d" if days < 1 else "<3d" if days < 3 else "<7d" if days < 7 else "<30d" if days < 30 else "30d+"
    return f"{r['venue']}|{'sport' if r['sport'] else 'other'}|p{int(min(r['ask'], 0.99) * 10)}|{dband}"


def bucket_edges(con: sqlite3.Connection, agent: int | None = None) -> dict[str, tuple[int, float]]:
    """bucket -> (settled n, mean (win - fill)). Positive means the market underpriced that bucket."""
    q = "select bucket, count(*), avg(y - fill) from bets where status in ('won','lost')"
    args: tuple = ()
    if agent is not None:
        q, args = q + " and agent=?", (agent,)
    return {b: (n, e) for b, n, e in con.execute(q + " group by bucket", args)}


def candidates(g: dict, rows: list[dict], hits: set, rng: random.Random) -> list[dict]:
    horizon = time.time() + g["max_days"] * 86400
    ok = [r for r in rows
          if g["lo"] <= r["ask"] <= g["hi"] and r["end"] and time.time() < r["end"] <= horizon
          and r["vol"] >= g["min_vol"]
          and (g["venue"] == "any" or r["venue"] == g["venue"])
          and (g["sport"] == "any" or (g["sport"] == "sports") == r["sport"])]
    k = g["kind"]
    if k == "momentum":
        ok = sorted((r for r in ok if r["chg"] >= g["t"]), key=lambda r: -r["chg"])
    elif k == "reversion":
        ok = sorted((r for r in ok if r["chg"] <= -g["t"]), key=lambda r: r["chg"])
    elif k == "volume":
        ok = sorted((r for r in ok if r["chg"] > 0 and r["vol"] >= g["t"] * 10 * max(r["liq"], 1)),
                    key=lambda r: -r["vol"] / max(r["liq"], 1))
    elif k == "insider":
        ok = [r for r in ok if (r["cond"], r["outcome"]) in hits]
    elif k == "random":
        ok = rng.sample(ok, k=min(len(ok), 5))
    else:  # band: soonest resolution first, so the agent learns fast
        ok = sorted(ok, key=lambda r: r["end"])
    return ok


def fee(contracts: float, px: float) -> float:
    return 0.07 * contracts * px * (1 - px)  # Kalshi taker formula, applied to both venues as the conservative case


def settle_pnl(contracts: float, stake: float, fee_: float, value: float) -> float:
    return round(contracts * value - stake - fee_, 4)


# ---------- db ----------

def db() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript("""
    create table if not exists agents(id integer primary key, genome text, born text, retired text, parent int);
    create table if not exists bets(id integer primary key, agent int, venue text, mkt text, side int, outcome text,
      title text, fill real, contracts real, stake real, fee real, opened text, end_ts real, bucket text,
      status text default 'open', pnl real, y real, settled text);
    create table if not exists meta(k text primary key, v text);
    create index if not exists bets_open on bets(status, venue, mkt);
    """)
    return con


def meta(con, k, v=None):
    if v is None:
        r = con.execute("select v from meta where k=?", (k,)).fetchone()
        return r[0] if r else None
    con.execute("insert or replace into meta values(?,?)", (k, str(v)))


def live_agents(con) -> list[tuple[int, dict]]:
    return [(i, json.loads(g)) for i, g in con.execute("select id, genome from agents where retired is null")]


def seed(con, rng):
    n = len(live_agents(con))
    for _ in range(POP - n):
        con.execute("insert into agents(genome, born) values(?,?)", (json.dumps(rand_genome(rng)), now_iso()))


def stats(con, agent: int) -> dict:
    n, pnl, staked, wins = con.execute(
        "select count(*), coalesce(sum(pnl),0), coalesce(sum(stake+fee),0), coalesce(sum(status='won'),0) "
        "from bets where agent=? and status in ('won','lost','void')", (agent,)).fetchone()
    rois = [p / s for p, s in con.execute(
        "select pnl, stake+fee from bets where agent=? and status in ('won','lost','void')", (agent,))]
    mean = sum(rois) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in rois) / (n - 1)) if n > 1 else 1.0
    se = sd / math.sqrt(n) if n else 1.0
    opn = con.execute("select count(*), coalesce(sum(stake+fee),0) from bets where agent=? and status='open'",
                      (agent,)).fetchone()
    return dict(n=n, wins=wins, pnl=pnl, roi=mean, lcb=mean - WIN_Z * se, score=mean - se,
                bank=START_BANK + pnl, open=opn[0], at_risk=opn[1])


# ---------- settle ----------

def resolve(venue: str, mkt: str) -> list[float] | None:
    """Final value per side, or None while unresolved."""
    try:
        if venue == "pm":
            m = get(f"https://gamma-api.polymarket.com/markets/{mkt}")
            if not m.get("closed"):
                return None
            px = [float(x) for x in json.loads(m["outcomePrices"])]
            return px if all(p in (0.0, 1.0) for p in px) or m.get("umaResolutionStatus") == "resolved" else None
        m = get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{mkt}")["market"]
        if m.get("status") not in ("settled", "finalized"):
            return None
        if m.get("result") in ("yes", "no"):
            return [1.0, 0.0] if m["result"] == "yes" else [0.0, 1.0]
        v = float(m.get("settlement_value_dollars") or 0)
        return [v, 1 - v]
    except (OSError, KeyError, ValueError):
        return None


def settle(con) -> int:
    due = con.execute("select distinct venue, mkt from bets where status='open' and end_ts < ? limit 150",
                      (time.time() + 3600,)).fetchall()
    done = 0
    for venue, mkt in due:
        vals = resolve(venue, mkt)
        if vals is None:
            continue
        for bid, side, c, s, f in con.execute(
                "select id, side, contracts, stake, fee from bets where status='open' and venue=? and mkt=?",
                (venue, mkt)).fetchall():
            v = vals[side]
            status = "won" if v == 1 else "lost" if v == 0 else "void"
            con.execute("update bets set status=?, pnl=?, y=?, settled=? where id=?",
                        (status, settle_pnl(c, s, f, v), v, now_iso(), bid))
            done += 1
    return done


# ---------- trade ----------

def trade(con, rows, hits, rng, open_new: bool) -> int:
    if not open_new:
        return 0
    shared = bucket_edges(con)
    taken = {(a, v, m) for a, v, m in con.execute("select agent, venue, mkt from bets where status='open'")}
    placed = 0
    for aid, g in live_agents(con):
        st = stats(con, aid)
        free = g["max_open"] - st["open"]
        if free <= 0 or st["bank"] - st["at_risk"] < 5:
            continue
        own = bucket_edges(con, aid)
        cands = candidates(g, rows, hits, rng)
        if g.get("shared"):  # prefer buckets the whole arena has found underpriced
            cands.sort(key=lambda r: -(shared.get(bucket(r), (0, 0.0))[1] if shared.get(bucket(r), (0,))[0] >= 20 else 0))
        for r in cands:
            if free <= 0:
                break
            b = bucket(r)
            n, e = own.get(b, (0, 0.0))
            if n >= 8 and e < -0.05:  # its own mistake: stop buying a bucket it keeps losing in
                continue
            sn, se_ = shared.get(b, (0, 0.0))
            if g.get("shared") and sn >= 30 and se_ < -0.05:
                continue
            if (aid, r["venue"], r["mkt"]) in taken:
                continue
            px = min(0.99, r["ask"] + SLIP)
            stake = max(5.0, round(g["stake"] * st["bank"], 2))
            if stake > st["bank"] - st["at_risk"]:
                break
            c = stake / px
            con.execute("insert into bets(agent, venue, mkt, side, outcome, title, fill, contracts, stake, fee, opened,"
                        " end_ts, bucket) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, r["venue"], r["mkt"], r["side"], r["outcome"], r["title"], px, c, stake,
                         round(fee(c, px), 4), now_iso(), r["end"], b))
            taken.add((aid, r["venue"], r["mkt"]))
            st["at_risk"] += stake
            free -= 1
            placed += 1
    return placed


def evolve(con, rng) -> list[str]:
    """Retire the bottom quarter of agents with enough settled bets; refill with mutated winners and fresh genomes."""
    ranked = sorted(((stats(con, a), a, g) for a, g in live_agents(con)), key=lambda x: -x[0]["score"])
    mature = [x for x in ranked if x[0]["n"] >= 15]
    if len(mature) < 4:
        return []
    log = []
    elite = [x for x in ranked[:3] if x[0]["n"] >= 15]
    for st, aid, g in mature[-max(1, len(mature) // 4):]:
        con.execute("update agents set retired=? where id=?", (now_iso(), aid))
        if elite and rng.random() < 0.7:
            pst, pid, pg = rng.choice(elite)
            child, parent = mutate(pg, rng), pid
        else:
            child, parent = rand_genome(rng), None
        con.execute("insert into agents(genome, born, parent) values(?,?,?)", (json.dumps(child), now_iso(), parent))
        log.append(f"retired #{aid} {g['kind']} roi {st['roi']:+.1%} n{st['n']} -> child of #{parent or 'random'}")
    return log


def tick() -> None:
    con, rng = db(), random.Random()
    seed(con, rng)
    done = settle(con)
    open_new = datetime.now().strftime("%Y-%m-%d") < END
    last = float(meta(con, "last_tick") or 0)
    rows = pm_rows() + kalshi_rows() if open_new else []
    placed = trade(con, rows, insider_keys(last or time.time() - 3600), rng, open_new)
    meta(con, "last_tick", time.time())
    day = datetime.now().strftime("%Y-%m-%d")
    evo = []
    if meta(con, "last_evolve") != day and datetime.now().hour >= 6:
        evo = evolve(con, rng)
        meta(con, "last_evolve", day)
    con.commit()
    print(f"{now_iso()} rows {len(rows)} settled {done} placed {placed} agents {len(live_agents(con))}"
          + "".join(f"\n  {line}" for line in evo))


# ---------- report ----------

def report_text(con) -> str:
    allst = [(stats(con, a), a, g) for a, g in con.execute("select id, genome from agents")]
    allst = [(s, a, json.loads(g)) for s, a, g in allst]
    live = {a for a, _ in live_agents(con)}
    tot = con.execute("select count(*), coalesce(sum(status='open'),0), coalesce(sum(pnl),0) from bets").fetchone()
    top = sorted((x for x in allst if x[1] in live and x[0]["n"]), key=lambda x: -x[0]["score"])[:5]
    winners = [x for x in allst if x[0]["n"] >= WIN_N and x[0]["roi"] > WIN_ROI and x[0]["lcb"] > 0]
    pats = sorted(((b, n, e) for b, (n, e) in bucket_edges(con).items() if n >= 30), key=lambda x: -x[2])
    lines = [f"Arena {datetime.now():%m-%d} (paper, ends {END})",
             f"- bets {tot[0]}, open {tot[1]}, settled pnl ${tot[2]:+,.0f}",
             f"- agents live {len(live)}, tried {len(allst)}"]
    lines += [f"- #{a} {g['kind']} {g['venue']}/{g['sport']} p{g['lo']}-{g['hi']} <={g['max_days']}d: "
              f"roi {s['roi']:+.1%} n{s['n']} ${s['pnl']:+,.0f}" for s, a, g in top]
    if pats:
        lines.append("- best bucket " + " ".join(f"{b} {e:+.2f} n{n}" for b, n, e in pats[:2]))
        lines.append("- worst bucket " + " ".join(f"{b} {e:+.2f} n{n}" for b, n, e in pats[-2:]))
    lines.append("- WINNER: " + ", ".join(f"#{a} {g['kind']} roi {s['roi']:+.1%} n{s['n']}" for s, a, g in winners)
                 if winners else f"- winner: none yet (needs roi>{WIN_ROI:.0%}, n>={WIN_N}, z{WIN_Z} lower bound >0)")
    return "\n".join(lines)


def report(sms: bool) -> None:
    con = db()
    text = report_text(con)
    print(text)
    if sms:
        sys.path.insert(0, str(Path(__file__).parent))
        from sms_alerts import send_sms
        print("sms", send_sms(text, dry=False))


# ---------- selftest ----------

def selftest() -> None:
    global DB
    import tempfile
    DB = Path(tempfile.mkdtemp()) / "arena.db"
    rng = random.Random(1)
    assert abs(fee(100, 0.5) - 1.75) < 1e-9
    assert settle_pnl(200, 100, 1.75, 1.0) == 98.25 and settle_pnl(200, 100, 1.75, 0.0) == -101.75
    for _ in range(500):
        g = mutate(rand_genome(rng), rng)
        assert 0.02 <= g["lo"] < g["hi"] <= 0.97 and g["kind"] in KINDS
    soon = time.time() + 3600
    rows = [dict(venue="pm", mkt="1", cond="c1", side=0, outcome="Yes", title="a vs b", ask=0.7, chg=0.12, vol=5e4,
                 liq=1e3, end=soon, sport=True),
            dict(venue="pm", mkt="1", cond="c1", side=1, outcome="No", title="a vs b", ask=0.31, chg=-0.12, vol=5e4,
                 liq=1e3, end=soon, sport=True),
            dict(venue="kalshi", mkt="K", cond=None, side=0, outcome="Yes", title="x", ask=0.9, chg=0, vol=10,
                 liq=0, end=time.time() + 40 * 86400, sport=False)]
    base = dict(lo=0.05, hi=0.95, max_days=1, min_vol=0, sport="any", venue="any", t=0.05, stake=0.02,
                max_open=10, shared=False)
    assert [r["side"] for r in candidates({**base, "kind": "momentum"}, rows, set(), rng)] == [0]
    assert [r["side"] for r in candidates({**base, "kind": "reversion"}, rows, set(), rng)] == [1]
    assert [r["side"] for r in candidates({**base, "kind": "insider"}, rows, {("c1", "No")}, rng)] == [1]
    assert candidates({**base, "kind": "band", "max_days": 30}, rows, set(), rng)[-1]["mkt"] == "1"  # K is 40d out
    con = db()
    con.execute("insert into agents(genome, born) values(?,?)", (json.dumps({**base, "kind": "band"}), now_iso()))
    assert trade(con, rows, set(), rng, True) == 1  # one bet per market per agent
    b = bucket(rows[0])
    for i in range(10):  # teach agent 1 that bucket b loses
        con.execute("insert into bets(agent, venue, mkt, side, fill, contracts, stake, fee, bucket, status, pnl, y)"
                    " values(1,'pm',?,0,0.7,10,7,0,?,'lost',-7,0)", (f"old{i}", b))
    con.execute("delete from bets where status='open'")
    assert trade(con, rows, set(), rng, True) == 1 and \
        con.execute("select side from bets where status='open'").fetchone()[0] == 1  # skipped its losing bucket
    print("arena selftest ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    {"tick": tick, "selftest": selftest, "report": lambda: report("--sms" in sys.argv)}[cmd]()
