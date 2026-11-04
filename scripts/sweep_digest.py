"""Telegram digest of an election sweep: only flags not seen in earlier digests.

uv run --env-file .env python scripts/sweep_digest.py sweep.json
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SEEN = Path("~/.polywatch/sweep_seen.json").expanduser()


def main() -> None:
    d = json.loads(Path(sys.argv[1]).read_text())
    seen: set[str] = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    lines: list[str] = []
    for h in d["hits"]:
        if not h["rules"] and "P1_priced_in" not in h.get("signals", []):
            continue
        key = f"{h['wallet']}|{h['market']}|{h['outcome']}"
        if key in seen:
            continue
        seen.add(key)
        tags = ",".join(h["rules"] + h.get("signals", []))
        lines.append(
            f"- ${h['usdc_window']:,} {h['outcome']} @{h['avg_px']} now {h['now_px']} | "
            f"{h['q'][:55]} | {tags} | age {h.get('age_d')}d | {h['wallet'][:10]}"
        )
    for c in d["C1_fresh_clusters"]:
        key = f"C1|{c['q']}|{c['outcome']}|{c['wallets'] // 5}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"- cluster {c['wallets']} fresh wallets ${c['usdc']:,} {c['outcome']} "
            f"@{c.get('avg_px')} | {c['q'][:55]} | pnl ${c.get('est_pnl', 0):,}"
        )
    SEEN.write_text(json.dumps(sorted(seen)))
    head = f"Election sweep {d['window_h']}h: {d['buys']} buys, {len(lines)} new flags"
    print(head)
    print("\n".join(lines[:40]))
    if not lines:
        return
    text = head + "\n" + "\n".join(lines[:25])
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)


if __name__ == "__main__":
    main()
