"""Command-line entry point: health, scan, run (scheduler), show, dashboard, backtest."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import all_pairs, load_config
from .health import HealthMonitor, classify_error, data_problem, internet_ok
from .weekly import run_if_due as run_weekly_if_due
from .logging_setup import log, setup_logging
from .report import format_terminal
from .scanner import ScanBusy, run_scan

logger = logging.getLogger("fxsignals.cli")


def cmd_health(cfg, args) -> int:
    from .market_data import TradingViewData
    from .mcp_client import TradingViewMCP

    async def go():
        async with TradingViewMCP(cfg) as mcp:
            data = TradingViewData(mcp, cfg)
            h = await data.health()
            tickers = await data.resolve_symbols(args.pairs or all_pairs(cfg)[:3])
            quotes = await data.screener(tickers)
            print(json.dumps({"tradingview": {k: h.get(k) for k in ("cdp_connected", "api_available", "chart_symbol")},
                              "mcp_tools": len(mcp.tools), "symbols": tickers,
                              "quotes": {p: vars(q) for p, q in quotes.items()},
                              "limitations": data.limitations}, indent=2, default=str))
    asyncio.run(go())
    return 0


def cmd_scan(cfg, args) -> int:
    try:
        report = asyncio.run(run_scan(cfg, force=args.force, pairs=args.pairs))
    except ScanBusy as e:
        print(f"Scan not started: {e}", file=sys.stderr)
        return 2
    if report is None:
        print("Scan skipped (see logs). Use --force to run anyway.", file=sys.stderr)
        return 0
    print(format_terminal(report, color=sys.stdout.isatty()))
    print(f"\nReport: {report['_path']}")
    return 0


def write_schedule(cfg: dict, interval_s: float, wait_s: float) -> None:
    """Publish when the scheduler wakes next and when the next *real* scan will run, for the dashboard timer."""
    from datetime import timedelta
    from .history import atomic_write_json
    from .scanner import scan_day_and_market_ok
    now = datetime.now(timezone.utc)
    tick = now + timedelta(seconds=wait_s)
    nxt = tick
    for _ in range(int(7 * 86400 / interval_s) + 2):  # first tick that falls on a scan day with the market open
        if scan_day_and_market_ok(cfg, nxt):
            break
        nxt += timedelta(seconds=interval_s)
    atomic_write_json(Path(cfg["paths"]["state_dir"]) / "scheduler.json", {
        "interval_minutes": interval_s / 60, "updated_at": now.isoformat(),
        "next_tick_at": tick.isoformat(), "next_scan_at": nxt.isoformat(),
    })


def cmd_run(cfg, args) -> int:
    interval = (args.interval or cfg["scan"]["interval_minutes"]) * 60
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    log(logger, logging.INFO, "scheduler started", interval_min=interval / 60)
    health = HealthMonitor(cfg)
    while not stop["flag"]:
        started = time.monotonic()
        try:
            report = asyncio.run(run_scan(cfg))
            if report:
                print(format_terminal(report, color=sys.stdout.isatty()), flush=True)
                issue = data_problem(report)
                if issue:
                    health.problem(issue)
                else:
                    health.ok(f"{len(report['signals'])} signals")
            else:
                health.alive("scan skipped (not a scan day / too soon)")
        except ScanBusy as e:
            log(logger, logging.WARNING, "scan already running; skipping this tick", error=str(e))
            health.alive("scan already running")
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive, alert, retry next tick
            log(logger, logging.ERROR, "scan failed", error=str(e))
            health.problem(classify_error(str(e), internet_ok()))
        try:
            run_weekly_if_due(cfg)  # weekend only; blocks this loop for the duration (no scans on weekends anyway)
        except Exception as e:  # noqa: BLE001
            log(logger, logging.ERROR, "weekly backtest error", error=str(e))
        remaining = interval - (time.monotonic() - started)
        write_schedule(cfg, interval, max(0.0, remaining))
        # wall-clock deadline: time the Mac spends asleep counts, so an overdue scan runs right after wake
        deadline = time.time() + remaining
        while time.time() < deadline and not stop["flag"]:
            time.sleep(min(5, max(0.0, deadline - time.time())))
    log(logger, logging.INFO, "scheduler stopped")
    return 0


def cmd_show(cfg, args) -> int:
    out_dir = Path(cfg["paths"]["output_dir"])
    files = sorted(out_dir.glob("forex_signals_*.json"))
    if not files:
        print("No reports yet. Run: python -m fxsignals scan", file=sys.stderr)
        return 1
    report = json.loads(files[-1].read_text())
    print(format_terminal(report, color=sys.stdout.isatty(), now=datetime.now(timezone.utc)))
    return 0


def cmd_dashboard(cfg, args) -> int:
    from .dashboard.server import serve
    serve(cfg)
    return 0


def cmd_telegram_setup(cfg, args) -> int:
    """Link the bot to your Telegram chat: you message the bot first, then this finds and saves the chat."""
    from .notify import Telegram, heartbeat
    tg = Telegram(cfg)
    if not tg.token:
        print("TELEGRAM_BOT_TOKEN is not set. Add it to the .env file in the project folder (see .env.example).")
        return 1
    me = tg._call("getMe")
    if not me.get("ok"):
        print("Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in .env.")
        return 1
    bot = me["result"].get("username")
    chat = tg.discover_chat()
    if not chat:
        print(f"Open Telegram, search for @{bot}, press Start (or send any message), then run this command again.")
        return 1
    who = chat.get("username") or chat.get("first_name") or chat["id"]
    ok = tg.send(f"✅ <b>Forex Signal Desk connected</b>\nSignals, results and scanner warnings will arrive here, {who}.")
    heartbeat(True, "telegram setup")
    print(f"Linked to @{bot} → chat {who}. Test message {'sent' if ok else 'queued (no connection?)'}.")
    if not os.environ.get("HEALTHCHECK_URL"):
        print("Note: HEALTHCHECK_URL is not set, so there is no alert when the Mac is offline or asleep (see README).")
    return 0


def cmd_telegram_bot(cfg, args) -> int:
    from .bot import run
    run(cfg)
    return 0


def cmd_backtest(cfg, args) -> int:
    from .backtest import DEFAULT_HISTORY, run_backtest
    targets = {**DEFAULT_HISTORY, "H1": args.h1_bars} if args.h1_bars else None
    res = asyncio.run(run_backtest(cfg, args.pairs or all_pairs(cfg), targets=targets))
    cols = ("trades", "win_rate_pct", "expectancy_r", "total_r", "profit_factor", "max_drawdown_r")
    print(f"{'rules':<10}" + "".join(f"{c:>16}" for c in cols))
    for mode, s in res["overall"].items():
        print(f"{mode:<10}" + "".join(f"{str(s[c]):>16}" for c in cols))
    for mode, groups in res["by_setup"].items():
        print(f"\n{mode} — by setup")
        for setup, s in groups.items():
            print(f"  {setup:<40} trades {s['trades']:>4}  win {s['win_rate_pct']}%  avg {s['expectancy_r']}R  total {s['total_r']}R")
    print(f"\nFull results: {res['_path']}")
    return 0


def cmd_lab(cfg, args) -> int:
    from .lab import run_lab
    res = run_lab(cfg, args.pairs or all_pairs(cfg), refresh=args.refresh)
    p = res["period"]
    print(f"History {p['from'][:10]} → {p['to'][:10]} · walk-forward split {p['split'][:10]} · costs {res['costs']}\n")
    row = lambda s: (f"{s['trades']:>6} {str(s['win_rate_pct']):>6}% {str(s['expectancy_r']):>8} {s['total_r']:>8} "
                     f"{str(s['profit_factor']):>6} {s['max_drawdown_r']:>7}")
    print(f"{'rule set':<34}{'trades':>6} {'win':>7} {'avg R':>8} {'total R':>8} {'PF':>6} {'max DD':>7}   avg R 1st / 2nd half")
    for name, r in res["rulesets"].items():
        print(f"{name:<34}{row(r['overall'])}   {r['first_half']['expectancy_r']} / {r['second_half']['expectancy_r']}")
    c = res["setup_cut_walk_forward"]
    print(f"\nSetups cut (lost in the first half): {', '.join(c['setups_cut']) or 'none'}")
    print(f"  second half before cut: {row(c['before_cut']['second_half'])}")
    print(f"  second half after cut:  {row(c['after_cut']['second_half'])}")
    for dim in ("setup", "type", "direction", "daily_trend_agree", "hour_utc", "score", "h1_adx"):
        print(f"\nCurrent rules by {dim}:")
        for k, s in res["current_rules_by"][dim].items():
            print(f"  {k:<44}{row(s)}   {s['first_half']} / {s['second_half']}")
    print(f"\nLimits: {res['limitations'][0]}\nFull results: {res['_path']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fxsignals", description="Forex signal generator (analysis only, no trading).")
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health", help="verify TradingView MCP connectivity and live quotes")
    h.add_argument("--pairs", nargs="*")
    s = sub.add_parser("scan", help="run one scan now")
    s.add_argument("--force", action="store_true", help="ignore min-interval and market-closed guards")
    s.add_argument("--pairs", nargs="*", help="subset of pairs (default: all 20)")
    r = sub.add_parser("run", help="scan automatically every N minutes")
    r.add_argument("--interval", type=int, help="minutes between scans (default from config)")
    sub.add_parser("show", help="print the latest report")
    sub.add_parser("dashboard", help="start the web dashboard")
    lb = sub.add_parser("lab", help="strategy lab: which rules and setups make money (walk-forward, ~10 months)")
    lb.add_argument("--pairs", nargs="*")
    lb.add_argument("--refresh", action="store_true", help="reload history from TradingView instead of the cache")
    b = sub.add_parser("backtest", help="replay strict vs adaptive rules on months of TradingView history")
    b.add_argument("--pairs", nargs="*")
    b.add_argument("--h1-bars", type=int, help="H1 bars of history to load (default 5000 ≈ 10 months)")
    sub.add_parser("telegram-setup", help="link Telegram alerts to your chat and send a test message")
    sub.add_parser("telegram-bot", help="listen for 'I took it' / 'Skipped' taps and /open, /mytrades commands")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg["paths"]["log_dir"], cfg["logging"]["level"])
    return {"health": cmd_health, "scan": cmd_scan, "run": cmd_run, "show": cmd_show,
            "dashboard": cmd_dashboard, "backtest": cmd_backtest, "lab": cmd_lab,
            "telegram-setup": cmd_telegram_setup, "telegram-bot": cmd_telegram_bot}[args.cmd](cfg, args)
