"""Entry-placement test: how much do pending orders gain or lose by sitting closer to price?

Signals are generated once per H1 bar with the live (adaptive) rules, then the same signals are
replayed with different entry placements, so the comparison isolates the entry and nothing else:

  at_level      — current rule: the limit sits exactly at the structure level / order block
  front_0.15atr — the limit sits 0.15 × ATR in front of the level (closer to current price)
  front_0.25atr — 0.25 × ATR in front
  front_0.40atr — 0.40 × ATR in front
  split         — half at market on the next bar, half as the at-level limit

Stops and targets stay where the structure puts them; an order whose new entry would leave less
than the minimum risk/reward is not placed (as the live engine would refuse it). Market and stop
orders are identical in every variant. Same limits as the main backtest (H1 only, no news filter,
fixed costs, stop assumed first when a bar touches both).
"""
from __future__ import annotations

import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .backtest import DEFAULT_COSTS, LOOKBACK, _slice_closed, fetch_history, spread_pips_for, summarise
from .fundamentals import PairFundamentals
from .history import atomic_write_json
from .instruments import instrument
from .models import Candles
from .signals import analyse_pair

VARIANTS = {"at_level": 0.0, "front_0.15atr": 0.15, "front_0.25atr": 0.25, "front_0.40atr": 0.40, "split": None}
MAX_HOLD = 120  # H1 bars


def pair_signals(pair: str, h1: Candles, h4: Candles, d1: Optional[Candles], cfg: dict) -> list[dict]:
    """Every bar's signal (the expensive part, done once for all variants)."""
    out = []
    nofund = PairFundamentals(available=False)
    for i in range(max(cfg["analysis"]["min_bars"], 205), len(h1) - 1):
        now_ts = h1.t[i] + 3600
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        tfs = {"H1": h1.window(i + 1, LOOKBACK), "H4": _slice_closed(h4, now_ts), "D1": _slice_closed(d1, now_ts) if d1 else None}
        res = analyse_pair(pair, tfs, None, nofund, cfg, now, True)
        if res.get("direction") in ("BUY", "SELL") and res["confidence_score"] >= cfg["signals"]["min_confidence"]:
            out.append({"i": i, "time": now.isoformat(), "direction": res["direction"], "type": res["signal_type"],
                        "entry": res["entry_price"], "sl": res["stop_loss"], "tp": res["take_profit_2"],
                        "tp1": res["take_profit_1"], "atr": res.get("atr") or 0.0, "setup": res.get("market_structure")})
    return out


def _exit(h1: Candles, buy: bool, fill_i: int, fill: float, sl: float, tp: float, slip: float) -> tuple[float, int, str]:
    for j in range(fill_i, min(len(h1), fill_i + MAX_HOLD)):
        if (h1.l[j] <= sl) if buy else (h1.h[j] >= sl):
            return (sl - slip if buy else sl + slip), j, "STOP"
        if (h1.h[j] >= tp) if buy else (h1.l[j] <= tp):
            return tp, j, "TARGET"
    j = min(len(h1), fill_i + MAX_HOLD) - 1
    return h1.c[j], j, "OPEN_AT_END" if j == len(h1) - 1 else "TIME_EXIT"


def _leg(h1, buy, fill_i, fill, sl, tp, slip) -> tuple[float, int, str]:
    px, j, outcome = _exit(h1, buy, fill_i, fill, sl, tp, slip)
    risk = abs(fill - sl)
    return (((px - fill) if buy else (fill - px)) / risk if risk else 0.0), j, outcome


