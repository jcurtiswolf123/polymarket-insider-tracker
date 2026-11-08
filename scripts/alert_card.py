"""Build the recommendation behind one SMS alert (local fork).

For a rule hit it works out:
  - win chance: the market price, moved by how past trades with the same rules at a similar
    price actually resolved (shrunk toward the price when that history is thin). Once a
    learned model is promoted, its P(win) is used instead.
  - bet size: quarter Kelly on that win chance against SMS_BANKROLL, capped at SMS_MAX_BET_PCT.
  - a details page (secret GitHub gist) with the market, the wallet, the track record and the
    unvalidated shadow model's read.

Used by scripts/sms_alerts.py. Never raises: a failed lookup leaves that field out.
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_insider_tracker.detector.learned import (  # noqa: E402
    FEATURES_PATH,
    HOME,
    META_PATH,
    LearnedModel,
    vectorize,
)

STATS_PATH = HOME / "rule_stats.json"
SHADOW_PATH = HOME / "model_shadow.pkl"
SHADOW_META_PATH = HOME / "model_shadow_meta.json"
GAMMA = "https://gamma-api.polymarket.com/markets"
PRIOR_GROUPS = 30.0  # history needs ~30 wallet+market groups before it moves the price halfway
MODEL_MAX_DAYS = 30.0  # model trained on markets that resolved within days; no read past this
RULE_LABEL = {
    "R1_fresh_big_bet": "fresh wallet, big bet",
    "R2_young_thin_wallet": "young wallet, thin history",
    "R3_longshot_conviction": "longshot conviction",
    "R4_single_purpose_wallet": "single-purpose wallet",
    "R5_whale_thin_history": "whale, thin history",
    "R6_whale_longshot": "whale on a longshot",
    "R7_proven_longshot_hitter": "proven longshot hitter",
    "R8_event_sweep": "sweeping one event",
    "C1_fresh_cluster": "cluster of fresh wallets",
    "M1_model_edge": "model edge",
}


def env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _rules(hit: dict) -> list[str]:
    return [r[2:] if r.startswith("W_") else r for r in hit.get("rules") or []]


def _bucket(price: float, edges: list[float]) -> int:
    return next((i for i, e in enumerate(edges) if price < e), len(edges))


def _feature_row(trade_id: str) -> dict | None:
    """The full logged feature row for this trade (newest first, last ~8MB only)."""
    if not trade_id or not FEATURES_PATH.exists():
        return None
    with open(FEATURES_PATH, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 8_000_000))
        lines = fh.read().splitlines()
    for line in reversed(lines):
        if trade_id.encode() in line:
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _market(cid: str) -> dict:
    try:
        req = urllib.request.Request(
            f"{GAMMA}?condition_ids={cid}", headers={"User-Agent": "polywatch"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return data[0] if isinstance(data, list) and data else {}
    except Exception:
        return {}


def track_record(rules: list[str], price: float) -> tuple[str, dict] | None:
    """Most specific history with enough groups: strongest rule at this price, then wider."""
    try:
        doc = json.loads(STATS_PATH.read_text())
    except (OSError, ValueError):
        return None
    stats, b = doc.get("stats", {}), _bucket(price, doc.get("price_edges") or [])
    cands = []
    for ru in rules:
        cands += [
            (f"{ru}|{b}", f"{RULE_LABEL.get(ru, ru)} at this price"),
            (ru, RULE_LABEL.get(ru, ru)),
        ]
    cands += [
        (f"flagged|{b}", "all flagged trades at this price"),
        ("flagged", "all flagged trades"),
    ]
    for key, label in cands:  # most specific first
        s = stats.get(key)
        if s and s["groups"] >= 15:
            return label, s
    return None


def shadow_p(row: dict | None) -> float | None:
    if not row or not SHADOW_PATH.exists():
        return None
    try:
        with open(SHADOW_PATH, "rb") as fh:
            model = pickle.load(fh)  # our own file, written by scripts/learner.py
        return float(model.predict_proba([vectorize(row)])[0][1])
    except Exception:
        return None


def model_edge_cap(meta: dict) -> float:
    """Largest edge the held-out test shows actually paying (ROI > 0 on 15+ groups), else 0."""
    cap = 0.0
    for k, b in meta.items():
        paid = isinstance(b, dict) and (b.get("roi") or 0) > 0 and (b.get("groups") or 0) >= 15
        if k.startswith("edge_ge_") and paid:
            cap = max(cap, float(k[len("edge_ge_") :]))
    return cap


def _days_left(m: dict) -> float | None:
    from datetime import datetime

    try:
        t = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
        return (t.timestamp() - time.time()) / 86400
    except ValueError:
        return None


def recommend(hit: dict) -> dict:
    price = float(hit.get("price") or 0)
    rules = _rules(hit)
    row = _feature_row(hit.get("trade_id") or "")
    rec: dict[str, Any] = {"price": price, "rules": rules, "row": row}
    m = _market(hit.get("market") or "")
    days = _days_left(m)
    live = LearnedModel()
    verdict = live.score(row) if row and live.promoted else None
    try:
        cap = model_edge_cap(json.loads(META_PATH.read_text()))
    except (OSError, ValueError):
        cap = 0.0
    if days is not None and days > MODEL_MAX_DAYS:
        verdict = None  # out of the model's training range
    if verdict and cap <= 0:
        verdict = None  # no model edge size has paid on held-out trades
    tr = track_record(rules, price)
    rec["track"] = tr
    if verdict:
        p = price + max(-cap, min(verdict.p_win - price, cap))
        basis = f"learned model {verdict.version} (validated)"
        if abs(verdict.p_win - price) > cap:
            basis += f", raw {verdict.p_win:.0%} capped at the +/-{cap:.0%} edge that paid"
    elif tr:
        label, s = tr
        excess = s["win_rate"] - s["avg_price"]
        p = price + excess * s["groups"] / (s["groups"] + PRIOR_GROUPS)
        basis = f"history of {label}"
    else:
        p, basis = price, "market price (no history yet)"
    p = min(max(p, 0.01), 0.99)
    rec.update({"p": p, "basis": basis, "shadow": shadow_p(row)})
    bankroll = env_f("SMS_BANKROLL", 1000)
    kelly = (p - price) / (1 - price) if price < 1 else 0.0
    frac = max(0.0, min(kelly * 0.25, env_f("SMS_MAX_BET_PCT", 0.05)))
    rec.update(
        {
            "edge": p - price,
            "bankroll": bankroll,
            "bet_pct": frac,
            "bet_usd": round(bankroll * frac),
            "max_price": round(price + (p - price) / 2, 2) if p > price else None,
        }
    )
    rec["market"] = m
    rec["starts_in_h"] = None
    gs = (m.get("gameStartTime") or "").strip()
    if gs:
        from datetime import datetime

        try:
            t = datetime.fromisoformat(gs.replace(" ", "T").replace("+00", "+00:00"))
            rec["starts_in_h"] = (t.timestamp() - time.time()) / 3600
        except ValueError:
            pass
    ev = (m.get("events") or [{}])[0].get("slug") if m else None
    rec["market_url"] = (
        f"https://polymarket.com/event/{ev}"
        if ev
        else f"https://polymarket.com/market/{hit.get('slug')}"
    )
    rec["wallet_url"] = f"https://polymarket.com/profile/{hit.get('wallet')}"
    return rec


def _now_price(m: dict, outcome: str) -> float | None:
    try:
        outs = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        return float(prices[outs.index(outcome)])
    except (ValueError, IndexError, TypeError):
        return None


def card_markdown(hit: dict, rec: dict) -> str:
    m, row = rec["market"], rec["row"] or {}
    outcome = hit.get("outcome") or "?"
    price = rec["price"]
    lines = [
        f"**Polywatch alert** {hit.get('seen') or hit.get('ts')}",
        "",
        f"**Market:** [{hit.get('title') or hit.get('slug')}]({rec['market_url']})",
        f"**Signal:** wallet bought {outcome} @ {price:.2f}, ${float(hit.get('usdc') or 0):,.0f}",
    ]
    now = _now_price(m, outcome)
    if now is not None:
        lines.append(f"**Price now:** {now:.2f}")
    if rec["starts_in_h"] is not None:
        h = rec["starts_in_h"]
        lines.append(f"**Starts in:** {h:.1f}h" if h > 0 else "**Status:** game already started")
    if m.get("endDate"):
        lines.append(f"**Resolves by:** {m['endDate'][:16].replace('T', ' ')} UTC")
    if m.get("volume"):
        lines.append(f"**Market volume:** ${float(m['volume']):,.0f}")
    lines += ["", "**Recommendation**"]
    if rec["bet_usd"] > 0:
        lines += [
            f"- Play: BUY {outcome}, ${rec['bet_usd']:,} "
            f"({rec['bet_pct']:.1%} of ${rec['bankroll']:,.0f} bankroll, quarter Kelly)",
            f"- Max price: {rec['max_price']:.2f} (above that, half the edge is gone)",
        ]
    else:
        lines.append("- Play: no bet. History of trades like this shows no edge over the price.")
    lines += [
        f"- Win chance: {rec['p']:.0%} vs market {price:.0%} (edge {rec['edge']:+.0%})",
        f"- Based on: {rec['basis']}",
    ]
    if rec["shadow"] is not None:
        lines.append(f"- Shadow model (not validated yet): {rec['shadow']:.0%}")
    if rec["track"]:
        label, s = rec["track"]
        lines += [
            "",
            f"**Track record: {label}**",
            f"- {s['groups']:.0f} resolved wallet+market bets",
            f"- won {s['win_rate']:.0%} at avg price {s['avg_price']:.2f}",
            f"- return per $1 staked: {s['roi']:+.0%}",
        ]
    rules = ", ".join(f"{r.split('_')[0]} {RULE_LABEL.get(r, r)}" for r in rec["rules"])
    age = float(hit.get("age_h") or 0)
    lines += [
        "",
        "**Wallet**",
        f"- [{hit.get('wallet')}]({rec['wallet_url']})",
        f"- age {age / 24:.1f}d, {hit.get('prior_markets')} prior markets, "
        f"{row.get('prior_trades', hit.get('prior_trades'))} prior trades",
        f"- rules: {rules or 'none'}",
    ]
    if row.get("longshot_markets"):
        lines.append(f"- longshot record: {row.get('longshot_wins')}/{row['longshot_markets']} won")
    if row.get("market_share") not in (None, -1):
        lines.append(f"- share of this market's volume: {float(row['market_share']):.1%}")
    lines += [
        "",
        "**Caveats**",
        "- Win chance comes from a few days of resolved trades. Treat it as a lean, not a lock.",
        "- Polymarket blocks US accounts; the copy trader runs on paper only.",
    ]
    return "\n".join(lines) + "\n"


def publish(md: str, name: str) -> str | None:
    """Secret gist (unlisted, only the link opens it). Returns its URL."""
    gh = shutil.which("gh") or "/Users/joshua.wolf/miniconda3/bin/gh"
    try:
        out = subprocess.run(
            [gh, "gist", "create", "--filename", f"{name}.md", "--desc", "Polywatch alert", "-"],
            input=md,
            capture_output=True,
            text=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    url = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""
    return url if url.startswith("https://") else None


def sms_text(hit: dict, rec: dict, link: str | None) -> str:
    outcome = hit.get("outcome") or "?"
    price = rec["price"]
    age = float(hit.get("age_h") or 0)
    age_s = f"{age:.0f}h" if age < 48 else f"{age / 24:.0f}d"
    rules = ", ".join(RULE_LABEL.get(r, r) for r in rec["rules"][:2])
    title = (hit.get("title") or hit.get("slug") or "")[:70]
    if rec["bet_usd"] > 0:
        play = f"Play: BUY {outcome} ${rec['bet_usd']:,} ({rec['bet_pct']:.1%}), max {rec['max_price']:.2f}"
    else:
        play = "Play: no bet, watch only"
    parts = [
        f"Polywatch: {title}",
        f"Wallet bought {outcome} @{price:.2f} ${float(hit.get('usdc') or 0):,.0f} ({age_s} old, {rules})",
        f"Win chance {rec['p']:.0%} vs mkt {price:.0%}",
        play,
    ]
    if rec["starts_in_h"] is not None and rec["starts_in_h"] > 0:
        parts.append(f"Starts in {rec['starts_in_h']:.1f}h")
    parts.append(link or rec["market_url"])
    return "\n".join(parts)


def build(hit: dict, publish_page: bool = True) -> tuple[str, str, str | None]:
    """(sms body, details markdown, details url)."""
    rec = recommend(hit)
    md = card_markdown(hit, rec)
    link = publish(md, f"polywatch-{time.strftime('%Y%m%d-%H%M%S')}") if publish_page else None
    return sms_text(hit, rec, link), md, link
