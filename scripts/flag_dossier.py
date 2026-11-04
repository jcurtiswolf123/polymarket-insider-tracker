"""Dossier for every wallet flagged in a window: positions, PnL, current price.

Sources: today's election_sweep_*.json (hits + fresh clusters), rule_hits.jsonl,
and high-notional rows from risk_assessments (via `polywatch sql`).
Run: uv run python scripts/flag_dossier.py [--hours 24] [--min-usdc 20000] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

HOME = Path.home() / ".polywatch"
POS = "https://data-api.polymarket.com/v2/positions"
TRADES = "https://data-api.polymarket.com/v2/trades"


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "polywatch-dossier"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def collect(hours: float, min_usdc: float) -> dict[str, set[str]]:
    """wallet -> set of reasons it was flagged."""
    cut = datetime.now(UTC) - timedelta(hours=hours)
    out: dict[str, set[str]] = {}

    def add(w: str, why: str) -> None:
        out.setdefault(w.lower(), set()).add(why)

    for f in HOME.glob("election_sweep_*.json"):
        if datetime.fromtimestamp(f.stat().st_mtime, UTC) < cut:
            continue
        d = json.loads(f.read_text())
        for h in d.get("hits", []):
            add(h["wallet"], f"sweep:{','.join(h.get('rules', [])) or 'hit'}")
        for c in d.get("C1_fresh_clusters", []):
            for m in c.get("members", []):
                add(m["wallet"] if isinstance(m, dict) else m, f"cluster:{c.get('q', '')[:40]}")
    rh = HOME / "rule_hits.jsonl"
    if rh.exists():
        for line in rh.read_text().splitlines():
            r = json.loads(line)
            if datetime.fromisoformat(r["ts"]) >= cut:
                add(r["wallet"], "live:" + ",".join(r["rules"]))
    q = (
        "select distinct wallet_address from risk_assessments where side='BUY' and "
        f"notional_usdc >= {min_usdc} and weighted_score >= 0.8 and "
        f"trade_timestamp > now() - interval '{hours} hours'"
    )
    try:
        res = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "polymarket-postgres",
                "psql",
                "-U",
                "tracker",
                "-d",
                "polymarket_tracker",
                "-tAc",
                q,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        for w in res.stdout.split():
            add(w, f"scored>={int(min_usdc / 1000)}k")
    except Exception as e:  # DB down must not kill the dossier
        print("db skipped:", e)
    return out


def dossier(wallet: str) -> dict:
    pos = get(f"{POS}?user={wallet}&limit=100").get("data", [])
    trades = get(f"{TRADES}?user={wallet}&limit=500").get("data", [])
    first = min((t["timestamp"] for t in trades), default=None)
    open_pos = sorted(
        (p for p in pos if p.get("status") == "OPEN"),
        key=lambda p: -(p.get("current_value") or 0),
    )
    return {
        "wallet": wallet,
        "first_trade_age_d": round((time.time() - first) / 86400, 1) if first else None,
        "trades_seen": len(trades),
        "markets": len({p["condition_id"] for p in pos}),
        "total_cost": round(sum(p.get("total_cost_usdc") or 0 for p in pos)),
        "total_pnl": round(sum(p.get("total_pnl") or 0 for p in pos)),
        "open": [
            {
                "title": p["title"],
                "outcome": p["outcome"],
                "cond": p["condition_id"],
                "token": p["token_id"],
                "avg": p["avg_price"],
                "now": p["current_price"],
                "cost": round(p.get("total_cost_usdc") or 0),
                "value": round(p.get("current_value") or 0),
                "pnl": round(p.get("total_pnl") or 0),
                "end": p.get("end_date"),
            }
            for p in open_pos[:5]
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--min-usdc", type=float, default=20000)
    ap.add_argument("--json")
    a = ap.parse_args()
    flagged = collect(a.hours, a.min_usdc)
    rows = []
    for w, why in flagged.items():
        try:
            d = dossier(w)
        except Exception as e:
            print(w, "error", e)
            continue
        d["why"] = sorted(why)
        rows.append(d)
    rows.sort(key=lambda d: -sum(p["cost"] for p in d["open"]))
    for d in rows:
        print(
            f"\n{d['wallet']} age {d['first_trade_age_d']}d trades {d['trades_seen']} "
            f"mkts {d['markets']} cost ${d['total_cost']:,} pnl ${d['total_pnl']:,} | {'; '.join(d['why'])[:120]}"
        )
        for p in d["open"][:3]:
            print(
                f"   {p['outcome']} {p['avg']:.3f}->{p['now']:.3f} ${p['cost']:,} pnl ${p['pnl']:,} end {p['end']} | {p['title'][:70]}"
            )
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
