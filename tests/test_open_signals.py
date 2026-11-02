from datetime import datetime, timedelta, timezone

from fxsignals.history import SignalHistory, display_status, still_valid
from fxsignals.report import merge_open_signals

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
LIMIT = {"symbol": "EURGBP", "direction": "SELL", "signal_type": "SELL_LIMIT", "entry_price": 0.8345, "stop_loss": 0.8361,
         "take_profit_1": 0.8313, "take_profit_2": 0.8297, "take_profit_3": 0.8281, "atr": 0.0006, "status": "PENDING",
         "confidence_score": 81, "risk_reward": "1:3.0"}
MARKET = {**LIMIT, "symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000,
          "stop_loss": 1.0980, "take_profit_1": 1.1040, "take_profit_2": 1.1060, "take_profit_3": 1.1080, "status": "ACTIVE"}


def hist(tmp_path):
    return SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60, "SELL_LIMIT": 240})


def test_pending_order_stays_until_expiry_then_moves_to_past(tmp_path):
    h = hist(tmp_path)
    rec = h.register(dict(LIMIT), T0)
    assert still_valid(rec, T0 + timedelta(hours=3)) and display_status(rec, T0 + timedelta(hours=3)) == "PENDING"
    assert not still_valid(rec, T0 + timedelta(hours=5))


def test_entered_trade_stays_while_running_even_after_entry_window(tmp_path):
    h = hist(tmp_path)
    h.register(dict(MARKET), T0)
    h.records[0].update(outcome="OPEN", fill_price=1.1, open_r=0.8)
    later = T0 + timedelta(hours=6)  # far past the 60-minute entry window
    assert still_valid(h.records[0], later) and display_status(h.records[0], later) == "RUNNING"
    h.records[0].update(outcome="WIN_TP2", result_r=3.0)
    assert not still_valid(h.records[0], later)


def test_merge_keeps_earlier_valid_signal_with_latest_scan_note(tmp_path):
    h = hist(tmp_path)
    h.register(dict(LIMIT), T0)
    report = {"signals": [], "watchlist": [{"symbol": "EURGBP", "wait_code": "HIGH_SPREAD",
                                            "reason": "Spread 1.3 pips is too large relative to the stop",
                                            "live": {"last": 0.83418}}]}
    merge_open_signals(report, h, T0 + timedelta(minutes=20))
    assert len(report["signals"]) == 1
    s = report["signals"][0]
    assert s["symbol"] == "EURGBP" and s["carried_over"] and not s["confirmed_by_latest_scan"]
    assert "Spread 1.3 pips" in s["latest_scan_note"] and s["live"]["last"] == 0.83418 and s["rank"] == 1


def test_newer_signal_replaces_older_unfilled_one(tmp_path):
    h = hist(tmp_path)
    old = h.register(dict(LIMIT), T0)
    new = h.register({**LIMIT, "entry_price": 0.83538, "stop_loss": 0.8362}, T0 + timedelta(minutes=80))
    assert not new["duplicate_of_existing"]
    o = next(r for r in h.records if r["signal_id"] == old["signal_id"])
    assert o["status"] == "SUPERSEDED" and o["replaced_by"] == new["signal_id"] and not still_valid(o, T0 + timedelta(minutes=81))
    report = {"signals": [], "watchlist": []}
    merge_open_signals(report, h, T0 + timedelta(minutes=81))
    assert [s["entry_price"] for s in report["signals"]] == [0.83538]  # one EURGBP row


def test_entered_trade_is_never_replaced(tmp_path):
    h = hist(tmp_path)
    old = h.register(dict(LIMIT), T0)
    h.records[0].update(outcome="OPEN", fill_price=0.8345)
    h.register({**LIMIT, "entry_price": 0.83538}, T0 + timedelta(minutes=80))
    assert next(r for r in h.records if r["signal_id"] == old["signal_id"]).get("replaced_by") is None


def test_existing_duplicates_are_cleaned_on_refresh(tmp_path):
    h = hist(tmp_path)
    h.records = [  # two open orders written before the rule existed
        {**LIMIT, "signal_id": "a", "created_at": T0.isoformat(), "expires_at": (T0 + timedelta(hours=4)).isoformat(), "outcome": "PENDING_FILL"},
        {**LIMIT, "signal_id": "b", "entry_price": 0.83538, "created_at": (T0 + timedelta(minutes=80)).isoformat(),
         "expires_at": (T0 + timedelta(hours=5)).isoformat(), "outcome": "PENDING_FILL"}]
    h.refresh({}, T0 + timedelta(minutes=90))
    assert h.records[0]["status"] == "SUPERSEDED" and h.records[1]["status"] == "PENDING"


def test_merge_marks_confirmed_and_drops_finished(tmp_path):
    h = hist(tmp_path)
    rec = h.register(dict(MARKET), T0)
    h.register(dict(LIMIT), T0)
    h.records[1].update(outcome="NOT_FILLED")  # finished: belongs in Past signals
    report = {"signals": [{**MARKET, "signal_id": rec["signal_id"]}], "watchlist": []}
    merge_open_signals(report, h, T0 + timedelta(minutes=10))
    assert [s["symbol"] for s in report["signals"]] == ["EURUSD"]
    assert report["signals"][0]["confirmed_by_latest_scan"] is True
