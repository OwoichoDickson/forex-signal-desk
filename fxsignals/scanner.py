"""Scan orchestration: lock, fetch, analyse, rank, size, record, report."""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import filelock
from . import fundamentals as fx_fund
from .config import all_pairs
from .history import SignalHistory, atomic_write_json, effective_status
from .instruments import RateBook
from .logging_setup import log
from .market_data import TradingViewData, data_freshness
from .market_hours import is_instrument_open, market_status
from .mcp_client import TradingViewMCP
from . import taken as taken_store
from .monitor import events_for, review_open_signals
from .notify import Telegram
from .outlook import build_outlook, currency_strength, strength_pairings
from .ranking import rank_signals
from .report import build_report, write_chart_data, write_report
from .account import load_account, resize_signals
from .risk import currency_exposure, position_size
from .signals import TIMEFRAMES, Quote, analyse_pair

logger = logging.getLogger("fxsignals.scan")


class ScanBusy(RuntimeError):
    pass


@contextmanager
def scan_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    f = open(state_dir / "scan.lock", "w")
    try:
        try:
            filelock.lock(f, blocking=False)
        except BlockingIOError:
            raise ScanBusy("Another scan is already running")
        yield
    finally:
        try:
            filelock.unlock(f)
        finally:
            f.close()


def should_skip(cfg: dict, now: datetime, force: bool) -> Optional[str]:
    if force:
        return None
    state = Path(cfg["paths"]["state_dir"]) / "last_scan.json"
    if state.exists():
        last = json.loads(state.read_text()).get("finished_at_ts", 0)
        gap = cfg["scan"]["min_seconds_between_scans"]
        if now.timestamp() - last < gap:
            return f"last scan finished {now.timestamp() - last:.0f}s ago (< {gap}s); use --force to override"
    return _day_or_market_reason(cfg, now)


def _day_or_market_reason(cfg: dict, now: datetime) -> Optional[str]:
    days = cfg["scan"].get("scan_days")
    if days:
        local = now.astimezone(ZoneInfo(cfg["scan"].get("scan_timezone", "UTC")))
        if local.strftime("%a") not in days:
            return f"{local:%A} is not a scan day (scan_days: {', '.join(days)})"
    if market_status(now) == "CLOSED" and not cfg["scan"]["scan_when_market_closed"]:
        return "forex market is closed (scan_when_market_closed: false)"
    return None


def scan_day_and_market_ok(cfg: dict, when: datetime) -> bool:
    return _day_or_market_reason(cfg, when) is None


async def fetch_market_data(cfg: dict, pairs: list[str], now: datetime) -> dict:
    """All TradingView access for one scan. Restores the user's chart afterwards."""
    async with TradingViewMCP(cfg) as mcp:
        data = TradingViewData(mcp, cfg)
        health = await data.health()
        original = await data.begin_chart_session()
        try:
            tickers = await data.resolve_symbols(pairs)
            quotes = await data.screener(tickers)
            events = await data.calendar(now)
            news = await data.headlines(tickers)
            candles = await data.load_candles(tickers, TIMEFRAMES)
        finally:
            if cfg["data"]["restore_chart_after_scan"]:
                await data.end_chart_session(original)
        return {"health": health, "tickers": tickers, "quotes": quotes, "events": events, "news": news,
                "candles": candles, "limitations": data.limitations, "mcp_calls": mcp.calls}


