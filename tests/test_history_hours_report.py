import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fxsignals.fundamentals import CalendarEvent, analyse_pair as fund_pair, filter_headlines
from fxsignals.history import SignalHistory, effective_status
from fxsignals.market_hours import is_fx_open, is_instrument_open, market_status, next_open
from fxsignals.ranking import rank_signals
from fxsignals.report import SCHEMA_SIGNAL_KEYS, _signal_out, format_terminal, refresh_statuses
from fxsignals.scanner import should_skip

WED = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
SAT = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)


def sig(**kw):
    s = {"symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000,
         "stop_loss": 1.0980, "take_profit_1": 1.1040, "take_profit_2": 1.1060, "take_profit_3": 1.1080,
         "atr": 0.0010, "status": "ACTIVE", "actionable": True, "confidence_score": 80, "risk_reward": "1:3.0"}
    s.update(kw)
    return s


# ── market hours ──────────────────────────────────────────────────────────────
def test_market_hours():
    assert not is_fx_open(SAT)
    assert market_status(SAT) == "CLOSED"
    assert is_fx_open(WED)
    sun_before = datetime(2026, 11, 1, 21, 59, tzinfo=timezone.utc)  # 16:59 New York (EST)
    sun_after = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)
    assert not is_fx_open(sun_before) and is_fx_open(sun_after)
    fri_close = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)  # 17:00 New York (EDT)
    assert not is_fx_open(fri_close)
    assert next_open(SAT) == sun_after
    gold_break = datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc)  # 17:30 New York
    assert is_fx_open(gold_break) and not is_instrument_open("XAUUSD", gold_break)