def simulate(pair: str, h1: Candles, sigs: list[dict], variant: str, cfg: dict, costs: dict) -> dict:
    inst = instrument(pair)
    spread = spread_pips_for(inst, costs)
    cost = (spread + costs["slippage_pips"]) * inst.pip_size
    slip = costs["slippage_pips"] * inst.pip_size
    min_rr = cfg["signals"]["min_risk_reward"]
    trades, missed, missed_winners, refused = [], 0, 0, 0
    busy_until = -1
    for s in sigs:
        i = s["i"]
        if i <= busy_until:
            continue
        buy = s["direction"] == "BUY"
        sgn = 1 if buy else -1
        expiry = max(1, cfg["signals"]["expiry_minutes"].get(s["type"], 60) // 60)
        legs = []  # (weight, R, exit bar, outcome)
        if s["type"] == "MARKET_ENTRY":
            fill = h1.o[i + 1] + sgn * cost
            r, j, o = _leg(h1, buy, i + 1, fill, s["sl"], s["tp"], slip)
            legs.append((1.0, r, j, o))
        elif "LIMIT" not in s["type"]:  # stop orders: unchanged in every variant
            fi = next((j for j in range(i + 1, min(len(h1), i + 1 + expiry))
                       if (h1.h[j] >= s["entry"] if buy else h1.l[j] <= s["entry"])), None)
            if fi is None:
                continue
            r, j, o = _leg(h1, buy, fi, s["entry"] + sgn * cost, s["sl"], s["tp"], slip)
            legs.append((1.0, r, j, o))
        else:
            off = VARIANTS[variant] or 0.0
            entry = s["entry"] + sgn * off * s["atr"]  # toward the current price
            if abs(s["tp"] - entry) < min_rr * abs(entry - s["sl"]):
                refused += 1
                continue
            weight = 1.0
            if variant == "split":
                mfill = h1.o[i + 1] + sgn * cost
                if (s["tp"] - mfill) * sgn >= min_rr * (mfill - s["sl"]) * sgn > 0:
                    r, j, o = _leg(h1, buy, i + 1, mfill, s["sl"], s["tp"], slip)
                    legs.append((0.5, r, j, o))
                    weight = 0.5
            fi = next((j for j in range(i + 1, min(len(h1), i + 1 + expiry))
                       if (h1.l[j] <= entry if buy else h1.h[j] >= entry)), None)
            if fi is not None:
                r, j, o = _leg(h1, buy, fi, entry + sgn * cost, s["sl"], s["tp"], slip)
                legs.append((weight, r, j, o))
            elif not legs:
                missed += 1
                # would the at-level order have won if it had been taken at the signal price instead?
                px, _, o = _exit(h1, buy, i + 1, h1.o[i + 1], s["sl"], s["tp"], slip)
                missed_winners += o == "TARGET"
                continue
        r = sum(w * x for w, x, _, _ in legs)
        end = max(j for _, _, j, _ in legs)
        outcome = "OPEN_AT_END" if any(o == "OPEN_AT_END" for *_, o in legs) else (
            "TARGET" if r > 0 else "STOP")
        trades.append({"pair": pair, "time": s["time"], "type": s["type"], "setup": s["setup"], "r": round(r, 3), "outcome": outcome})
        busy_until = end
    return {"trades": trades, "missed": missed, "missed_would_have_won": missed_winners, "refused_rr": refused}


def _signals_job(job):
    pair, h1, h4, d1, cfg = job
    return pair, pair_signals(pair, h1, h4, d1, cfg)


async def load_or_fetch(cfg: dict, pairs: list[str], targets: dict, refresh: bool = False) -> dict:
    cache = Path(cfg["paths"]["state_dir"]) / "backtests" / "history_cache.pkl"
    data = pickle.loads(cache.read_bytes()) if cache.exists() and not refresh else {}
    missing = [p for p in pairs if p not in data]
    if not missing:
        return data
    _, candles = await fetch_history(cfg, missing, targets)  # only the new instruments; the rest keep their period
    data.update(candles)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(data))
    return data


async def run_entry_test(cfg: dict, pairs: list[str], targets: dict, refresh: bool = False) -> dict:
    costs = {**DEFAULT_COSTS, **(cfg.get("backtest", {}) or {})}
    candles = await load_or_fetch(cfg, pairs, targets, refresh)
    jobs = [(p, candles[p]["H1"], candles[p]["H4"], candles[p].get("D1"), cfg)
            for p in pairs if candles.get(p, {}).get("H1") and candles[p].get("H4")]
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as ex:
        sigs = dict(ex.map(_signals_job, jobs))
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "costs": costs,
              "period": {p: [datetime.fromtimestamp(candles[p]["H1"].t[0], timezone.utc).date().isoformat(),
                             datetime.fromtimestamp(candles[p]["H1"].t[-1], timezone.utc).date().isoformat()] for p in sigs},
              "signals": sum(len(v) for v in sigs.values()), "variants": {}}
    for v in VARIANTS:
        allt, missed, mw, refused, per_pair = [], 0, 0, 0, {}
        for p, ss in sigs.items():
            res = simulate(p, candles[p]["H1"], ss, v, cfg, costs)
            allt += res["trades"]
            missed += res["missed"]
            mw += res["missed_would_have_won"]
            refused += res["refused_rr"]
            per_pair[p] = summarise(res["trades"])
        limits = [t for t in allt if "LIMIT" in t["type"]]
        result["variants"][v] = {**summarise(allt), "limit_orders": summarise(limits), "missed_no_fill": missed,
                                 "missed_that_went_to_target": mw, "refused_rr": refused, "per_pair": per_pair}
    out = Path(cfg["paths"]["state_dir"]) / "backtests" / f"entry_test_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    atomic_write_json(out, result)
    result["_path"] = str(out)
    return result
