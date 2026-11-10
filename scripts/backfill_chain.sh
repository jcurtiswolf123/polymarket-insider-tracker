#!/bin/zsh
# After trades finish: pause backfill, repair errored markets, resume (features), then eval and text the result.
export _ZO_DOCTOR=0
cd ~/Projects/forked-tools/polymarket-insider-tracker
H=~/.polywatch/history; L=$H/run.log
until grep -q "^features: wallets" $L; do sleep 5; done
pkill -f "history_backfill.py run"; sleep 5
uv run --env-file .env python scripts/history_backfill.py repair >> $L 2>&1
caffeinate -i uv run --env-file .env python scripts/history_backfill.py run --since 2026-08-01 --min-volume 10000 >> $L 2>&1
uv run --env-file .env python scripts/history_backfill.py eval > $H/eval.txt 2>&1
uv run --env-file .env python - <<'PY'
import sys; sys.path.insert(0, "scripts"); import sms_alerts
from pathlib import Path
t = Path.home().joinpath(".polywatch/history/eval.txt").read_text().splitlines()
i = next((n for n, l in enumerate(t) if l.startswith("-- test set")), 0)
keep = [l.strip()[:90] for l in t[i + 1:] if any(k in l for k in ("fresh", "size", "cluster", "proven", "any insider", "all "))][:8]
print(sms_alerts.send_sms("Polywatch backfill eval done\n" + "\n".join("- " + k for k in keep) + "\n- full: ~/.polywatch/history/eval.txt", False))
PY
