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
from . import indicators as ind
from .structure import find_quasimodo, find_swings

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
# Quasimodo reversal (QMR): limit at the QM level (left shoulder), stop beyond the head, targets in R
QM_RULESETS = {
    "QM · targets 2R/3R (house rules)": dict(tp=(2.0, 3.0), hours=(7, 16), cooldown_h=4, breakeven=True, d1=None),
    "QM · targets 1R/2R": dict(tp=(1.0, 2.0), hours=(7, 16), cooldown_h=4, breakeven=True, d1=None),
    "QM · targets 1.5R/2.5R": dict(tp=(1.5, 2.5), hours=(7, 16), cooldown_h=4, breakeven=True, d1=None),
    "QM · 2R/3R, any hour": dict(tp=(2.0, 3.0), hours=None, cooldown_h=4, breakeven=True, d1=None),
    "QM · 2R/3R, no break-even": dict(tp=(2.0, 3.0), hours=(7, 16), cooldown_h=4, breakeven=False, d1=None),
}
QM_BASE = "QM · targets 2R/3R (house rules)"


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


def qm_signals(pair: str, h1: Candles, h4: Candles, cfg: dict, start: int = 205, end: Optional[int] = None) -> list[dict]:
    """Every Quasimodo on H1 at the bar its structure break closes (one signal per pattern).
    Bars start..end-1 are checked; the lab stops one bar early (a fill needs the next bar), live checks all."""
    a = cfg["analysis"]
    width = a["swing_width"].get("H1", 3)
    swings, atr = find_swings(h1, width), ind.atr(h1.h, h1.l, h1.c, 14)
    e50 = ind.ema(h4.c, 50)
    out, j4 = [], 0
    for i in range(start, len(h1) - 1 if end is None else end):
        now_ts = h1.t[i] + 3600
        while j4 + 1 < len(h4) and h4.t[j4 + 1] + 4 * 3600 <= now_ts:
            j4 += 1  # last closed H4 bar
        for q in find_quasimodo(h1, swings, width, i, max_break_ago=0):
            x = atr[i]
            if not x:
                continue
            bear = q.direction == "BEARISH"
            sl = q.head + (x * a["sl_buffer_atr"] if bear else -x * a["sl_buffer_atr"])
            risk = abs(sl - q.qml)
            if not (a["min_sl_atr"] * x <= risk <= a["max_sl_atr"] * x) or abs(q.qml - h1.c[i]) < 0.2 * x:
                continue
            h4_with = None if e50[j4] is None else (h4.c[j4] < e50[j4]) == bear
            out.append({"i": i, "t": now_ts, "hour": datetime.fromtimestamp(now_ts, timezone.utc).hour,
                        "direction": "SELL" if bear else "BUY", "type": "SELL_LIMIT" if bear else "BUY_LIMIT",
                        "setup": f"{q.direction}_QUASIMODO", "score": None, "entry": q.qml, "sl": sl, "risk": risk,
                        "adx": None, "ext_atr": None, "d1_agree": None, "valid_bars": 12,
                        "h4_trend": "with H4 trend" if h4_with else "against H4 trend" if h4_with is not None else "n/a",
                        "risk_atr": round(risk / x, 2)})
    return out


def _with_targets(sigs: list[dict], tp: tuple) -> list[dict]:
    sg = lambda s: -1 if s["direction"] == "SELL" else 1
    return [{**s, "tp1": s["entry"] + sg(s) * tp[0] * s["risk"], "tp2": s["entry"] + sg(s) * tp[1] * s["risk"]} for s in sigs]


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
            for j in range(i + 1, min(len(h1), i + 1 + s.get("valid_bars", 4))):  # pending orders live 4 hours (QM: 12)
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
                       "d1_agree": s["d1_agree"], "h4_trend": s.get("h4_trend"), "risk_atr": s.get("risk_atr"),
                       "outcome": out, "r": round(r, 3), "exit_t": h1.t[exit_i]})
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


