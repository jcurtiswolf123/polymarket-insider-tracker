"""Who saw each goal first? Reads ~/.polywatch/race/*.jsonl from live_race.py.
  uv run python -I scripts/edge/race_report.py
Goal = ESPN or GOAL API score change where total goals rise by exactly 1 (drops VAR reversals and catch-up jumps).
ESPN and GOAL API reports of the same goal (shared team-name word, within 180s) merge into one goal.
For each, the first Polymarket quote/trade and first Kalshi trade in that match whose price moved >= 8pt vs 30s
earlier, within -60..+120s of the goal. All times are local receive time (rt): what we could act on.
"""
import bisect, glob, json, re
from collections import defaultdict
from pathlib import Path
from statistics import median


def one_goal(prev, score):
    """True when the total rises by exactly one; anything else is a correction or a catch-up."""
    try:
        return sum(map(int, score)) - sum(map(int, prev)) == 1
    except (TypeError, ValueError):
        return False


assert one_goal(["1", "0"], ["1", "1"]) and not one_goal(["1", "1"], ["2", "2"]) and not one_goal(["1", "0"], ["0", "0"])
assert not one_goal([None, None], ["1", "0"])

words = lambda s: set(re.findall(r"[a-z]{4,}", (s or "").lower())) - {"will", "game", "match", "draw", "more", "markets", "women"}


def onsets(series):
    series.sort(); ts = [x[0] for x in series]; out = []
    for i, (t, p, lab) in enumerate(series):
        j = bisect.bisect_right(ts, t - 30) - 1
        if j >= 0 and abs(p - series[j][1]) >= 0.08:
            out.append((t, lab))
    return out


def main():
    rows = [json.loads(l) for f in sorted(glob.glob(str(Path.home() / ".polywatch/race/*.jsonl"))) for l in open(f)]
    pm = defaultdict(list); ka = defaultdict(list)
    for r in rows:
        if r["src"] == "pm_trade":
            pm[(r["cid"], r["oi"])].append((r["rt"], r["p"], r["q"] + " " + r["slug"]))
        elif r["src"] == "pm_quote" and r["bid"] > 0 and r["ask"] > 0 and r["ask"] - r["bid"] < 0.1:
            pm[(r["cid"], r["oi"])].append((r["rt"], (r["bid"] + r["ask"]) / 2, r["q"] + " " + r["slug"]))
        elif r["src"] == "kalshi":
            ka[r["ticker"]].append((r["rt"], r["p"], r["ticker"]))
    PM = [o for s in pm.values() for o in onsets(s)]; KA = [o for s in ka.values() for o in onsets(s)]

    goals = []; counted = set()  # {name, w, espn, goalapi}; counted = (src, name, score) already a goal
    for r in sorted((r for r in rows if r["src"] in ("espn", "goalapi") and one_goal(r["prev"], r["score"])), key=lambda r: r["rt"]):
        key = (r["src"], r["name"], tuple(r["score"]))
        if key in counted:  # same score again after a VAR flip-flop: one goal, not two
            continue
        counted.add(key); w = words(r["name"])
        g = next((g for g in goals if g["w"] & w and abs(g["t0"] - r["rt"]) < 180 and g[r["src"]] is None), None)
        if g is None:
            g = {"name": r["name"], "w": w, "t0": r["rt"], "espn": None, "goalapi": None}; goals.append(g)
        g[r["src"]] = r["rt"]; g["t0"] = min(g["t0"], r["rt"])
    dropped = sum(1 for r in rows if r["src"] in ("espn", "goalapi") and not one_goal(r["prev"], r["score"]))
    print(f"goals: {len(goals)} (dropped {dropped} corrections/catch-ups)")

    fmt = lambda v, base: "-" if v is None or base is None else f"{v - base:+.1f}s"
    lead = []; kal = []
    for g in goals:
        near = lambda src: min((t for t, lab in src if g["t0"] - 60 <= t <= g["t0"] + 120 and g["w"] & words(lab)), default=None)
        p, k = near(PM), near(KA)
        base = g["goalapi"]
        print(f"{g['name'][:38]:38} goalapi 0s | espn {fmt(g['espn'], base)} | polymarket {fmt(p, base)} | kalshi {fmt(k, base)}")
        if base is not None and p is not None:
            lead.append(p - base)
        if base is not None and k is not None:
            kal.append(k - base)
    for lab, v in (("polymarket", lead), ("kalshi", kal)):
        if v:
            print(f"{lab} moved a median {median(v):+.1f}s after GOAL API ({len(v)} goals); GOAL API first by 1s+ on {sum(x >= 1 for x in v)}")
    if not lead:
        print("no goal yet with both a GOAL API report and a Polymarket move")


if __name__ == "__main__":
    main()