# ── history: expiry, duplicates, invalidation ────────────────────────────────
def test_expired_signal_never_active(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    rec = h.register(sig(), WED)
    assert effective_status(rec, WED + timedelta(minutes=59)) == "ACTIVE"
    assert effective_status(rec, WED + timedelta(minutes=61)) == "EXPIRED"
    report = {"signals": [dict(rec)]}
    refresh_statuses(report, WED + timedelta(hours=2))
    assert report["signals"][0]["status"] == "EXPIRED" and report["signals"][0]["actionable"] is False
    h.refresh({}, WED + timedelta(hours=2))
    assert h.records[0]["status"] == "EXPIRED"
    assert all(r["status"] != "ACTIVE" for r in h.recent(now=WED + timedelta(hours=2)))


def test_duplicate_signal_is_merged_and_expiry_not_extended(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    a = h.register(sig(), WED)
    b = h.register(sig(entry_price=1.1003, confidence_score=82), WED + timedelta(minutes=15))
    assert b["duplicate_of_existing"] and b["signal_id"] == a["signal_id"] and b["expires_at"] == a["expires_at"]
    assert len(h.records) == 1 and h.records[0]["confidence_score"] == 82 and h.records[0]["scan_count"] == 2
    c = h.register(sig(entry_price=1.1050), WED + timedelta(minutes=20))  # materially different entry
    assert not c["duplicate_of_existing"] and len(h.records) == 2


def test_opposite_signal_supersedes(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    h.register(sig(), WED)
    h.register(sig(direction="SELL", stop_loss=1.1020, take_profit_1=1.0960, take_profit_2=1.0940, take_profit_3=1.0920), WED)
    assert h.records[0]["status"] == "SUPERSEDED"


def test_stop_and_target_tracking(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60, "BUY_LIMIT": 240})
    h.register(sig(), WED)
    h.register(sig(symbol="GBPUSD", signal_type="BUY_LIMIT", entry_price=1.3000, stop_loss=1.2980,
                   take_profit_1=1.3040, take_profit_2=1.3060, take_profit_3=1.3080), WED)
    h.refresh({"EURUSD": 1.0975, "GBPUSD": 1.3045}, WED + timedelta(minutes=10))
    assert h.records[0]["status"] == "INVALIDATED"
    assert h.records[1]["status"] == "EXPIRED" and "missed" in h.records[1]["close_reason"]
    h.save()
    assert json.loads(Path(tmp_path / "h.json").read_text())["signals"][0]["status"] == "INVALIDATED"


def test_signal_created_on_weekend_expires_after_open(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    rec = h.register(sig(status="MARKET_CLOSED", actionable=False), SAT)
    assert datetime.fromisoformat(rec["expires_at"]) == next_open(SAT) + timedelta(minutes=60)
    assert effective_status(rec, next_open(SAT) + timedelta(minutes=5)) == "PENDING_REVALIDATION"


# ── ranking ───────────────────────────────────────────────────────────────────
def test_ranking_order_and_no_padding():
    res = [
        sig(symbol="A", confidence_score=70, details={"score_breakdown": {"higher_timeframe_alignment": 20}}),
        sig(symbol="B", confidence_score=80, details={"score_breakdown": {"higher_timeframe_alignment": 10}}),
        sig(symbol="C", confidence_score=80, details={"score_breakdown": {"higher_timeframe_alignment": 25}}),
        sig(symbol="D", confidence_score=60),
        {"symbol": "E", "direction": "WAIT"},
    ]
    ranked = rank_signals(res, 65, 10)
    assert [s["symbol"] for s in ranked] == ["C", "B", "A"]
    assert [s["rank"] for s in ranked] == [1, 2, 3]


# ── fundamentals ──────────────────────────────────────────────────────────────
def test_surprise_bias_and_inverse_titles():
    ev = [
        CalendarEvent(WED - timedelta(hours=3), "USD", "Non Farm Payrolls", 1, 120.0, 65.0, 29.0),  # USD +
        CalendarEvent(WED - timedelta(hours=3), "USD", "Unemployment Rate", 1, 4.4, 4.2, 4.2),      # USD -
        CalendarEvent(WED - timedelta(hours=2), "EUR", "CPI YoY", 0, 3.9, 3.9, 3.8),                 # no surprise
    ]
    f = fund_pair("EURUSD", ev, None, WED)
    assert f.bias == 0 and len(f.surprises) == 2
    assert {s["effect"] for s in f.surprises} == {"USD-positive surprise", "USD-negative surprise"}
    assert f.economic_risk == "LOW"
    f2 = fund_pair("EURUSD", ev[:1], None, WED)
    assert f2.bias < 0  # USD-positive -> bad for EURUSD longs


def test_calendar_unavailable_is_flagged():
    f = fund_pair("EURUSD", None, None, WED)
    assert not f.available and f.economic_risk is None


def test_upcoming_event_medium_risk():
    ev = [CalendarEvent(WED + timedelta(hours=3), "GBP", "BoE Interest Rate Decision", 1, None, 4.0, 3.75)]
    assert fund_pair("GBPUSD", ev, None, WED).economic_risk == "MEDIUM"
    assert fund_pair("EURUSD", ev, None, WED).economic_risk == "LOW"


def test_headline_filter_uses_currency_terms():
    ts = WED.timestamp() - 600
    items = [{"title": "Yen climbs after BOJ holds", "published": ts}, {"title": "US farm prices fall", "published": ts},
             {"title": "Dollar slips as Fed speakers turn dovish", "published": ts}]
    got = [h["title"] for h in filter_headlines(items, "USDJPY", WED)]
    assert got == ["Yen climbs after BOJ holds", "Dollar slips as Fed speakers turn dovish"]
    assert [h["title"] for h in filter_headlines(items, "AUDNZD", WED)] == []


# ── report schema & scan guards ───────────────────────────────────────────────
def test_signal_output_has_schema_keys_and_nulls():
    out = _signal_out(sig(rank=1, rsi=None))
    for k in SCHEMA_SIGNAL_KEYS:
        assert k in out
    assert out["rsi"] is None and out["headlines"] is None


def test_terminal_output_when_no_signals():
    rep = {"scanned_at": "x", "market_status": "CLOSED", "feed": {"mode": "REAL_TIME"}, "signals": [], "watchlist": [],
           "market_summary": {"pairs_analysed": 0, "bullish_pairs": 0, "bearish_pairs": 0, "waiting_pairs": 0, "high_impact_events": []},
           "scan": {"duration_s": 1, "mcp_calls": 1}, "limitations": []}
    assert "Nothing is forced" in format_terminal(rep, color=False)


def test_scan_days_monday_to_friday_only(cfg):
    sun_open = datetime(2026, 11, 1, 22, 30, tzinfo=timezone.utc)  # FX open, but Sunday in Lisbon
    assert is_fx_open(sun_open)
    assert "Sunday is not a scan day" in should_skip(cfg, sun_open, force=False)
    assert should_skip(cfg, sun_open, force=True) is None
    assert "Saturday is not a scan day" in should_skip(cfg, SAT, force=False)
    assert should_skip(cfg, WED, force=False) is None
    cfg["scan"]["scan_days"] = None  # without the day filter, the market-hours guard still applies
    assert "closed" in should_skip(cfg, SAT, force=False)


def test_scheduled_scan_skips_weekend_and_rapid_repeats(cfg, tmp_path):
    assert should_skip(cfg, SAT, force=False)
    assert should_skip(cfg, SAT, force=True) is None
    state = Path(cfg["paths"]["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "last_scan.json").write_text(json.dumps({"finished_at_ts": WED.timestamp() - 60}))
    assert "use --force" in should_skip(cfg, WED, force=False)