def run_qm_lab(cfg: dict, pairs: list[str], refresh: bool = False) -> dict:
    """The Quasimodo reversal on its own: same history, costs, replay and walk-forward split as run_lab."""
    import asyncio
    costs = {**DEFAULT_COSTS, **(cfg.get("backtest") or {})}
    candles = asyncio.run(load_or_fetch(cfg, pairs, DEFAULT_HISTORY, refresh))
    pairs = [p for p in pairs if candles.get(p, {}).get("H1") and candles[p].get("H4")]
    first = max(candles[p]["H1"].t[0] for p in pairs) + 205 * 3600
    last = min(candles[p]["H1"].t[-1] for p in pairs)
    split = first + (last - first) // 2
    sigs = {p: [s for s in qm_signals(p, candles[p]["H1"], candles[p]["H4"], cfg) if first <= s["t"] <= last] for p in pairs}
    trades = {name: sorted((t for p in pairs for t in replay(p, candles[p]["H1"], _with_targets(sigs[p], r["tp"]), r, costs)),
                           key=lambda t: t["t"]) for name, r in QM_RULESETS.items()}
    base = trades[QM_BASE]
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "costs": costs, "pairs": pairs,
        "period": {"from": datetime.fromtimestamp(first, timezone.utc).isoformat(),
                   "split": datetime.fromtimestamp(split, timezone.utc).isoformat(),
                   "to": datetime.fromtimestamp(last, timezone.utc).isoformat()},
        "patterns_found": sum(len(v) for v in sigs.values()),
        "rulesets": {name: {"overall": summarise(tr), **_halves(tr, split)} for name, tr in trades.items()},
        "qm_by": {
            "direction": breakdown(base, lambda t: t["direction"], split),
            "h4_trend": breakdown(base, lambda t: t["h4_trend"], split),
            "hour_utc": breakdown(base, lambda t: f"{t['hour']:02d}", split),
            "pair": breakdown(base, lambda t: t["pair"], split),
            "stop_atr": breakdown(base, lambda t: "under 1" if t["risk_atr"] < 1 else "1-2" if t["risk_atr"] < 2 else "2+", split),
        },
        "limitations": [__doc__.strip().split("\n\n")[-1].replace("\n", " ")],
    }
    out = Path(cfg["paths"]["state_dir"]) / "backtests" / f"lab_qm_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    atomic_write_json(out, {**result, "trades": trades})
    result["_path"] = str(out)
    return result


# ---- ideas lab: market type (trending / ranging) and the London-open sweep of the Asian range ----

def efficiency(c: list[float], i: int, n: int = 48) -> Optional[float]:
    """Kaufman efficiency ratio over n bars: net move / path length. Near 1 = clean trend, near 0 = chop."""
    if i < n:
        return None
    path = sum(abs(c[k] - c[k - 1]) for k in range(i - n + 1, i + 1))
    return abs(c[i] - c[i - n]) / path if path else None


def _regime_fields(h1: Candles, h4: Candles, h4_adx: list) -> callable:
    """Attach the market type at signal time: H1 efficiency ratio (48 bars) and H4 ADX of the last closed H4 bar."""
    import bisect

    def tag(s: dict) -> dict:
        j = bisect.bisect_right(h4.t, s["t"] - 4 * 3600) - 1
        return {**s, "er": efficiency(h1.c, s["i"]), "h4_adx": h4_adx[j] if j >= 0 else None}
    return tag


