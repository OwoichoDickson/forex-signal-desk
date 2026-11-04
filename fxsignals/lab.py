"""Strategy lab: which rules actually make money?

Every H1 bar of the cached history (about 10 months, 20 pairs) is analysed twice with the live signal
engine — once with the old entry rules and once with the current ones — and every signal is stored
with what it looked like (setup, hour, trend strength, distance from EMA20, daily trend). Rule sets
are then replayed as filters and trade-management choices on those same signals, so two rule sets
differ only in the rule being tested.

Walk-forward: the period is split in two halves. A rule only "helps" if it improves the average
result in both halves, and setups are cut only when they lost in the first half — the cut is then
judged on the second half it never saw.

Limits, stated in every result: H1/H4/D1 history only (no M5/M15 triggers, so scalps and M15 entries
are not tested), no economic calendar (no news filter), fixed spread + slippage, a bar touching
both stop and target counts as the stop, at most one trade per pair at a time.
"""
from __future__ import annotations

import copy
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .backtest import DEFAULT_COSTS, DEFAULT_HISTORY, LOOKBACK, _slice_closed, summarise
from .entry_test import load_or_fetch
from .fundamentals import PairFundamentals
from .history import atomic_write_json
from .instruments import instrument
from .models import Candles
from .signals import analyse_pair

MAX_HOLD = 120  # H1 bars (5 days, as live)
# signal generation settings: the old entry rules (before 4 Nov) and the current ones
GENERATIONS = {
    "old": {"max_extension_atr": 1.5, "market_entry_min_adx": 0},
    "live": {},
}
# rule sets replayed on the generated signals (gen = which signals; the rest are filters / management)
RULESETS = {
    "A. Old rules (before 4 Nov)": dict(gen="old", hours=None, cooldown_h=0, breakeven=False, d1=None),
    "B. Current live rules": dict(gen="live", hours=(7, 16), cooldown_h=4, breakeven=True, d1=None),
    "B + daily trend (strict)": dict(gen="live", hours=(7, 16), cooldown_h=4, breakeven=True, d1="strict"),
    "B + daily trend (loose)": dict(gen="live", hours=(7, 16), cooldown_h=4, breakeven=True, d1="loose"),
    "B without trading hours": dict(gen="live", hours=None, cooldown_h=4, breakeven=True, d1=None),
    "B without cooldown": dict(gen="live", hours=(7, 16), cooldown_h=0, breakeven=True, d1=None),
    "B without break-even": dict(gen="live", hours=(7, 16), cooldown_h=4, breakeven=False, d1=None),
    "B with old entry rules": dict(gen="old", hours=(7, 16), cooldown_h=4, breakeven=True, d1=None),
}
BASE = "B. Current live rules"


def generate(pair: str, h1: Candles, h4: Candles, d1: Optional[Candles], cfg: dict) -> list[dict]:
    """Every bar's signal with the attributes the rule sets filter on."""
    out = []
    nofund = PairFundamentals(available=False)
    for i in range(max(cfg["analysis"]["min_bars"], 205), len(h1) - 1):
        now_ts = h1.t[i] + 3600
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        tfs = {"H1": h1.window(i + 1, LOOKBACK), "H4": _slice_closed(h4, now_ts), "D1": _slice_closed(d1, now_ts) if d1 else None}
        res = analyse_pair(pair, tfs, None, nofund, cfg, now, True)
        if res.get("direction") not in ("BUY", "SELL") or res["confidence_score"] < cfg["signals"]["min_confidence"]:
            continue
        det = res.get("details") or {}
        tech = (det.get("technicals") or {}).get("H1") or {}
        sgn = 1 if res["direction"] == "BUY" else -1
        atr = res.get("atr") or 0.0
        out.append({
            "i": i, "t": now_ts, "hour": now.hour, "direction": res["direction"], "type": res["signal_type"],
            "setup": res.get("market_structure"), "score": res["confidence_score"],
            "entry": res["entry_price"], "sl": res["stop_loss"], "tp1": res["take_profit_1"], "tp2": res["take_profit_2"],
            "adx": tech.get("adx"), "ext_atr": round(sgn * (res["entry_price"] - res["ema_20"]) / atr, 2) if atr else None,
            "d1_agree": (det.get("d1_trend") or {}).get("agree"),
        })
    return out


