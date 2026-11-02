"""Weekly backtest run by the scheduler on the weekend, with the result summary sent to Telegram."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .backtest import DEFAULT_HISTORY, run_backtest
from .config import all_pairs
from .history import atomic_write_json
from .logging_setup import log
from .notify import Telegram

logger = logging.getLogger("fxsignals.weekly")


def due(cfg: dict, now: datetime) -> Optional[str]:
    """ISO week key when the weekly backtest should run now, else None."""
    bs = cfg.get("backtest_schedule") or {}
    if not bs.get("enabled"):
        return None
    local = now.astimezone(ZoneInfo(cfg["scan"].get("scan_timezone", "UTC")))
    if local.strftime("%a") != bs.get("day", "Sat") or local.hour < int(bs.get("hour", 10)):
        return None
    week = f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"
    state = Path(cfg["paths"]["state_dir"]) / "backtest_schedule.json"
    last = json.loads(state.read_text()).get("week") if state.exists() else None
    return None if last == week else week


def summary_message(res: dict) -> str:
    rows = []
    for mode, s in res["overall"].items():
        rows.append(f"<b>{escape(mode.title())}</b>: {s['trades']} trades · win {s['win_rate_pct']}% · "
                    f"avg {s['expectancy_r']}R · total {s['total_r']}R · PF {s['profit_factor']} · max DD {s['max_drawdown_r']}R")
    o = res["overall"]
    verdict = ""
    if {"strict", "adaptive"} <= set(o) and o["strict"]["trades"] and o["adaptive"]["trades"]:
        better = "adaptive (new)" if (o["adaptive"]["expectancy_r"] or -9) > (o["strict"]["expectancy_r"] or -9) else "strict (old)"
        verdict = f"\nHigher average result per trade: <b>{better}</b> rules."
    meaningful = all(s["statistically_meaningful"] for s in o.values())
    caveat = "" if meaningful else "\n⚠️ Fewer than 100 trades in at least one set — treat as indicative only."
    return ("📊 <b>Weekly backtest</b> (H1/H4/D1, ~10 months, spread + slippage included)\n\n"
            + "\n".join(rows) + verdict + caveat + "\n\nDetails on the dashboard. Past results do not guarantee future results.")


def run_if_due(cfg: dict) -> bool:
    week = due(cfg, datetime.now(timezone.utc))
    if not week:
        return False
    state = Path(cfg["paths"]["state_dir"]) / "backtest_schedule.json"
    tg = Telegram(cfg)
    log(logger, logging.INFO, "weekly backtest started", week=week)
    tg.send("📊 Weekly backtest started — results in about 20–30 minutes.")
    bs = cfg.get("backtest_schedule") or {}
    try:
        res = asyncio.run(run_backtest(cfg, all_pairs(cfg), targets={**DEFAULT_HISTORY, "H1": int(bs.get("h1_bars", 5000))}))
    except Exception as e:  # noqa: BLE001 - report, then try again next week
        log(logger, logging.ERROR, "weekly backtest failed", error=str(e))
        tg.send(f"⚠️ Weekly backtest failed: {escape(str(e)[:200])}")
        atomic_write_json(state, {"week": week, "status": "failed", "at": datetime.now(timezone.utc).isoformat()})
        return True
    atomic_write_json(state, {"week": week, "status": "done", "at": datetime.now(timezone.utc).isoformat(), "file": res["_path"]})
    tg.send(summary_message(res))
    log(logger, logging.INFO, "weekly backtest finished", file=res["_path"])
    return True
