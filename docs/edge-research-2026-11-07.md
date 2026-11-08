**Edge research 2026-11-07**

**Favorites lane** (`scripts/fav_lane.py`)
- fresh wallet (<7d), insider rule, 0.60-0.85, resolves <=7d
- +13.5 pts over price, 51 markets, 90% CI +6 to +21, data 11-03 to 11-07
- paper book live; call it at ~100 resolved bets

**Entry delay** (`scripts/research/lane_speed.py`)
- edge at +0s +17.9, +30s +18.7, +2m +19.6, +10m +20.8, +30m +23.1 (42 markets)
- price drifts down after the insider buy; late entry costs nothing
- our detection lag: median 7s, p90 27s
- speed work: not needed

**Longshot fade**
- 0.20-0.40: -0.7 pts, CI -3.3 to +1.9 (740 markets)
- 0.03-0.20: +0.5 pts, CI -3.2 to +4.1 (211 markets)
- live spread ~1 cent; dead

**Kalshi single-venue arb** (`scripts/kalshi_arb.py`)
- old ladder check paired legs across teams: 43,793 false hits; fixed
- after fix: 0 ladders, 0 ALL_NO across 20,417 events
- 94 ALL_YES hits, all non-exhaustive lists; not risk-free

**Cross-venue game winners** (`scripts/research/xvenue_gaps.py`)
- 122 NFL/NBA/NHL/CFB games matched, 239 pairs
- 1 positive (+2 cents, game in play, likely stale); Polymarket US-blocked anyway
- dead
