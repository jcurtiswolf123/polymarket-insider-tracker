"""Does Kalshi reprice soccer goals later than Polymarket? Match goal-like jumps across venues and time them.
  uv run python -I scripts/edge/venue_lag.py
Jump onset = first trade whose price differs >= TH from the last trade >= 30s earlier. Kalshi tapes from
~/.polywatch/kalshi_tapes (kalshi_goal_sync.py), Polymarket fills via skilled_decay.fills (cached).
"""
import bisect, json, re, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
sys.path.insert(0, str(Path(__file__).parent))
from skilled_decay import fills

H = Path.home() / ".polywatch"
TH = 0.08
LG = {"EPL": "epl", "LALIGA": "lal", "SERIEA": "sea", "BUNDESLIGA": "bun", "LIGUE1": "fl1", "UCL": "ucl", "MLS": "mls"}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def onsets(tape):
    """tape: [(ts, price)] sorted. Times where price jumps >= TH vs 30s earlier; one per 120s."""
    ts = [t for t, _ in tape]; out = []
    for i, (t, p) in enumerate(tape):
        j = bisect.bisect_right(ts, t - 30) - 1
        if j >= 0 and abs(p - tape[j][1]) >= TH and (not out or t - out[-1] > 120):
            out.append(t)
    return out


# Kalshi games grouped by event, onsets merged across the 3 outcome markets
kal = defaultdict(list)
for f in (H / "kalshi_tapes").glob("KX*GAME-*.json"):
    ev = f.stem.rsplit("-", 1)[0]
    kal[ev] += [t for t in onsets([(t, p) for t, p, _ in json.loads(f.read_text())])]
pm = defaultdict(list)  # (league, date) -> polymarket team-win/draw markets
for l in open(H / "history/markets.jsonl"):
    m = json.loads(l); e = (m.get("event") or "")
    mm = re.match(r"^(epl|lal|sea|bun|fl1|ucl|mls)-.*-(\d{4}-\d{2}-\d{2})$", e)
    if mm and re.search(r"win on 20\d\d|end in a draw", m.get("question") or ""):
        pm[(mm.group(1), mm.group(2))].append(m)
pairs = []
for ev, ko in kal.items():
    s, sfx = ev.split("-", 1)
    lg = LG.get(s[2:-4]); d = re.match(r"(\d\d)([A-Z]{3})(\d\d)", sfx)
    if not lg or not d or not ko:
        continue
    date = f"20{d.group(1)}-{MON[d.group(2)]:02d}-{d.group(3)}"
    pairs.append((ev, sorted(ko), pm.get((lg, date), [])))
need = {m["cid"] for _, _, ms in pairs for m in ms}
with ThreadPoolExecutor(6) as ex:
    T = dict(zip(sorted(need), ex.map(fills, sorted(need))))
lags = []
for ev, ko, ms in pairs:
    by_event = defaultdict(list)
    for m in ms:
        fts, p0 = T[m["cid"]]
        by_event[m["event"]] += onsets(list(zip(fts, p0)))
    # the Polymarket event whose jumps line up with this Kalshi game
    best, score = None, 0
    for e, po in by_event.items():
        po.sort(); n = sum(1 for k in ko if po and min(abs(k - p) for p in po) < 120)
        if n > score:
            best, score = e, n
    if not best or score < 1:
        continue
    po = sorted(by_event[best])
    for k in ko:
        p = min(po, key=lambda x: abs(k - x))
        if abs(k - p) < 120:
            lags.append((k - p, ev, best))
print(f"kalshi games {len(kal)}, paired with polymarket {len({x[1] for x in lags})}, matched goal jumps {len(lags)}")
if lags:
    L = sorted(x[0] for x in lags)
    print(f"kalshi minus polymarket onset, seconds: median {median(L):+.0f}, p25 {L[len(L)//4]:+.0f}, p75 {L[3*len(L)//4]:+.0f}")
    print(f"kalshi later by >= 5s: {sum(x >= 5 for x in L)} | within 5s: {sum(abs(x) < 5 for x in L)} | kalshi earlier by >= 5s: {sum(x <= -5 for x in L)}")
    for x in lags[:10]:
        print(f"   {x[0]:+5.0f}s  {x[1]}  {x[2]}")