def _gen_job(job: tuple) -> tuple:
    pair, name, h1, h4, d1, cfg = job
    c = copy.deepcopy(cfg)
    c["analysis"].update(GENERATIONS[name])
    c["analysis"]["d1_trend_filter"] = "shadow"  # recorded, filtered later
    c.setdefault("scalp", {})["enabled"] = False
    return pair, name, generate(pair, h1, h4, d1, c)


def replay(pair: str, h1: Candles, sigs: list[dict], rules: dict, costs: dict) -> list[dict]:
    """Trade the accepted signals in time order, one trade at a time per pair."""
    inst = instrument(pair)
    spread = costs["xau_spread_pips"] if inst.is_metal else costs["jpy_spread_pips"] if inst.is_jpy else costs["spread_pips"]
    cost, slip = (spread + costs["slippage_pips"]) * inst.pip_size, costs["slippage_pips"] * inst.pip_size
    trades, busy_until, last_loss = [], -1, {}
    for s in sigs:
        if s["i"] <= busy_until:
            continue  # a trade on this pair is still running: no repeat signal
        if rules["hours"] and not rules["hours"][0] <= s["hour"] < rules["hours"][1]:
            continue
        if rules["d1"] and s["d1_agree"] is not None and (s["d1_agree"] < 2 if rules["d1"] == "strict" else s["d1_agree"] < 1):
            continue
        if rules["cooldown_h"] and s["t"] - last_loss.get(s["direction"], -10**12) < rules["cooldown_h"] * 3600:
            continue
        buy = s["direction"] == "BUY"
        i, fill_i, fill = s["i"], None, None
        if s["type"] == "MARKET_ENTRY":
            fill_i = i + 1
            fill = h1.o[fill_i] + (cost if buy else -cost)
        else:
            for j in range(i + 1, min(len(h1), i + 5)):  # pending orders live 4 hours
                hit = (h1.l[j] <= s["entry"] <= h1.h[j]) if "LIMIT" in s["type"] else (h1.h[j] >= s["entry"] if buy else h1.l[j] <= s["entry"])
                if hit:
                    fill_i, fill = j, s["entry"] + (cost if buy else -cost)
                    break
        if fill_i is None:
            continue
        risk = abs(fill - s["sl"]) or 1e-12
        at_entry, out, exit_px, exit_i = False, None, None, None
        for j in range(fill_i, min(len(h1), fill_i + MAX_HOLD)):
            stop = fill if at_entry else s["sl"]
            if (h1.l[j] <= stop) if buy else (h1.h[j] >= stop):
                exit_px, exit_i = (stop - slip if buy else stop + slip), j
                out = "BREAKEVEN" if at_entry else "STOP"
                break
            if (h1.h[j] >= s["tp2"]) if buy else (h1.l[j] <= s["tp2"]):
                exit_px, exit_i, out = s["tp2"], j, "TARGET"
                break
            if rules["breakeven"] and ((h1.h[j] >= s["tp1"]) if buy else (h1.l[j] <= s["tp1"])):
                at_entry = True  # from the next bar on
        if out is None:
            exit_i = min(len(h1), fill_i + MAX_HOLD) - 1
            exit_px, out = h1.c[exit_i], "OPEN_AT_END" if exit_i == len(h1) - 1 else "TIME_EXIT"
        r = ((exit_px - fill) if buy else (fill - exit_px)) / risk
        trades.append({"pair": pair, "t": s["t"], "hour": s["hour"], "direction": s["direction"], "type": s["type"],
                       "setup": s["setup"], "score": s["score"], "adx": s["adx"], "ext_atr": s["ext_atr"],
                       "d1_agree": s["d1_agree"], "outcome": out, "r": round(r, 3)})
        busy_until = exit_i
        if r < 0:
            last_loss[s["direction"]] = h1.t[exit_i]
    return trades


