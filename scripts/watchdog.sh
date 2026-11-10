#!/bin/zsh
# launchd com.josh.polywatch-watchdog (every 10 min): texts when the laptop is about to die
# or a polywatch job goes quiet. One text per issue per 3h (state in ~/.polywatch/watchdog_state).
export _ZO_DOCTOR=0
cd ~/Projects/forked-tools/polymarket-insider-tracker || exit 1
P=~/.polywatch; S=$P/watchdog_state; touch $S; now=$(date +%s); issues=()
age() { [ -e "$1" ] && echo $(( now - $(stat -f %m "$1") )) || echo 999999; }

b=$(pmset -g batt)
pct=$(echo "$b" | grep -o '[0-9]*%' | head -1 | tr -d %)
if echo "$b" | grep -q "Battery Power" && [ "${pct:-100}" -le 30 ]; then issues+=("battery:battery ${pct}%, plug in or polywatch dies"); fi
[ $(age $P/features.jsonl) -gt 1800 ] && issues+=("scanner:scanner quiet $(( $(age $P/features.jsonl) / 60 ))m")
[ $(age $P/watch.log) -gt 1200 ] && issues+=("watch:wallet watch quiet $(( $(age $P/watch.log) / 60 ))m")
[ ! -f $P/history/DONE ] && [ $(age $P/history/run.log) -gt 1800 ] && issues+=("backfill:backfill stalled $(( $(age $P/history/run.log) / 60 ))m")
for j in $(launchctl list | awk '$3 ~ /^com\.josh\.polywatch/ && $1 == "-" && $2 != 0 {print $3}'); do
  issues+=("$j:${j#com.josh.} last exit nonzero")
done

for it in $issues; do
  k=${it%%:*}; msg=${it#*:}
  last=$(awk -v k="$k" '$1==k{print $2}' $S | tail -1)
  [ -n "$last" ] && [ $(( now - last )) -lt 10800 ] && continue
  echo "$(date '+%F %T') $msg"
  uv run --env-file .env python -c "import sys; sys.path.insert(0,'scripts'); import sms_alerts; print(sms_alerts.send_sms('Polywatch watchdog: ' + sys.argv[1], bool(sys.argv[2])))" "$msg" "${WATCHDOG_DRY:-}"
  [ -z "$WATCHDOG_DRY" ] && echo "$k $now" >> $S
done
exit 0
