"""JSON report building/writing and terminal display."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .history import atomic_write_json, effective_status
from .market_hours import market_status

SCHEMA_SIGNAL_KEYS = [
    "rank", "symbol", "direction", "signal_type", "entry_price", "stop_loss", "take_profit_1", "take_profit_2",
    "take_profit_3", "risk_reward", "confidence_score", "trend", "momentum", "rsi", "ema_20", "ema_50", "atr",
    "timeframe_alignment", "market_structure", "catalyst", "headlines", "economic_risk", "reason", "status",
]


def _signal_out(s: dict) -> dict:
    out = {k: s.get(k) for k in SCHEMA_SIGNAL_KEYS}
    out.update({
        "signal_id": s.get("signal_id"), "created_at": s.get("created_at"), "expires_at": s.get("expires_at"),
        "actionable": s.get("actionable", False), "duplicate_of_existing": s.get("duplicate_of_existing"),
        "tradingview_symbol": s.get("tradingview_symbol"), "live": s.get("live"),
        "position_size": s.get("position_size"), "portfolio": s.get("portfolio"), "risk_status": s.get("risk_status"),
        "details": s.get("details"),
        # trade style (SCALP / QMR; none = intraday) and its timing, so the dashboard can tag and filter it
        "style": s.get("style"), "max_hold_minutes": s.get("max_hold_minutes"), "entry_minutes": s.get("entry_minutes"),
    })
    return out


def _watch_out(w: dict) -> dict:
    return {
        "symbol": w["symbol"], "direction": "WAIT", "wait_code": w.get("wait_code"), "reason": w.get("reason"),
        "bias": w.get("bias"), "confidence_score": w.get("confidence_score"), "rsi": w.get("rsi"),
        "ema_20": w.get("ema_20"), "ema_50": w.get("ema_50"), "atr": w.get("atr"),
        "timeframe_bias": w.get("timeframe_bias"), "economic_risk": w.get("economic_risk"),
        "catalyst": w.get("catalyst"), "headlines": w.get("headlines", []),
        "tradingview_symbol": w.get("tradingview_symbol"), "live": w.get("live"), "details": w.get("details"),
    }


def build_report(cfg: dict, now: datetime, raw: dict, analysis: dict, duration_s: float) -> dict:
    ranked, watch = analysis["ranked"], analysis["watchlist"]
    feeds = Counter((r.get("live") or {}).get("feed", "UNKNOWN") for r in analysis["results"])
    events = analysis["events"]
    pairs_ccy = {c for p in raw["tickers"] for c in (p[:3], p[3:6])}
    upcoming = []
    if events is not None:
        horizon = now + timedelta(hours=cfg["news_filter"]["summary_hours"])
        upcoming = [e.to_dict() | {"within_60_min": e.date <= now + timedelta(minutes=60)}
                    for e in events if e.importance >= 1 and e.currency in pairs_ccy and now - timedelta(minutes=15) <= e.date <= horizon]
    feed_mode = "REAL_TIME" if feeds and set(feeds) == {"REAL_TIME"} else "DELAYED" if feeds.get("DELAYED") else \
        "MIXED" if len(feeds) > 1 else next(iter(feeds), "UNKNOWN")
    limitations = list(dict.fromkeys(raw["limitations"] + [
        "Technical-rating, calendar, headline and bid/ask data come from TradingView web endpoints via the MCP "
        "ui_evaluate tool (no dedicated MCP tools exist); they are undocumented and may change.",
        "Headlines are attached for context only; they are not sentiment-scored.",
        "VWAP uses broker tick volume (spot FX has no central volume) and is informational only.",
        "Confidence scores are heuristics, not statistically validated probabilities of profit.",
    ]))
    return {
        "scanned_at": now.isoformat(),
        "data_source": "TradingView MCP",
        "market_status": market_status(now),
        "feed": {"mode": feed_mode, "counts": dict(feeds)},
        "signals": [_signal_out(s) for s in ranked],
        "watchlist": [_watch_out(w) for w in watch],
        "outlook": analysis.get("outlooks", []),
        "currency_strength": analysis.get("currency_strength", []),
        "strength_pairings": analysis.get("strength_pairings", []),
        "track_record": analysis.get("track_record"),
        "market_summary": {
            "bullish_pairs": sum(1 for s in ranked if s["direction"] == "BUY"),
            "bearish_pairs": sum(1 for s in ranked if s["direction"] == "SELL"),
            "waiting_pairs": len(watch),
            "pairs_analysed": len(analysis["results"]),
            "trend_bias": dict(Counter(_bias(r) for r in analysis["results"])),
            "high_impact_events": upcoming,
            "currency_exposure_pct": analysis["exposure"],
        },
        "risk_settings": {k: analysis.get("risk", cfg["risk"]).get(k) for k in (
            "account_balance", "account_currency", "risk_per_trade_pct", "max_open_positions", "max_daily_loss_pct",
            "daily_loss_used_pct", "max_currency_exposure_pct", "max_total_risk_pct", "source")},
        "account": analysis.get("account"),
        "limitations": limitations,
        "scan": {"duration_s": round(duration_s, 1), "mcp_calls": raw["mcp_calls"],
                 "symbols": raw["tickers"], "timeframes": ["M5", "M15", "H1", "H4", "D1"]},
        "disclaimer": "Analysis only. No trades are executed. Not financial advice.",
    }


def _bias(r: dict) -> str:
    if r.get("direction") == "BUY":
        return "BULLISH"
    if r.get("direction") == "SELL":
        return "BEARISH"
    return r.get("bias") or "UNKNOWN"


def write_report(cfg: dict, report: dict, now: datetime) -> Path:
    out_dir = Path(cfg["paths"]["output_dir"])
    path = out_dir / f"forex_signals_{now:%Y-%m-%d}.json"
    atomic_write_json(path, report)
    archive = Path(cfg["paths"]["state_dir"]) / "scans" / f"scan_{now:%Y%m%dT%H%M%SZ}.json"
    atomic_write_json(archive, report)
    return path


def write_chart_data(cfg: dict, raw: dict, analysis: dict, bars: int = 150) -> Path:
    """Candles + EMA20/50/200 + key levels + signal levels per pair/timeframe, for the dashboard chart."""
    from .indicators import atr, ema
    from .structure import analyze
    a_cfg = cfg["analysis"]
    signals = {s["symbol"]: s for s in analysis["ranked"]}
    results = {r["symbol"]: r for r in analysis["results"]}
    out: dict = {}
    for pair, tfs in raw["candles"].items():
        r = results.get(pair, {})
        entry: dict = {"key_levels": (r.get("details") or {}).get("key_levels"), "timeframes": {}}
        s = signals.get(pair)
        if s:
            entry["signal"] = {k: s.get(k) for k in ("direction", "signal_type", "entry_price", "stop_loss",
                                                     "take_profit_1", "take_profit_2", "take_profit_3")}
        for tf, c in tfs.items():
            if c is None or not len(c):
                continue
            n = min(bars, len(c))
            lines = {}
            for length in (20, 50, 200):
                e = ema(c.c, length)[-n:]
                lines[f"ema{length}"] = [[c.t[-n + i], round(v, 6)] for i, v in enumerate(e) if v is not None]
            rep = analyze(c, atr(c.h, c.l, c.c, 14), None, width=a_cfg["swing_width"].get(tf, 3),
                          ob_cfg=a_cfg.get("order_block"))
            first_t = c.t[-n]
            entry["timeframes"][tf] = {
                "candles": [[c.t[i], c.o[i], c.h[i], c.l[i], c.c[i]] for i in range(len(c) - n, len(c))],
                **lines,
                "breaks": [{"t": e.t, "kind": e.kind, "direction": e.direction, "price": e.price}
                           for e in rep.events if e.kind in ("BOS", "CHOCH") and e.t >= first_t][-15:],
                "order_blocks": [{"kind": o.kind, "low": o.low, "high": o.high, "t": o.t, "fresh": o.fresh,
                                  "formed_by": o.formed_by} for o in rep.order_blocks if o.t >= first_t],
            }
        out[pair] = entry
    path = Path(cfg["paths"]["state_dir"]) / "chart_data.json"
    atomic_write_json(path, out)
    return path


BLOCKER_LABELS = {
    "RSI_MOMENTUM": "RSI momentum missing", "MACD_FILTER": "MACD not supporting", "EMA_FILTER": "EMA 20/50 not aligned",
    "RSI_EXTREME": "RSI stretched (won't chase)", "NO_TREND": "No clear H1/H4 trend", "CONFLICT": "H1 and H4 disagree",
    "POOR_RR": "Level blocks the target / reward too small", "NO_TRIGGER": "No entry trigger yet",
    "STOP_TOO_WIDE": "Structural stop too far", "NO_STRUCTURE_STOP": "No structure for a stop",
    "NEWS_BLACKOUT": "High-impact news soon", "HIGH_SPREAD": "Spread too wide", "LOW_CONFIDENCE": "Score below 65",
    "INSUFFICIENT_DATA": "Missing data", "STALE_DATA": "Stale data", "DATA_MISMATCH": "Price/candle mismatch",
    "OUTSIDE_TOP_N": "Valid but outside top 10",
}


def blocker_stats(state_dir: str, hours: int = 24) -> dict:
    """How often each rule was the reason for WAIT across recent scans (from the scan archive)."""
    from collections import Counter
    import json
    from datetime import timedelta, timezone as tz
    cutoff = datetime.now(tz.utc) - timedelta(hours=hours)
    counts: Counter = Counter()
    scans = signals = 0
    for f in sorted((Path(state_dir) / "scans").glob("scan_*.json"))[-400:]:
        try:
            r = json.loads(f.read_text())
            if datetime.fromisoformat(r["scanned_at"]) < cutoff:
                continue
        except (ValueError, KeyError, OSError):
            continue
        scans += 1
        signals += len(r.get("signals", []))
        counts.update(w.get("wait_code") for w in r.get("watchlist", []))
    total = sum(counts.values())
    rows = [{"code": c, "label": BLOCKER_LABELS.get(c, c), "count": n, "pct": round(100 * n / total, 1) if total else 0}
            for c, n in counts.most_common()]
    return {"hours": hours, "scans": scans, "signals": signals, "pair_checks": total + signals, "blockers": rows}


def merge_open_signals(report: dict, history, now: datetime, answers: Optional[dict] = None) -> dict:
    """Show every still-valid signal: the latest scan's plus earlier ones whose order can still fill or
    whose trade is still running. Each says whether the latest scan still confirms it."""
    from . import taken as taken_store
    from .history import display_status, on_board
    board = lambda rec: on_board(rec, now, taken_store.status((answers or {}).get(rec.get("signal_id"))))  # noqa: E731
    by_id = {r.get("signal_id"): r for r in history.records}
    latest_ids = set()
    # a signal from the latest scan that has since expired unfilled or finished belongs in Past signals
    report["signals"] = [s for s in report.get("signals", [])
                         if s.get("signal_id") not in by_id or board(by_id[s["signal_id"]])]
    for s in report["signals"]:
        latest_ids.add(s.get("signal_id"))
        rec = by_id.get(s.get("signal_id"), {})
        s.update(confirmed_by_latest_scan=True, carried_over=False,
                 **{k: rec.get(k) for k in ("outcome", "fill_price", "filled_at", "open_r", "tp1_hit") if rec.get(k) is not None})
        if rec.get("outcome") == "OPEN":
            s["status"] = "RUNNING"
    rows = {w["symbol"]: w for w in report.get("watchlist", [])}
    live = {x["symbol"]: x.get("live") for x in report.get("signals", []) + report.get("watchlist", [])}
    carried = []
    for rec in history.records:
        if rec.get("signal_id") in latest_ids or not board(rec):
            continue
        w = rows.get(rec["symbol"], {})
        note = f"latest scan: WAIT — {w.get('reason')}" if w else "latest scan: not re-checked"
        carried.append({
            **{k: v for k, v in rec.items() if k not in ("key", "notified_new", "notified_outcome")},
            "status": display_status(rec, now),
            "carried_over": True,
            "confirmed_by_latest_scan": False,
            "latest_scan_note": note,
            "live": live.get(rec["symbol"]) or rec.get("live"),
        })
    carried.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    report["signals"] = report.get("signals", []) + carried
    for i, s in enumerate(report["signals"], 1):
        s["rank"] = i
    return report


def refresh_statuses(report: dict, now: datetime) -> dict:
    """Recompute status at read time so an expired signal is never shown as ACTIVE."""
    for s in report.get("signals", []):
        s["status"] = effective_status(s, now)
        if s["status"] != "ACTIVE":
            s["actionable"] = False
    return report


GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def format_terminal(report: dict, color: bool = True, now: Optional[datetime] = None) -> str:
    if now:
        refresh_statuses(report, now)
    c = (lambda code, s: f"{code}{s}{RESET}") if color else (lambda code, s: s)
    ms = report["market_summary"]
    lines = [
        c(BOLD, f"FOREX SIGNALS  {report['scanned_at']}  market={report['market_status']}  feed={report['feed']['mode']}"),
        f"pairs analysed {ms['pairs_analysed']} | BUY {ms['bullish_pairs']} | SELL {ms['bearish_pairs']} | WAIT {ms['waiting_pairs']}"
        f" | scan {report['scan']['duration_s']}s, {report['scan']['mcp_calls']} MCP calls",
        "",
    ]
    if not report["signals"]:
        lines.append(c(DIM, "No setup met the BUY/SELL criteria. Nothing is forced — see the watchlist below."))
    else:
        hdr = f"{'#':>2} {'PAIR':<7} {'DIR':<4} {'TYPE':<12} {'ENTRY':>10} {'STOP':>10} {'TP1':>10} {'TP2':>10} {'TP3':>10} {'R:R':>6} {'CONF':>4} {'LOTS':>5} STATUS"
        lines.append(c(BOLD, hdr))
        for s in report["signals"]:
            col = GREEN if s["direction"] == "BUY" else RED
            lots = (s.get("position_size") or {}).get("lots")
            alloc = "" if (s.get("portfolio") or {}).get("allocated") else " (not allocated)"
            lines.append(c(col, f"{s['rank']:>2} {s['symbol']:<7} {s['direction']:<4} {s['signal_type']:<12} "
                                f"{s['entry_price']:>10} {s['stop_loss']:>10} {s['take_profit_1']:>10} {s['take_profit_2']:>10} "
                                f"{s['take_profit_3']:>10} {s['risk_reward']:>6} {s['confidence_score']:>4} {str(lots):>5} {s['status']}{alloc}"))
            lines.append(c(DIM, f"     {s['market_structure']} | align {s['timeframe_alignment']} | {s['reason']}"))
    lines.append("")
    if report.get("outlook"):
        lines.append(c(BOLD, "MARKET OUTLOOK — every pair, all timeframes (strongest lean first)"))
        for o in report["outlook"]:
            col = GREEN if o["lean"].endswith("BULLISH") else RED if o["lean"].endswith("BEARISH") else DIM
            tfs = " ".join(f"{tf}:{(v['bias'] or '-')[:4]}" for tf, v in o["timeframes"].items())
            lines.append(c(col, f"  {o['symbol']:<7} {o['lean']:<15} {o['lean_score']:>4}  {o['action']:<6} {tfs}"))
            lines.append(c(DIM, f"          {o['summary']}"))
            if o.get("alert_levels"):
                lines.append(c(DIM, "          alerts: " + "; ".join(f"{a['price']} ({a['why']})" for a in o["alert_levels"])))
    else:
        lines.append(c(BOLD, "WATCHLIST / WAIT"))
        for w in report["watchlist"]:
            score = "" if w.get("confidence_score") is None else f" score {w['confidence_score']}"
            lines.append(c(DIM, f"  {w['symbol']:<7} {str(w.get('bias') or '-'):<8} {w.get('wait_code') or '':<17}{score:<10} {w.get('reason')}"))
    if report.get("currency_strength"):
        lines.append("")
        lines.append(c(BOLD, "CURRENCY STRENGTH (avg % vs other currencies, ~1 day / ~1 week)"))
        lines.append("  " + "  ".join(f"{r['currency']} {r['day_pct']:+.2f}/{r['week_pct']:+.2f}" for r in report["currency_strength"]
                                     if r["day_pct"] is not None and r["week_pct"] is not None))
        for p in report.get("strength_pairings", []):
            lines.append(c(DIM, f"  {p['note']}"))
    tr = report.get("track_record")
    if tr and (tr["closed_trades"] or tr["open"] or tr["awaiting_fill"]):
        lines.append("")
        lines.append(c(BOLD, "TRACK RECORD") + f"  closed {tr['closed_trades']} | TP2 {tr['wins_tp2']} | stop {tr['losses_stop']}"
                     f" | win {tr['win_rate_pct']}% | avg {tr['avg_r']}R | open {tr['open']} | awaiting fill {tr['awaiting_fill']}")
    ev = ms["high_impact_events"]
    if ev:
        lines.append("")
        lines.append(c(BOLD, "HIGH-IMPACT EVENTS"))
        for e in ev[:10]:
            flag = " ⚠ within 60 min" if e.get("within_60_min") else ""
            lines.append(f"  {e['date'][:16].replace('T', ' ')} UTC  {e['currency']}  {e['title']}{flag}")
    if report.get("limitations"):
        lines.append("")
        lines.append(c(DIM, "Limitations: " + " | ".join(report["limitations"][:3])))
    return "\n".join(lines)
