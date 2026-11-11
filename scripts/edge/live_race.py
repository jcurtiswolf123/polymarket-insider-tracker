"""Live goal race: log when Polymarket, Kalshi and ESPN each show a soccer goal, to the millisecond.
  uv run python -I scripts/edge/live_race.py            (launchd com.josh.polywatch-race, KeepAlive)
Writes ~/.polywatch/race/<UTC date>.jsonl, one JSON per event: src = pm_trade | pm_quote | kalshi | espn,
t = source timestamp (s), rt = local receive time (s). scripts/edge/race_report.py turns it into who-moved-first.
"""
import asyncio, json, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import websockets

OUT = Path.home() / ".polywatch/race"; OUT.mkdir(exist_ok=True)
SOCCER_Q = re.compile(r"win on 20\d\d|end in a draw|O/U \d", re.I)
KAL = re.compile(r"^KX(EPL|LALIGA|SERIEA|BUNDESLIGA|LIGUE1|UCL|UEL|UECL|MLS|EREDIVISIE|LIGAMX|LIGAPORTUGAL|WCQ|WC|UNL|SAUDIPL|BRASILEIRAO|ARG|EFLCHAMP|SCOTPREM|CONCACAF|COPA)\w*(GAME|TOTAL)-")
ESPN = ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "usa.1", "uefa.champions", "uefa.europa", "ned.1", "por.1", "mex.1",
        "fifa.worldq.uefa", "fifa.worldq.conmebol", "fifa.worldq.concacaf", "fifa.worldq.afc", "fifa.worldq.caf", "uefa.nations", "eng.2", "bra.1", "arg.1"]


def log(rec):
    rec["rt"] = time.time()
    with open(OUT / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


async def live_tokens(c):
    """Token ids of Polymarket soccer win/draw/O-U markets whose game started in the last 2.5h or starts within 15 min."""
    now = datetime.now(timezone.utc); toks = {}
    for off in range(0, 3000, 100):
        r = await c.get("https://gamma-api.polymarket.com/events", params={  # sports events carry the kickoff in endDate
            "tag_slug": "soccer", "closed": "false", "limit": 100, "offset": off,
            "end_date_min": f"{now - timedelta(hours=3):%Y-%m-%dT%H:%M:%SZ}", "end_date_max": f"{now + timedelta(minutes=20):%Y-%m-%dT%H:%M:%SZ}"})
        evs = r.json()
        if not isinstance(evs, list):  # past the last page gamma answers with an error object
            break
        for e in evs:
            for m in e.get("markets") or []:
                g = m.get("gameStartTime")
                if not g or not SOCCER_Q.search(m.get("question") or ""):
                    continue
                gs = datetime.fromisoformat(g.replace(" ", "T").replace("+00", "+00:00") if g.endswith("+00") else g.replace("Z", "+00:00"))
                if now - timedelta(hours=2.5) <= gs <= now + timedelta(minutes=15):
                    for i, tok in enumerate(json.loads(m.get("clobTokenIds") or "[]")):
                        toks[tok] = (m["conditionId"], e["slug"], m["question"][:80], i)
        if len(evs) < 100:
            break
    return toks


async def polymarket():
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            try:
                toks = await live_tokens(c)
            except Exception as e:
                log({"src": "err", "where": "gamma", "e": repr(e)}); await asyncio.sleep(60); continue
            log({"src": "pm_subscribe", "n": len(toks), "slugs": sorted({v[1] for v in toks.values()})})
            if not toks:
                await asyncio.sleep(300); continue
            try:
                async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", ping_interval=10, max_size=None) as ws:
                    await ws.send(json.dumps({"assets_ids": list(toks), "type": "market"}))
                    until = time.time() + 600  # re-discover live games every 10 min
                    while time.time() < until:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue
                        for ev in (json.loads(raw) if raw.startswith("[") else [json.loads(raw)]) if raw not in ("PONG", "") else []:
                            et = ev.get("event_type")
                            if et == "last_trade_price" and ev.get("asset_id") in toks:
                                cid, slug, q, oi = toks[ev["asset_id"]]
                                log({"src": "pm_trade", "t": int(ev["timestamp"]) / 1000, "cid": cid, "slug": slug, "q": q, "oi": oi, "p": float(ev["price"]), "size": float(ev.get("size") or 0), "side": ev.get("side")})
                            elif et == "price_change":
                                for ch in ev.get("price_changes") or []:
                                    if ch.get("asset_id") in toks and ch.get("best_bid") is not None:
                                        cid, slug, q, oi = toks[ch["asset_id"]]
                                        log({"src": "pm_quote", "t": int(ev["timestamp"]) / 1000, "cid": cid, "slug": slug, "q": q, "oi": oi, "bid": float(ch["best_bid"]), "ask": float(ch.get("best_ask") or 0)})
            except Exception as e:
                log({"src": "err", "where": "pm_ws", "e": repr(e)}); await asyncio.sleep(5)


async def kalshi():
    since = int(time.time()) - 5; seen = set()
    async with httpx.AsyncClient(timeout=15) as c:
        while True:
            try:
                r = await c.get("https://api.elections.kalshi.com/trade-api/v2/markets/trades", params={"limit": 1000, "min_ts": since})
                for t in r.json().get("trades") or []:
                    if t["trade_id"] in seen or not KAL.match(t["ticker"]):
                        continue
                    seen.add(t["trade_id"])
                    log({"src": "kalshi", "t": datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp(), "ticker": t["ticker"],
                         "p": float(t["yes_price_dollars"]), "size": float(t.get("count_fp") or 0), "side": t.get("taker_side")})
                since = int(time.time()) - 5
                if len(seen) > 200000:
                    seen = set(list(seen)[-50000:])
            except Exception as e:
                log({"src": "err", "where": "kalshi", "e": repr(e)}); await asyncio.sleep(3)
            await asyncio.sleep(1)


async def espn():
    last = {}
    async with httpx.AsyncClient(timeout=15) as c:
        while True:
            for lg in ESPN:
                try:
                    r = await c.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/scoreboard")
                    for ev in r.json().get("events") or []:
                        comp = (ev.get("competitions") or [{}])[0]
                        score = tuple(x.get("score") for x in comp.get("competitors") or [])
                        k = ev.get("id")
                        if k in last and last[k] != score:
                            log({"src": "espn", "t": time.time(), "league": lg, "name": ev.get("name"), "score": score, "prev": last[k], "clock": (ev.get("status") or {}).get("displayClock")})
                        last[k] = score
                except Exception:
                    pass
            await asyncio.sleep(3)


async def main():
    await asyncio.gather(polymarket(), kalshi(), espn())


if __name__ == "__main__":
    asyncio.run(main())