def _halves(trades: list[dict], split: int) -> dict:
    return {"first_half": summarise([t for t in trades if t["t"] < split]),
            "second_half": summarise([t for t in trades if t["t"] >= split])}


def breakdown(trades: list[dict], key, split: int) -> dict:
    groups: dict = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {str(k): {**summarise(v), **{h: s["expectancy_r"] for h, s in _halves(v, split).items()}}
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def run_lab(cfg: dict, pairs: list[str], refresh: bool = False) -> dict:
    import asyncio
    costs = {**DEFAULT_COSTS, **(cfg.get("backtest") or {})}
    candles = asyncio.run(load_or_fetch(cfg, pairs, DEFAULT_HISTORY, refresh))
    pairs = [p for p in pairs if candles.get(p, {}).get("H1") and candles[p].get("H4")]
    starts = [candles[p]["H1"].t[0] for p in pairs]
    ends = [candles[p]["H1"].t[-1] for p in pairs]
    first, last = max(starts) + 205 * 3600, min(ends)
    split = first + (last - first) // 2
    jobs = [(p, g, candles[p]["H1"], candles[p]["H4"], candles[p].get("D1"), cfg) for p in pairs for g in GENERATIONS]
    signals: dict = {}
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as ex:
        for pair, gen, sigs in ex.map(_gen_job, jobs):
            signals.setdefault(gen, {})[pair] = sigs

    trades = {name: [t for p in pairs for t in replay(p, candles[p]["H1"], signals[r["gen"]][p], r, costs)]
              for name, r in RULESETS.items()}
    for v in trades.values():
        v.sort(key=lambda t: t["t"])
    rulesets = {name: {"overall": summarise(tr), **_halves(tr, split)} for name, tr in trades.items()}

    base = trades[BASE]
    # walk-forward setup cut: setups that lost in the first half are dropped, judged on the second half
    first_half = breakdown([t for t in base if t["t"] < split], lambda t: t["setup"], split)
    losing = sorted(k for k, s in first_half.items() if s["trades"] >= 10 and (s["expectancy_r"] or 0) < 0)
    kept = [t for t in base if t["setup"] not in losing]
    cut = {"setups_cut": losing, "rule": "setups with 10+ trades and a negative average in the first half",
           "before_cut": _halves(base, split), "after_cut": _halves(kept, split)}

    def bucket(v, edges):
        if v is None:
            return "n/a"
        lo = None
        for e in edges:
            if v < e:
                return f"under {e}" if lo is None else f"{lo}-{e}"
            lo = e
        return f"{edges[-1]}+"
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "costs": costs, "pairs": pairs,
        "period": {"from": datetime.fromtimestamp(first, timezone.utc).isoformat(),
                   "split": datetime.fromtimestamp(split, timezone.utc).isoformat(),
                   "to": datetime.fromtimestamp(last, timezone.utc).isoformat()},
        "rulesets": rulesets,
        "setup_cut_walk_forward": cut,
        "current_rules_by": {
            "setup": breakdown(base, lambda t: t["setup"], split),
            "type": breakdown(base, lambda t: t["type"], split),
            "direction": breakdown(base, lambda t: t["direction"], split),
            "pair": breakdown(base, lambda t: t["pair"], split),
            "hour_utc": breakdown(base, lambda t: f"{t['hour']:02d}", split),
            "daily_trend_agree": breakdown(base, lambda t: t["d1_agree"], split),
            "score": breakdown(base, lambda t: bucket(t["score"], [70, 75, 80, 85, 90]), split),
            "h1_adx": breakdown(base, lambda t: bucket(t["adx"], [15, 20, 25, 30, 40]), split),
        },
        "limitations": [__doc__.strip().split("\n\n")[-1].replace("\n", " ")],
    }
    out = Path(cfg["paths"]["state_dir"]) / "backtests" / f"lab_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    atomic_write_json(out, {**result, "trades": trades})
    result["_path"] = str(out)
    return result
