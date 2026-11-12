#!/usr/bin/env bash
# Boot: write creds from secrets, start Redis + Postgres on the volume, run the tracker, then the job loop.
set -uo pipefail
export HOME=/data
mkdir -p /data/.polywatch /data/.gstack/credentials /data/redis
umask 077
printf '%s\n' "$KALSHI_KEY_ID" > /data/.gstack/credentials/kalshi-key-id.txt
printf '%s\n' "$KALSHI_PRIVKEY" > /data/.gstack/credentials/kalshi-privkey.pem
printf '%s' "$TWILIO_SID" > /data/.gstack/credentials/twilio-account-sid.txt
printf '%s' "$TWILIO_TOKEN" > /data/.gstack/credentials/twilio-auth-token.txt
umask 022

redis-server --dir /data/redis --appendonly yes --daemonize yes --bind 127.0.0.1
PG=/usr/lib/postgresql/$(ls /usr/lib/postgresql | head -1)/bin
if [ ! -f /data/pg/PG_VERSION ]; then
  mkdir -p /data/pg && chown postgres /data/pg
  su postgres -c "$PG/initdb -D /data/pg -U postgres" >/dev/null
fi
chown -R postgres /data/pg; mkdir -p /run/postgresql && chown postgres /run/postgresql
su postgres -c "$PG/pg_ctl -D /data/pg -l /data/pg/server.log -o '-c listen_addresses=127.0.0.1' start -w"
su postgres -c "psql -tAc \"select 1 from pg_roles where rolname='$POSTGRES_USER'\"" | grep -q 1 ||
  su postgres -c "psql -c \"create role $POSTGRES_USER login password '$POSTGRES_PASSWORD'\" && createdb -O $POSTGRES_USER $POSTGRES_DB"

# First boot waits paused so Mac state can be copied in before anything runs (no double bets or texts).
[ -f /data/.polywatch/.initialized ] || touch /data/.polywatch/PAUSE /data/.polywatch/.initialized
while [ -f /data/.polywatch/PAUSE ]; do sleep 10; done

cd /app
alembic upgrade head || exit 1
( while true; do python -m polymarket_insider_tracker >> /data/.polywatch/tracker.log 2>&1; echo "$(date) tracker exited, restart" >> /data/.polywatch/tracker.log; sleep 10; done ) &

job() { timeout "$1" python "${@:2}" >> "/data/.polywatch/$(basename "$2" .py).log" 2>&1; }
n=0
while true; do
  job 50 scripts/sms_alerts.py
  job 50 scripts/fav_lane.py tick
  [ $((n % 2)) -eq 0 ] && job 100 scripts/kalshi_copy.py tick
  job 120 scripts/fav_live.py tick
  [ $((n % 30)) -eq 0 ] && { nice -n 15 bash -c "$(declare -f job); job 900 scripts/learner.py; job 600 scripts/alert_results.py" & }
  [ "$(date +%H:%M)" = "21:00" ] && job 300 scripts/alert_results.py --summary &
  # shortcut: tracker leaks ~1GB/h and got OOM-killed at 3.3GB; recycle it cleanly before that until the leak is found
  rss=$(ps -eo rss=,args= | awk '/-m polymarket_insider_tracker/ && !/awk/ {print $1; exit}')
  [ "${rss:-0}" -gt 2200000 ] && pkill -f -- "-m polymarket_insider_tracker" && echo "$(date) tracker rss ${rss}k, recycled" >> /data/.polywatch/tracker.log
  n=$((n + 1)); sleep 30
done
