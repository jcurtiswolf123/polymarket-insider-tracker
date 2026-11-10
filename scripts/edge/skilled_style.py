"""What do the z >= 4 wallets do in-play? Price path of their side around each bet, from the market's fill tape.
  uv run python -I scripts/edge/skilled_style.py
"""
import bisect, json, re, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
sys.path.insert(0, str(Path(__file__).parent))
from skilled_decay import fills

H = Path.home() / ".polywatch/history"
bets = [b for b in json.load(open(H / "skilled_follow.json"))["4.0"] if b["inplay"]]
q = {}
for l in open(H / "markets.jsonl"):
    m = json.loads(l); q[m["cid"]] = m
SPORT = [("nba", r"\b(76ers|bulls|celtics|lakers|knicks|heat|warriors|nuggets|suns|spurs|mavericks|pacers|grizzlies|pelicans|jazz|clippers|rockets|bucks|nets|hawks|hornets|magic|pistons|raptors|wizards|cavaliers|kings|trail blazers|thunder|timberwolves)\b"),
         ("soccer", r"win on 20|fc\b|united|city|real madrid|atl[eé]tico|o/u|both teams"), ("tennis", r"atp|wta|open|masters|vs\. [A-Z][a-z]+ [A-Z]"),
         ("mlb", r"yankees|dodgers|mets|red sox|cubs|braves|astros|phillies|padres|mariners|blue jays|orioles|guardians|brewers|twins|rangers|tigers|royals|rays|giants|cardinals|reds|pirates|marlins|nationals|rockies|angels|athletics|white sox|diamondbacks"),
         ("nfl", r"chiefs|eagles|ravens|jaguars|bills|cowboys|packers|49ers|steelers|bengals|dolphins|jets|patriots|lions|vikings|bears|saints|falcons|buccaneers|panthers|seahawks|rams|cardinals|broncos|raiders|chargers|texans|colts|titans|browns|commanders|giants"),
         ("college", r"\b(state|university|tech|college)\b")]
sport = lambda t: next((s for s, rx in SPORT if re.search(rx, t or "", re.I)), "other")
mk = sorted({b["m"] for b in bets})
with ThreadPoolExecutor(6) as ex:
    F = dict(zip(mk, ex.map(fills, mk)))


def px_at(fts, p0, oi, t, before=False):
    j = bisect.bisect_right(fts, t) - 1 if before else bisect.bisect_left(fts, t)
    if j < 0 or j >= len(fts):
        return None
    return p0[j] if oi == 0 else 1 - p0[j]


rows = []
for b in bets:
    fts, p0 = F[b["m"]]
    pre = {d: px_at(fts, p0, b["oi"], b["ts"] - d, before=True) for d in (300, 60, 10, 1)}
    post = {d: px_at(fts, p0, b["oi"], b["ts"] + d) for d in (5, 60)}
    if None in pre.values() or None in post.values():
        continue
    rows.append(dict(b, pre=pre, post=post, sport=sport((q.get(b["m"]) or {}).get("question")),
                     trades60=bisect.bisect_left(fts, b["ts"]) - bisect.bisect_left(fts, b["ts"] - 60)))
print(f"in-play bets with a full tape: {len(rows)} of {len(bets)}\n")
print("sport mix:", Counter(r["sport"] for r in rows).most_common())
print("wallets:", Counter(r["w"][:10] for r in rows).most_common(8))
print(f"their price: median {median(r['p'] for r in rows):.2f}; bands", Counter(f"{int(r['p'] * 10) / 10:.1f}" for r in rows).most_common())
print(f"bet size median ${median(r['usdc'] for r in rows):,.0f}; fills in the minute before median {median(r['trades60'] for r in rows)}")
mv = lambda r, d: r["p"] - r["pre"][d]
for d in (300, 60, 10):
    up = sum(mv(r, d) > 0.02 for r in rows); dn = sum(mv(r, d) < -0.02 for r in rows)
    print(f"their side's price over the {d}s before: rose >2pt {up}, fell >2pt {dn}, flat {len(rows) - up - dn}; median move {median(mv(r, d) for r in rows):+.3f}")
print(f"paid vs last fill before: median {median(r['p'] - r['pre'][1] for r in rows):+.3f}")
print(f"move after: +5s median {median(r['post'][5] - r['p'] for r in rows):+.3f}, +60s median {median(r['post'][60] - r['p'] for r in rows):+.3f}")
fee = lambda p: 0.07 * (1 - p)
print("\nroi at +5s by what the price did in the 60s before (their side):")
for lab, f in (("rose > 5pt (chasing a move)", lambda r: mv(r, 60) > 0.05), ("rose 2-5pt", lambda r: 0.02 < mv(r, 60) <= 0.05),
               ("flat", lambda r: abs(mv(r, 60)) <= 0.02), ("fell 2-5pt", lambda r: -0.05 <= mv(r, 60) < -0.02), ("fell > 5pt (buying the drop)", lambda r: mv(r, 60) < -0.05)):
    s = [r for r in rows if f(r)]
    if s:
        v = [(r["y"] - r["post"][5]) / r["post"][5] - fee(r["post"][5]) for r in s if 0.02 < r["post"][5] < 0.97]
        print(f"  {lab:<30} n {len(s):4d} win {sum(r['y'] for r in s) / len(s):.2f} px {sum(r['p'] for r in s) / len(s):.2f} roi@5s {sum(v) / max(len(v), 1):+.3f}")
print("\nby sport, roi at +5s:")
for sp, n in Counter(r["sport"] for r in rows).most_common():
    s = [r for r in rows if r["sport"] == sp]
    v = [(r["y"] - r["post"][5]) / r["post"][5] - fee(r["post"][5]) for r in s if 0.02 < r["post"][5] < 0.97]
    print(f"  {sp:<8} n {n:4d} win {sum(r['y'] for r in s) / n:.2f} px {sum(r['p'] for r in s) / n:.2f} roi@5s {sum(v) / max(len(v), 1):+.3f}")
print("\nsample titles:")
for r in rows[:12]:
    print(f"  {r['w'][:10]} {r['sport']:7} @{r['p']:.2f} pre60 {r['pre'][60]:.2f} +5s {r['post'][5]:.2f} {'W' if r['y'] else 'L'} {(q.get(r['m']) or {}).get('question', '')[:60]}")