def analyse_market(cfg: dict, raw: dict, now: datetime, history: SignalHistory) -> dict:
    pairs = list(raw["tickers"])
    events = fx_fund.parse_events(raw["events"]) if raw["events"] is not None else None
    fcfg = cfg["news_filter"]
    results = []
    prices: dict[str, float] = {}
    for pair in pairs:
        open_now = is_instrument_open(pair, now)
        cs = {tf: (c.closed_only(now.timestamp()) if c is not None else None) for tf, c in raw["candles"][pair].items()}
        q: Optional[Quote] = raw["quotes"].get(pair)
        if q is None and cs.get("M5") is not None:
            q = Quote(last=raw["candles"][pair]["M5"].c[-1])
        if q and q.last:
            prices[pair] = q.last
        fund = fx_fund.analyse_pair(pair, events, raw["news"], now, fcfg["blackout_minutes_before"],
                                    fcfg["blackout_minutes_after"], fcfg["caution_hours"])
        stale = data_freshness(raw["candles"][pair], now, open_now, cfg["data"]["max_data_age_minutes"])
        if stale:
            res = {"symbol": pair, "direction": "WAIT", "wait_code": "STALE_DATA", "reason": stale,
                   "economic_risk": fund.economic_risk, "headlines": fund.headlines, "details": {}}
        else:
            res = analyse_pair(pair, cs, q, fund, cfg, now, open_now)
        res["tradingview_symbol"] = raw["tickers"][pair]
        res["live"] = None if q is None else {"last": q.last, "bid": q.bid, "ask": q.ask, "change_pct": _round(q.change_pct, 3),
                                              "feed": _feed_label(q.update_mode), "tv_ratings": q.ratings or None}
        day = raw["candles"][pair].get("D1")  # includes the current (possibly forming) session bar
        if day is not None and len(day) >= 2:
            live = res["live"] or {}
            live.update(day_open=day.o[-1], day_high=day.h[-1], day_low=day.l[-1], previous_close=day.c[-2],
                        session_bar_time=datetime.fromtimestamp(day.t[-1], timezone.utc).isoformat())
            if live.get("change_pct") is None and day.c[-2]:
                live["change_pct"] = round((day.c[-1] / day.c[-2] - 1) * 100, 3)
            res["live"] = live
        results.append(res)

    m5 = {p: raw["candles"][p].get("M5") for p in pairs}
    h1 = {p: raw["candles"][p].get("H1") for p in pairs if raw["candles"][p].get("H1") is not None}
    history.refresh(prices, now, bars=m5, confirm=h1)
    review_open_signals(history, results, now)  # cancel broken pending setups, flag warnings on running trades
    r = load_account(cfg)  # balance/risk entered in the dashboard override config.yaml
    book = RateBook(prices)
    min_conf, top_n = cfg["signals"]["min_confidence"], cfg["signals"]["top_n"]
    scalps = [s for s in (res.pop("scalp", None) for res in results) if s]
    hold_back(results, history, now, cfg["signals"])
    ranked = rank_signals(results, min_conf, top_n)
    if scalps:  # ⚡ short trades: ranked on their own, checked only against other scalps
        sc = cfg.get("scalp") or {}
        hold_back(scalps, history, now, cfg["signals"], style="SCALP")
        ranked += rank_signals(scalps, sc.get("min_confidence", 70), sc.get("top_n", 5))
    for s in ranked:
        # pip value + stop pips are always stored; lots are only kept when an amount was entered
        s["position_size"] = position_size(s["symbol"], s["entry_price"], s["stop_loss"], r["account_balance"] or 100.0,
                                           r["risk_per_trade_pct"], r["account_currency"], book,
                                           r["lot_step"], r["min_lot"])
    account_summary = resize_signals(ranked, r)
    for s in ranked:
        reg = history.register(s, now)
        for k in ("signal_id", "created_at", "expires_at", "duplicate_of_existing", "scan_count"):
            s[k] = reg.get(k)
        s["status"] = effective_status(s, now)
        s["actionable"] = s["status"] == "ACTIVE" and bool((s.get("portfolio") or {}).get("allocated"))
    ranked_ids = {id(s) for s in ranked}
    watch = [x for x in results if id(x) not in ranked_ids]
    for w in watch:
        if w.get("direction") in ("BUY", "SELL"):  # valid signal outside the top N
            w["wait_code"], w["reason"] = "OUTSIDE_TOP_N", f"Valid {w['direction']} signal ranked outside the top {top_n}"
    watch.sort(key=lambda x: -(x.get("confidence_score") or 0))
    outlooks = {x["symbol"]: build_outlook(x) for x in results}
    strength = currency_strength(raw["candles"])
    return {"ranked": ranked, "watchlist": watch, "results": results, "events": events,
            "account": account_summary, "risk": r,
            "exposure": currency_exposure(ranked, r["risk_per_trade_pct"]),
            "outlooks": sorted(outlooks.values(), key=lambda o: -abs(o["lean_score"])),
            "currency_strength": strength,
            "strength_pairings": strength_pairings(strength, pairs, outlooks),
            "track_record": history.track_record()}