def asian_sweep_signals(pair: str, h1: Candles, cfg: dict, window=(7, 10), min_range_atr=0.5, max_range_atr=3.0) -> list[dict]:
    """London open: an H1 bar in ``window`` (UTC) trades beyond the Asian range (00-07 UTC) and closes back
    inside it -> enter the other way at the next open, stop beyond the sweep extreme. One per pair per day."""
    atr = ind.atr(h1.h, h1.l, h1.c, 14)
    buf = cfg["analysis"]["sl_buffer_atr"]
    out, done_day = [], None
    for i in range(205, len(h1) - 1):
        t = datetime.fromtimestamp(h1.t[i], timezone.utc)
        if not window[0] <= t.hour < window[1] or t.date() == done_day or not atr[i]:
            continue
        day0 = h1.t[i] - t.hour * 3600
        asia = [k for k in range(max(0, i - 12), i) if day0 <= h1.t[k] < day0 + 7 * 3600]
        if len(asia) < 6:
            continue
        hi, lo = max(h1.h[k] for k in asia), min(h1.l[k] for k in asia)
        if not min_range_atr * atr[i] <= hi - lo <= max_range_atr * atr[i]:
            continue
        # the session's extreme so far, including earlier London bars
        sess = range(asia[-1] + 1, i + 1)
        top, bot = max(h1.h[k] for k in sess), min(h1.l[k] for k in sess)
        sell = h1.h[i] > hi and h1.c[i] < hi and top == h1.h[i]
        buy = h1.l[i] < lo and h1.c[i] > lo and bot == h1.l[i]
        if sell == buy:
            continue
        sl = h1.h[i] + buf * atr[i] if sell else h1.l[i] - buf * atr[i]
        done_day = t.date()
        out.append({"i": i, "t": h1.t[i] + 3600, "hour": t.hour + 1, "direction": "SELL" if sell else "BUY",
                    "type": "MARKET_ENTRY", "setup": f"{'BEARISH' if sell else 'BULLISH'}_ASIAN_RANGE_SWEEP",
                    "score": None, "adx": None, "ext_atr": None, "d1_agree": None,
                    "entry": h1.c[i], "sl": sl, "risk": abs(h1.c[i] - sl),
                    "range_mid": (hi + lo) / 2, "range_far": lo if sell else hi, "range_atr": round((hi - lo) / atr[i], 2)})
    return out


def _range_targets(sigs: list[dict]) -> list[dict]:
    """TP1 at the middle of the Asian range, TP2 at its far side (classic sweep target), at least 1R."""
    out = []
    for s in sigs:
        sg = -1 if s["direction"] == "SELL" else 1
        tp1 = s["range_mid"] if sg * (s["range_mid"] - s["entry"]) >= 0.5 * s["risk"] else s["entry"] + sg * 0.5 * s["risk"]
        tp2 = s["range_far"] if sg * (s["range_far"] - s["entry"]) >= s["risk"] else s["entry"] + sg * s["risk"]
        out.append({**s, "tp1": tp1, "tp2": tp2})
    return out


def _live_signals(cfg: dict, pairs: list[str], candles: dict, refresh: bool) -> dict:
    """The current engine's signals on every bar, cached (generation is the slow part of the lab)."""
    import pickle
    cache = Path(cfg["paths"]["state_dir"]) / "backtests" / "lab_live_signals.pkl"
    if cache.exists() and not refresh:
        data = pickle.loads(cache.read_bytes())
        if set(pairs) <= set(data):
            return data
    jobs = [(p, "live", candles[p]["H1"], candles[p]["H4"], candles[p].get("D1"), cfg) for p in pairs]
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as ex:
        data = {pair: sigs for pair, _, sigs in ex.map(_gen_job, jobs)}
    cache.write_bytes(pickle.dumps(data))
    return data


