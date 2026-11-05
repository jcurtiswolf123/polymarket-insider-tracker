"""Learned trade scorer (local fork).

Every BUY the rules look at is logged with its features to ``~/.polywatch/features.jsonl``.
``scripts/learner.py`` labels each row once its market resolves (did the bought outcome win),
retrains a gradient-boosted classifier on all labelled rows, and promotes the new model only if
it beats the market price itself on a held-out, later slice of trades. The model predicts
P(win); ``edge = P(win) - price`` is the money the wallet is expected to make per dollar, which
is what an informed trader looks like from the outside.

The live pipeline and the learner both build vectors with ``vectorize`` so training and scoring
never drift apart.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HOME = Path(os.path.expanduser(os.environ.get("POLYWATCH_HOME", "~/.polywatch")))
FEATURES_PATH = HOME / "features.jsonl"
MODEL_PATH = HOME / "model.pkl"
META_PATH = HOME / "model_meta.json"

_SPORTS = re.compile(
    r"\bvs\.?\b|nba|nfl|nhl|mlb|ncaa|epl|premier league|la liga|serie a|bundesliga|ligue 1|"
    r"champions league|uefa|fifa|tennis|atp|wta|ufc|mma|boxing|golf|pga|f1|grand prix|"
    r"win on 20\d\d-\d\d-\d\d|spread|o/u|over/under|handicap|moneyline",
    re.I,
)
_ESPORTS = re.compile(
    r"lol:|league of legends|counter-strike|cs2|dota|valorant|esports|bo3|bo5", re.I
)
_POLITICS = re.compile(
    r"election|president|prime minister|senate|house|governor|mayor|parliament|minister|"
    r"trump|biden|congress|referendum|ballot|primary|cabinet|impeach|nominee|party",
    re.I,
)
_CRYPTO = re.compile(r"bitcoin|btc|ethereum|eth\b|solana|crypto|xrp|doge|up or down", re.I)
_MACRO = re.compile(r"fed\b|interest rate|cpi|inflation|gdp|jobs report|unemployment|tariff", re.I)
_CORPORATE = re.compile(
    r"earnings|ipo|acquire|merger|ceo|launch|release|announce|openai|apple", re.I
)

RULE_NAMES = (
    "R1_fresh_big_bet",
    "R2_young_thin_wallet",
    "R3_longshot_conviction",
    "R4_single_purpose_wallet",
    "R5_whale_thin_history",
    "R6_whale_longshot",
    "R7_proven_longshot_hitter",
    "R8_event_sweep",
    "C1_fresh_cluster",
)

FEATURE_NAMES = (
    "price",
    "log_usdc",
    "log_age_h",
    "log_prior_markets",
    "log_prior_trades",
    "trades_per_hour",
    "market_share",
    "log_market_cum",
    "longshot_markets",
    "longshot_win_rate",
    "event_markets",
    "event_share",
    "in_play",
    "hours_to_start",
    "hours_to_end",
    "log_market_volume",
    "niche",
    "size_conf",
    "fresh_conf",
    "watch",
    "cat_sports",
    "cat_esports",
    "cat_politics",
    "cat_crypto",
    "cat_macro",
    "cat_corporate",
    *(f"rule_{r}" for r in RULE_NAMES),
)


def _f(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = row.get(key)
    if v is None or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _log1p(x: float) -> float:
    return math.log1p(max(x, 0.0))


def category(text: str) -> dict[str, float]:
    return {
        "cat_sports": float(bool(_SPORTS.search(text))) if not _ESPORTS.search(text) else 0.0,
        "cat_esports": float(bool(_ESPORTS.search(text))),
        "cat_politics": float(bool(_POLITICS.search(text))),
        "cat_crypto": float(bool(_CRYPTO.search(text))),
        "cat_macro": float(bool(_MACRO.search(text))),
        "cat_corporate": float(bool(_CORPORATE.search(text))),
    }


def vectorize(row: dict[str, Any]) -> list[float]:
    """One logged row to the model's input vector, in FEATURE_NAMES order."""
    age = _f(row, "age_h")
    trades = _f(row, "prior_trades")
    ls_m = _f(row, "longshot_markets")
    text = " ".join(str(row.get(k) or "") for k in ("title", "slug", "outcome"))
    cats = category(text)
    rules = set(row.get("rules") or ())
    rules |= {r[2:] for r in rules if r.startswith("W_")}
    vals = {
        "price": _f(row, "price"),
        "log_usdc": _log1p(_f(row, "usdc")),
        "log_age_h": _log1p(age),
        "log_prior_markets": _log1p(_f(row, "prior_markets")),
        "log_prior_trades": _log1p(trades),
        "trades_per_hour": min(trades / max(age, 1.0), 500.0),
        "market_share": _f(row, "market_share", -1.0),
        "log_market_cum": _log1p(_f(row, "market_cum")),
        "longshot_markets": ls_m,
        "longshot_win_rate": (_f(row, "longshot_wins") / ls_m) if ls_m else -1.0,
        "event_markets": _f(row, "event_markets"),
        "event_share": _f(row, "event_share", -1.0),
        "in_play": _f(row, "in_play"),
        "hours_to_start": max(min(_f(row, "hours_to_start", -9999.0), 2000.0), -9999.0),
        "hours_to_end": max(min(_f(row, "hours_to_end", -9999.0), 9000.0), -9999.0),
        "log_market_volume": _log1p(_f(row, "market_volume")),
        "niche": _f(row, "niche"),
        "size_conf": _f(row, "size_conf"),
        "fresh_conf": _f(row, "fresh_conf"),
        "watch": _f(row, "watch"),
        **cats,
        **{f"rule_{r}": float(r in rules) for r in RULE_NAMES},
    }
    return [vals[n] for n in FEATURE_NAMES]


@dataclass
class Verdict:
    p_win: float
    edge: float
    promoted: bool
    version: str


class LearnedModel:
    """Loads the learner's latest model; reloads when the file changes. Never raises."""

    def __init__(self, model_path: Path = MODEL_PATH, meta_path: Path = META_PATH) -> None:
        self._model_path = model_path
        self._meta_path = meta_path
        self._mtime = 0.0
        self._model: Any = None
        self._meta: dict[str, Any] = {}

    def _refresh(self) -> None:
        try:
            mtime = self._model_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            with open(self._model_path, "rb") as fh:
                model = pickle.load(fh)  # our own file, written by scripts/learner.py
            meta = json.loads(self._meta_path.read_text())
        except Exception as e:  # a half-written file: keep the previous model
            logger.warning("learned model reload failed: %s", e)
            return
        self._model, self._meta, self._mtime = model, meta, mtime
        logger.info(
            "learned model loaded: version=%s promoted=%s n=%s",
            meta.get("version"),
            meta.get("promoted"),
            meta.get("n_train"),
        )

    @property
    def promoted(self) -> bool:
        self._refresh()
        return bool(self._model is not None and self._meta.get("promoted"))

    def score(self, row: dict[str, Any]) -> Verdict | None:
        self._refresh()
        if self._model is None:
            return None
        try:
            p = float(self._model.predict_proba([vectorize(row)])[0][1])
        except Exception as e:
            logger.warning("learned model score failed: %s", e)
            return None
        return Verdict(
            p_win=p,
            edge=p - _f(row, "price"),
            promoted=bool(self._meta.get("promoted")),
            version=str(self._meta.get("version") or ""),
        )


def append_features(row: dict[str, Any]) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):  # tests never write the live training log
        return
    try:
        FEATURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(FEATURES_PATH, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        logger.warning("features log write failed: %s", e)
