"""Walk-forward replay of the technical engine on history loaded from TradingView.

Limits, stated in every result: only H1/H4/D1 history is replayed (M5/M15 triggers and the
economic calendar are not available historically through the MCP, so entries come from H1
structure only and there is no news filter), each step sees the same 300-bar look-back as a
live scan, and costs are modelled as fixed spread + slippage. A small sample is reported as
statistically meaningless; no profitability is claimed.
"""
from __future__ import annotations

import copy
import json
import os
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .fundamentals import PairFundamentals
from .history import atomic_write_json
from .instruments import instrument
from .market_data import TradingViewData
from .mcp_client import TradingViewMCP
from .models import TF_SECONDS, Candles
from .signals import analyse_pair

# BTC: 1 pip = $1; $50 spread is an assumption (OANDA does not publish it through TradingView), change under backtest:
DEFAULT_COSTS = {"spread_pips": 1.5, "jpy_spread_pips": 2.0, "xau_spread_pips": 4.0, "btc_spread_pips": 50.0, "slippage_pips": 0.5}


def spread_pips_for(inst, costs: dict) -> float:
    if inst.is_crypto:
        return costs.get("btc_spread_pips", DEFAULT_COSTS["btc_spread_pips"])
    return costs["xau_spread_pips"] if inst.is_metal else costs["jpy_spread_pips"] if inst.is_jpy else costs["spread_pips"]
DEFAULT_HISTORY = {"H1": 5000, "H4": 2000, "D1": 800}
LOOKBACK = 300  # bars per timeframe a live scan analyses
MIN_MEANINGFUL_TRADES = 100


def _slice_closed(c: Candles, until_ts: int) -> Candles:
    """The last LOOKBACK bars that had fully closed by ``until_ts``."""
    end = bisect_right(c.t, until_ts - TF_SECONDS[c.tf])
    return c.window(end, LOOKBACK)


