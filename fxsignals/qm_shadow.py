"""QMR (Quasimodo reversal) in shadow mode: patterns are recorded and tracked on live H1 prices but never
sent, so the lab result (it lost after costs on ~10 months of history) can be checked on new data.

Each scan records the Quasimodos whose structure break closed in the last few H1 bars, then replays every
recorded pattern on the bars since, with the lab's rules and costs (limit at the QM level for 12 bars,
stop beyond the head, break-even after TP1, five days at most). Three exit sets are tracked: 2R/3R
(house rules), 1R/2R, and 1R/2R with half closed at 1R (the exits lab's best for QMR, +0.04R).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .backtest import DEFAULT_COSTS, summarise
from .history import atomic_write_json
from .lab import MAX_HOLD, _with_targets, qm_signals, replay
from .models import Candles

FILE = "qm_shadow.json"
TARGETS = {"2R/3R": ((2.0, 3.0), {}), "1R/2R": ((1.0, 2.0), {}), "1R/2R, half at 1R": ((1.0, 2.0), {"partial": 0.5})}
RULES = dict(hours=None, cooldown_h=0, breakeven=True, d1=None)  # hours are applied in the summary
RECENT_BARS = 6  # catch up on patterns missed while the Mac slept


def load(state_dir) -> list[dict]:
    p = Path(state_dir) / FILE
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return []


def update(state_dir, h1: dict[str, Candles], h4: dict[str, Candles], cfg: dict, now: datetime) -> list[dict]:
    """Record new patterns and settle the open ones. ``h1``/``h4`` hold closed bars only."""
    records = load(state_dir)
    seen = {r["key"] for r in records}
    costs = {**DEFAULT_COSTS, **(cfg.get("backtest") or {})}
    for pair, c in h1.items():
        if pair not in h4 or len(c) < 100:
            continue
        for s in qm_signals(pair, c, h4[pair], cfg, start=max(100, len(c) - RECENT_BARS), end=len(c)):
            key = f"{pair}|{s['direction']}|{c.t[s['i']]}"
            if key in seen:
                continue
            seen.add(key)
            records.append({"key": key, "symbol": pair, "bar_t": c.t[s["i"]], "recorded_at": now.isoformat(),
                            **{k: s[k] for k in ("t", "hour", "direction", "type", "setup", "entry", "sl", "risk",
                                                 "valid_bars", "h4_trend", "risk_atr", "score")},
                            "results": {}})
    for r in records:
        c = h1.get(r["symbol"])
        if c is None or r.get("done"):
            continue
        try:
            i = c.t.index(r["bar_t"])
        except ValueError:
            continue  # rolled out of the loaded bars
        sig = {**r, "i": i, "score": None, "adx": None, "ext_atr": None, "d1_agree": None}
        for name, (tp, extra) in TARGETS.items():
            trades = replay(r["symbol"], c, _with_targets([sig], tp), {**RULES, **extra}, costs)
            if trades and trades[0]["outcome"] != "OPEN_AT_END":
                r["results"][name] = {"outcome": trades[0]["outcome"], "r": trades[0]["r"], "exit_t": trades[0]["exit_t"]}
            elif not trades and len(c) > i + 1 + r["valid_bars"]:
                r["results"][name] = {"outcome": "NOT_FILLED", "r": None}
            elif trades:
                r["results"][name] = {"outcome": "OPEN", "r": None}
        r["done"] = all(r["results"].get(n, {}).get("outcome") not in (None, "OPEN") for n in TARGETS) or \
            len(c) > i + 1 + r["valid_bars"] + MAX_HOLD
    atomic_write_json(Path(state_dir) / FILE, records)
    return records


def summary(records: list[dict], hours=(7, 16)) -> dict:
    """Closed shadow trades per target set, house trading hours, one trade per pair at a time."""
    out = {}
    for name in TARGETS:
        trades, busy = [], {}
        for r in sorted(records, key=lambda r: r["t"]):
            res = r["results"].get(name) or {}
            if res.get("r") is None or (hours and not hours[0] <= r["hour"] < hours[1]):
                continue
            if r["t"] < busy.get(r["symbol"], 0):
                continue
            busy[r["symbol"]] = res["exit_t"]
            trades.append({"outcome": res["outcome"], "r": res["r"]})
        out[name] = summarise(trades)
    out["patterns_recorded"] = len(records)
    out["since"] = min((r["recorded_at"] for r in records), default=None)
    return out