def hold_back(results: list[dict], history: SignalHistory, now: datetime, sig_cfg: dict, style: str = "INTRADAY") -> None:
    """Turn BUY/SELL results into WAIT when the pair already has a running trade, the same idea was just
    stopped out, or it is outside the trading hours. The setup is kept under details.proposed."""
    hours = sig_cfg.get("trade_hours_utc")
    cooldown = float(sig_cfg.get("cooldown_after_loss_hours", 0))
    for res in results:
        if res.get("direction") not in ("BUY", "SELL"):
            continue
        why = history.blocked(res["symbol"], res["direction"], now, cooldown, style)
        if why is None and hours and not hours[0] <= now.hour < hours[1]:
            why = ("OUTSIDE_TRADING_HOURS", f"New signals only {hours[0]:02d}:00–{hours[1]:02d}:00 UTC (London/New York); "
                                            f"the Asian session lost every trade so far")
        if why is None:
            continue
        proposed = {k: res.get(k) for k in ("direction", "signal_type", "entry_price", "stop_loss", "take_profit_1",
                                             "take_profit_2", "take_profit_3", "risk_reward", "confidence_score")}
        res["details"] = {**(res.get("details") or {}), "proposed": proposed}
        res.update(direction="WAIT", wait_code=why[0], reason=why[1],
                   bias="BULLISH" if proposed["direction"] == "BUY" else "BEARISH")


def _round(v, d):
    return None if v is None else round(v, d)


def _feed_label(mode: Optional[str]) -> str:
    if mode == "streaming":
        return "REAL_TIME"
    if mode and "delayed" in mode:
        return "DELAYED"
    if mode == "endofday":
        return "END_OF_DAY"
    return "UNKNOWN"


def notify_history(cfg: dict, history: SignalHistory, ranked: list[dict]) -> None:
    """Telegram: every change to every signal, each exactly once (new, entry reached, progress, warnings,
    cancellation, result) — whether or not the user took it. Sent keys live on the history record.

    While Telegram is not configured, events are marked as handled so that enabling it later does not
    replay old ones. send() queues undeliverable messages, so an event is handled once handed over.
    """
    tg = Telegram(cfg)
    current = {s.get("signal_id"): s for s in ranked}
    url = cfg["dashboard"].get("public_url")
    answers = taken_store.load(cfg["paths"]["state_dir"])
    tz = cfg["scan"].get("scan_timezone", "UTC")
    remind = int(cfg["signals"].get("remind_before_end_minutes", 30))
    now = datetime.now(timezone.utc)
    for rec in history.records:
        sent = set(rec.get("notified_events") or [])
        if rec.pop("notified_new", None):  # flags from before per-event tracking
            sent.add("new")
        if rec.pop("notified_outcome", None):
            sent.add("final")
        view = {**rec, **{k: v for k, v in current.get(rec.get("signal_id"), {}).items()
                          if k in ("position_size", "status", "portfolio")}}
        answer = taken_store.status(answers.get(rec.get("signal_id")))
        neutral = {k: t for k, t, _ in events_for(view, None, url, frozenset(sent), now, tz, remind)}  # followers' view
        for key, text, buttons in events_for(view, answer, url, frozenset(sent), now, tz, remind):
            if key in sent:
                continue
            if tg.enabled:
                tg.send(text, buttons)
                if key in neutral:
                    tg.broadcast(neutral[key], rec.get("signal_id"), f"{rec.get('symbol')} {rec.get('direction')}")
            sent.add(key)
        rec["notified_events"] = sorted(sent)


async def run_scan(cfg: dict, force: bool = False, pairs: Optional[list[str]] = None) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    skip = should_skip(cfg, now, force)
    if skip:
        log(logger, logging.INFO, "scan skipped", reason=skip)
        return None
    state_dir = Path(cfg["paths"]["state_dir"])
    with scan_lock(state_dir):
        t0 = time.monotonic()
        pairs = pairs or all_pairs(cfg)
        log(logger, logging.INFO, "scan started", pairs=len(pairs), market=market_status(now))
        raw = await fetch_market_data(cfg, pairs, now)
        history = SignalHistory(state_dir / "signal_history.json", cfg["signals"]["expiry_minutes"])
        analysis = analyse_market(cfg, raw, now, history)
        report = build_report(cfg, now, raw, analysis, time.monotonic() - t0)
        path = write_report(cfg, report, now)
        write_chart_data(cfg, raw, analysis)
        notify_history(cfg, history, analysis["ranked"])
        history.save()
        atomic_write_json(state_dir / "last_scan.json", {"finished_at_ts": time.time(), "report": str(path)})
        log(logger, logging.INFO, "scan finished", file=str(path), signals=len(analysis["ranked"]),
            duration_s=round(time.monotonic() - t0, 1), mcp_calls=raw["mcp_calls"])
        report["_path"] = str(path)
        return report
