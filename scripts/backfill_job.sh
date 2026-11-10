#!/bin/zsh
# launchd com.josh.polywatch-backfill: resumes the history backfill after any reboot or crash,
# then runs eval and texts the result once. Exits 0 only when done (KeepAlive restarts otherwise).
export _ZO_DOCTOR=0
cd ~/Projects/forked-tools/polymarket-insider-tracker || exit 1
H=~/.polywatch/history; L=$H/run.log
[ -f $H/DONE ] && exit 0
echo "job start $(date)" >> $L
for s in markets trades features; do  # not "run": it ends with eval, which runs again below
  caffeinate -i uv run --env-file .env python scripts/history_backfill.py $s --since 2026-08-01 --min-volume 10000 >> $L 2>&1 || exit 1
done
uv run --env-file .env python scripts/history_backfill.py eval > $H/eval.txt 2>&1 || exit 1
uv run --env-file .env python - <<'PY' || exit 1
import sys; sys.path.insert(0, "scripts"); import sms_alerts
from pathlib import Path
t = Path.home().joinpath(".polywatch/history/eval.txt").read_text().splitlines()
i = next((n for n, l in enumerate(t) if l.startswith("-- test set")), 0)
keep = [l.strip()[:90] for l in t[i + 1:] if any(k in l for k in ("fresh", "size", "cluster", "proven", "any insider", "all "))][:8]
print(sms_alerts.send_sms("Polywatch backfill eval done\n" + "\n".join("- " + k for k in keep) + "\n- full: ~/.polywatch/history/eval.txt", False))
PY
date > $H/DONE