def run_ideas_lab(cfg: dict, pairs: list[str], refresh: bool = False) -> dict:
    """Does trading each setup only in its market type help, and does the London sweep of the Asian range pay?"""
    import asyncio
    costs = {**DEFAULT_COSTS, **(cfg.get("backtest") or {})}
    candles = asyncio.run(load_or_fetch(cfg, pairs, DEFAULT_HISTORY, refresh))
    pairs = [p for p in pairs if candles.get(p, {}).get("H1") and candles[p].get("H4")]
    first = max(candles[p]["H1"].t[0] for p in pairs) + 205 * 3600
    last = min(candles[p]["H1"].t[-1] for p in pairs)
    split = first + (last - first) // 2
    off = set((cfg.get("signals") or {}).get("disabled_setups") or [])
    live = _live_signals(cfg, pairs, candles, refresh)
    house = dict(hours=(7, 16), cooldown_h=4, breakeven=True, d1=None)
    strat: dict = {"current": {}, "qmr": {}, "sweep": {}}
    for p in pairs:
        h1, h4 = candles[p]["H1"], candles[p]["H4"]
        tag = _regime_fields(h1, h4, ind.adx(h4.h, h4.l, h4.c, 14).adx)
        strat["current"][p] = [tag(s) for s in live.get(p, []) if s["setup"] not in off and first <= s["t"] <= last]
        strat["qmr"][p] = [tag(s) for s in _with_targets(qm_signals(p, h1, h4, cfg), (1.0, 2.0)) if first <= s["t"] <= last]
        strat["sweep"][p] = [tag(s) for s in asian_sweep_signals(p, h1, cfg) if first <= s["t"] <= last]

    def run(name: str, keep=lambda s: True, targets=None, rules=house) -> list[dict]:
        tr = []
        for p in pairs:
            sigs = [s for s in strat[name][p] if keep(s)]
            if targets:
                sigs = targets(sigs)
            for t in replay(p, candles[p]["H1"], sigs, rules, costs):
                tr.append(t)
        # carry the market type onto each trade for the breakdowns
        idx = {(s["t"], p): s for p in pairs for s in strat[name][p]}
        for t in tr:
            s = idx.get((t["t"], t["pair"])) or {}
            t["er"], t["h4_adx"] = s.get("er"), s.get("h4_adx")
        return sorted(tr, key=lambda t: t["t"])

    trending = lambda s, er=0.3: s["er"] is not None and s["er"] >= er
    ranging = lambda s, er=0.3: s["er"] is not None and s["er"] < er
    adx_trend = lambda s, a=25: s["h4_adx"] is not None and s["h4_adx"] >= a
    variants = {
        "Current rules (7 setups off)": run("current"),
        "Current, trending only (ER>=0.3)": run("current", trending),
        "Current, trending only (ER>=0.2)": run("current", lambda s: trending(s, 0.2)),
        "Current, H4 ADX>=25 only": run("current", adx_trend),
        "Current, ranging only (ER<0.3)": run("current", ranging),
        "QMR 1R/2R": run("qmr"),
        "QMR, ranging only (ER<0.3)": run("qmr", ranging),
        "QMR, ranging only (ER<0.2)": run("qmr", lambda s: ranging(s, 0.2)),
        "QMR, H4 ADX<25 only": run("qmr", lambda s: s["h4_adx"] is not None and s["h4_adx"] < 25),
        "Sweep, range targets": run("sweep", targets=_range_targets),
        "Sweep, 1R/2R": run("sweep", targets=lambda v: _with_targets(v, (1.0, 2.0))),
        "Sweep, 1.5R/2.5R": run("sweep", targets=lambda v: _with_targets(v, (1.5, 2.5))),
        "Sweep, range targets, ranging (ER<0.3)": run("sweep", ranging, targets=_range_targets),
        "Sweep, range targets, H4 ADX<25": run("sweep", lambda s: s["h4_adx"] is not None and s["h4_adx"] < 25, targets=_range_targets),
    }
    def er_b(t):
        return "n/a" if t["er"] is None else "ER<0.15" if t["er"] < 0.15 else "ER 0.15-0.3" if t["er"] < 0.3 else "ER 0.3-0.45" if t["er"] < 0.45 else "ER>=0.45"
    def adx_b(t):
        return "n/a" if t["h4_adx"] is None else "H4 ADX<20" if t["h4_adx"] < 20 else "H4 ADX 20-25" if t["h4_adx"] < 25 else "H4 ADX 25-35" if t["h4_adx"] < 35 else "H4 ADX>=35"
    by = {}
    for name in ("Current rules (7 setups off)", "QMR 1R/2R", "Sweep, range targets"):
        by[name] = {"efficiency": breakdown(variants[name], er_b, split), "h4_adx": breakdown(variants[name], adx_b, split)}
    by["Sweep, range targets"]["direction"] = breakdown(variants["Sweep, range targets"], lambda t: t["direction"], split)
    by["Sweep, range targets"]["hour_utc"] = breakdown(variants["Sweep, range targets"], lambda t: f"{t['hour']:02d}", split)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "costs": costs, "pairs": pairs,
        "period": {"from": datetime.fromtimestamp(first, timezone.utc).isoformat(),
                   "split": datetime.fromtimestamp(split, timezone.utc).isoformat(),
                   "to": datetime.fromtimestamp(last, timezone.utc).isoformat()},
        "rulesets": {k: {"overall": summarise(v), **_halves(v, split)} for k, v in variants.items()},
        "by": by,
        "limitations": [__doc__.strip().split("\n\n")[-1].replace("\n", " ")],
    }
    out = Path(cfg["paths"]["state_dir"]) / "backtests" / f"lab_ideas_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    atomic_write_json(out, {**result, "trades": variants})
    result["_path"] = str(out)
    return result