def simulate_pair(pair: str, h1: Candles, h4: Candles, d1: Optional[Candles], cfg: dict, costs: dict) -> list[dict]:
    inst = instrument(pair)
    spread = spread_pips_for(inst, costs)
    cost = (spread + costs["slippage_pips"]) * inst.pip_size
    trades: list[dict] = []
    start = max(cfg["analysis"]["min_bars"], 205)
    i = start
    nofund = PairFundamentals(available=False)
    while i < len(h1) - 1:
        now_ts = h1.t[i] + 3600
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        tfs = {"H1": h1.window(i + 1, LOOKBACK), "H4": _slice_closed(h4, now_ts), "D1": _slice_closed(d1, now_ts) if d1 else None}
        res = analyse_pair(pair, tfs, None, nofund, cfg, now, True)
        if res.get("direction") not in ("BUY", "SELL") or res["confidence_score"] < cfg["signals"]["min_confidence"]:
            i += 1
            continue
        buy = res["direction"] == "BUY"
        entry, sl, tp = res["entry_price"], res["stop_loss"], res["take_profit_2"]
        expiry_bars = max(1, cfg["signals"]["expiry_minutes"].get(res["signal_type"], 60) // 60)
        fill_i, fill = None, None
        if res["signal_type"] == "MARKET_ENTRY":
            fill_i = i + 1
            fill = h1.o[fill_i] + (cost if buy else -cost)
        else:
            for j in range(i + 1, min(len(h1), i + 1 + expiry_bars)):
                touched = (h1.l[j] <= entry <= h1.h[j]) if "LIMIT" in res["signal_type"] else \
                          (h1.h[j] >= entry if buy else h1.l[j] <= entry)
                if touched:
                    fill_i, fill = j, entry + (cost if buy else -cost)
                    break
        if fill_i is None:
            i += 1
            continue
        risk = abs(fill - sl)
        exit_px, exit_i, outcome = None, None, None
        for j in range(fill_i, min(len(h1), fill_i + 120)):
            hit_sl = h1.l[j] <= sl if buy else h1.h[j] >= sl
            hit_tp = h1.h[j] >= tp if buy else h1.l[j] <= tp
            if hit_sl:  # if both in one bar, assume the stop came first (conservative); stops slip
                slip = costs["slippage_pips"] * inst.pip_size
                exit_px, exit_i, outcome = (sl - slip if buy else sl + slip), j, "STOP"
                break
            if hit_tp:
                exit_px, exit_i, outcome = tp, j, "TARGET"
                break
        if exit_px is None:
            exit_i = min(len(h1), fill_i + 120) - 1
            exit_px, outcome = h1.c[exit_i], "OPEN_AT_END" if exit_i == len(h1) - 1 else "TIME_EXIT"
        r = ((exit_px - fill) if buy else (fill - exit_px)) / risk if risk else 0.0
        trades.append({"pair": pair, "direction": res["direction"], "type": res["signal_type"], "score": res["confidence_score"],
                       "setup": res.get("market_structure"),
                       "signal_time": now.isoformat(), "fill": round(fill, inst.price_decimals), "stop": sl, "target": tp,
                       "exit": round(exit_px, inst.price_decimals), "outcome": outcome, "r": round(r, 3)})
        i = exit_i + 1
    return trades


def summarise(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["outcome"] != "OPEN_AT_END"]
    rs = [t["r"] for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    n = len(rs)
    return {
        "trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1) if n else None,
        "expectancy_r": round(sum(rs) / n, 3) if n else None,
        "total_r": round(sum(rs), 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
        "max_drawdown_r": round(mdd, 2),
        "statistically_meaningful": n >= MIN_MEANINGFUL_TRADES,
        "verdict": (f"Only {n} trades — far too few to draw conclusions. No profitability claim is made."
                    if n < MIN_MEANINGFUL_TRADES else
                    "Sample size is adequate for a first look, but out-of-sample validation is still required."),
    }


def _run_one(job: tuple) -> tuple:
    pair, mode, h1, h4, d1, cfg, costs = job
    c = copy.deepcopy(cfg)
    c["analysis"]["momentum_mode"] = mode
    return pair, mode, simulate_pair(pair, h1, h4, d1, c, costs)


async def fetch_history(cfg: dict, pairs: list[str], targets: dict[str, int]) -> tuple[dict, dict]:
    from .scanner import scan_lock  # the live scanner must not drive the chart at the same time
    with scan_lock(Path(cfg["paths"]["state_dir"])):
        async with TradingViewMCP(cfg) as mcp:
            data = TradingViewData(mcp, cfg)
            await data.health()
            original = await data.begin_chart_session()
            try:
                tickers = await data.resolve_symbols(pairs)
                candles = await data.load_history(tickers, targets)
            finally:
                await data.end_chart_session(original)
    return tickers, candles


async def run_backtest(cfg: dict, pairs: list[str], costs: Optional[dict] = None,
                       modes: tuple[str, ...] = ("strict", "adaptive"), targets: Optional[dict] = None) -> dict:
    costs = {**DEFAULT_COSTS, **(costs or cfg.get("backtest", {}) or {})}
    tickers, candles = await fetch_history(cfg, pairs, targets or DEFAULT_HISTORY)
    jobs, coverage = [], {}
    for pair in tickers:
        c = candles[pair]
        if not c.get("H1") or not c.get("H4"):
            coverage[pair] = {"error": "history unavailable"}
            continue
        coverage[pair] = {tf: len(c[tf]) for tf in ("H1", "H4", "D1") if c.get(tf)}
        coverage[pair]["from"] = datetime.fromtimestamp(c["H1"].t[0], timezone.utc).isoformat()
        coverage[pair]["to"] = datetime.fromtimestamp(c["H1"].t[-1], timezone.utc).isoformat()
        for mode in modes:
            jobs.append((pair, mode, c["H1"], c["H4"], c.get("D1"), cfg, costs))
    trades: dict[str, list] = {m: [] for m in modes}
    per_pair: dict[str, dict] = {}
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as ex:
        for pair, mode, tr in ex.map(_run_one, jobs):
            trades[mode] += tr
            per_pair.setdefault(pair, {"coverage": coverage[pair]})[mode] = summarise(tr)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "costs": costs, "coverage": coverage,
        "overall": {m: summarise(trades[m]) for m in modes},
        "by_setup": {m: _by_setup(trades[m]) for m in modes},
        "per_pair": per_pair, "trades": trades,
        "limitations": [__doc__.strip().split("\n\n")[1].replace("\n", " ")],
    }
    out = Path(cfg["paths"]["state_dir"]) / "backtests" / f"backtest_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    atomic_write_json(out, result)
    result["_path"] = str(out)
    return result


def _by_setup(trades: list[dict]) -> dict:
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(t.get("setup") or "UNKNOWN", []).append(t)
    return {k: summarise(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}
