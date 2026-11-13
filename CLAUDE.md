**Fork ops (branch josh/learned-scoring-sms).** Upstream rules: AGENTS.md. This checkout is live: launchd jobs run from it, so never switch branches here (a 2026-11-09 switch dropped fav_lane.py and broke a job for hours). Use `git worktree` for other branches.

**Every command**
- prefix `export _ZO_DOCTOR=0;`
- `uv run --env-file .env python scripts/<x>.py`
- DB: `docker exec -i polymarket-postgres psql -U tracker -d polymarket_tracker`
- CLI: `~/.claude/bin/polywatch help`

**Runs on Fly (since 2026-11-11):** app `polywatch-bot` (sjc, performance-1x 4GB: shared CPU starved jobs 11-11, volume /data, HOME=/data): tracker + Postgres + Redis + sms_alerts, fav_lane, kalshi_copy, fav_live, learner, alert_results. Mac plists moved to `~/Library/LaunchAgents/moved-to-fly-2026-11-11/`; never reload them while Fly runs (double bets). Deploy: `flyctl deploy . --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile --app polywatch-bot --ha=false`. Shell: `flyctl ssh console --app polywatch-bot`. Kill: `touch /data/.polywatch/LIVE_OFF` on Fly.

**Paper lanes (live only where marked; real money needs Joshua's explicit "live" + caps)**
- alert book: `scripts/alert_results.py` (every 30m), nightly text `--summary` at 21:00
- Kalshi mirror: `scripts/kalshi_copy.py report` (every 2m): matches alerts to Kalshi game winners, totals (Over), esports maps; feeds fav_live; no texts
- favorites: `scripts/fav_lane.py report` (every 1m), $200/bet; Sunday 09:00 text adds every live lane hit by week via `scripts/edge/sms_gate_replay.py` (forward test, call at 200 resolved)
- LIVE Kalshi alerts only (lane off 11-12: 1,575 sports favorites won at price; `LANE_BETS`): `scripts/fav_live.py tick|report` (Fly, 1m), $200/bet, one bet per game, stops at $1,000 worst-case loss, kill: `touch ~/.polywatch/LIVE_OFF`; run with `uv run --with cryptography`; tests `tests/test_fav_live.py`
- learner: `scripts/learner.py --report`
- research desk: `scripts/edge/research_desk.py report|grade|run -n 20` (06:15, claude -p, Sonnet)
- hormuz: `scripts/edge/hormuz.py status` (07:05)
- nowcast TSA/gas: `scripts/edge/nowcast.py scan` (08:30), backtest FAIL 11-09, re-test ~Feb
- killed 11-09 (backtest FAIL): `scripts/edge/weather.py`, `scripts/edge/touch.py`
- gate for any lane: ROI > +5% after Kalshi fee on 200+ out-of-sample bets
- status doc: `docs/edge-builds-2026-11-09.md`

**History backfill**
- `scripts/history_backfill.py status|repair|eval`, data `~/.polywatch/history/`, log `run.log`
- launchd `com.josh.polywatch-backfill` runs `scripts/backfill_job.sh`: resumes after reboot, eval + SMS, writes `history/DONE`
- launchd `com.josh.polywatch-watchdog` (10 min) runs `scripts/watchdog.sh`: texts on battery <=30% unplugged, stale scanner/watch/backfill, nonzero job exit; 3h dedupe

**Constraints**
- Polymarket main = data only (CA geoblock); trade venue Kalshi
- secrets in `~/.gstack/credentials/`, `.env`; never print them
- Kalshi public API rate-limits: retry 429/timeouts with backoff
