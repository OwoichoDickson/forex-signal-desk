"""Fixes from the 2–4 Nov review: break-even after TP1, no repeat signals, cooldown, trading hours, stricter enter-now."""
from datetime import datetime, timedelta, timezone

from fxsignals.history import SignalHistory
from fxsignals.models import Candles
from fxsignals.scanner import hold_back

WED = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
SIG = {"symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000, "stop_loss": 1.0980,
       "take_profit_1": 1.1040, "take_profit_2": 1.1060, "take_profit_3": 1.1080, "atr": 0.001, "status": "ACTIVE"}
CFG = {"trade_hours_utc": [7, 16], "cooldown_after_loss_hours": 4}


def _m5(rows, start):
    return Candles("EURUSD", "M5", [start + i * 300 for i in range(len(rows))],
                   [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [0] * len(rows))


def _hist(tmp_path):
    return SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})


def test_back_to_entry_after_tp1_is_breakeven_not_loss(tmp_path):
    h = _hist(tmp_path)
    h.register(dict(SIG), WED)
    bars = _m5([(1.1, 1.1045, 1.0999, 1.1040),   # TP1 reached
                (1.104, 1.1041, 1.0975, 1.0978)],  # falls through the entry AND the original stop
               int(WED.timestamp()))
    h.refresh({}, WED + timedelta(minutes=10), bars={"EURUSD": bars})
    r = h.records[0]
    assert r["outcome"] == "BREAKEVEN" and r["result_r"] == 0.0
    tr = h.track_record()
    assert tr["breakeven"] == 1 and tr["losses_stop"] == 0 and tr["closed_trades"] == 1


def test_tp1_and_stop_in_the_same_bar_is_still_a_loss(tmp_path):
    h = _hist(tmp_path)
    h.register(dict(SIG), WED)
    bars = _m5([(1.1, 1.1045, 1.0975, 1.0978)], int(WED.timestamp()))  # can't know TP1 came first: conservative
    h.refresh({}, WED + timedelta(minutes=5), bars={"EURUSD": bars})
    assert h.records[0]["outcome"] == "LOSS_STOP" and h.records[0]["result_r"] == -1.0


def _buy(symbol="EURUSD", direction="BUY"):
    return {**SIG, "symbol": symbol, "direction": direction, "confidence_score": 80, "details": {}}


def test_no_new_signal_while_a_trade_on_the_pair_runs(tmp_path):
    h = _hist(tmp_path)
    h.register(dict(SIG), WED)
    h.records[0].update(outcome="OPEN", fill_price=1.1)
    res = [_buy(), _buy(direction="SELL"), _buy("GBPUSD")]
    hold_back(res, h, WED + timedelta(hours=2), CFG)
    assert [r["direction"] for r in res] == ["WAIT", "WAIT", "BUY"]
    assert res[0]["wait_code"] == "ALREADY_RUNNING" and res[0]["details"]["proposed"]["direction"] == "BUY"


def test_cooldown_after_a_stop_out(tmp_path):
    h = _hist(tmp_path)
    h.register(dict(SIG), WED)
    h.records[0].update(outcome="LOSS_STOP", outcome_at=(WED + timedelta(hours=1)).isoformat())
    res = [_buy(), _buy(direction="SELL")]
    hold_back(res, h, WED + timedelta(hours=3), CFG)
    assert res[0]["wait_code"] == "COOLDOWN_AFTER_LOSS" and res[1]["direction"] == "SELL"  # only the same idea waits
    res = [_buy()]
    hold_back(res, h, WED + timedelta(hours=5, minutes=1), CFG)
    assert res[0]["direction"] == "BUY"


def test_no_new_signals_outside_london_new_york(tmp_path):
    h = _hist(tmp_path)
    night, day = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc), datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
    res = [_buy()]
    hold_back(res, h, night, CFG)
    assert res[0]["wait_code"] == "OUTSIDE_TRADING_HOURS"
    res = [_buy()]
    hold_back(res, h, day, CFG)
    assert res[0]["direction"] == "BUY"


def _h1(rows, start):
    return Candles("EURUSD", "H1", [start + i * 3600 for i in range(len(rows))],
                   [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [0] * len(rows))


def test_bad_m5_bar_not_confirmed_by_h1_is_ignored(tmp_path):
    h = _hist(tmp_path)
    h.register(dict(SIG), WED)
    t0 = int(WED.timestamp())
    m5 = _m5([(1.1, 1.1010, 1.0995, 1.1005), (1.1005, 1.1008, 1.0900, 1.1004)], t0)  # 2nd bar: spike far below the stop
    h1 = _h1([(1.1, 1.1012, 1.0993, 1.1004)], t0)  # the hour never went below 1.0993
    h.refresh({}, WED + timedelta(minutes=10), bars={"EURUSD": m5}, confirm={"EURUSD": h1})
    assert h.records[0]["outcome"] == "OPEN"
    h1 = _h1([(1.1, 1.1012, 1.0900, 1.1004)], t0)  # hour confirms the drop: a real stop-out
    h.refresh({}, WED + timedelta(minutes=10), bars={"EURUSD": m5}, confirm={"EURUSD": h1})
    assert h.records[0]["outcome"] == "LOSS_STOP"


def test_switched_off_setups_wait_but_scalps_are_unaffected(tmp_path):
    h = _hist(tmp_path)
    cfg = {**CFG, "disabled_setups": ["BULLISH_PULLBACK_TO_LEVEL"]}
    day = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
    res = [{**_buy(), "market_structure": "BULLISH_PULLBACK_TO_LEVEL"}, {**_buy("GBPUSD"), "market_structure": "BULLISH_BOS"}]
    hold_back(res, h, day, cfg)
    assert res[0]["wait_code"] == "SETUP_SWITCHED_OFF" and res[1]["direction"] == "BUY"
    scalp = [{**_buy("USDJPY"), "market_structure": "BULLISH_PULLBACK_TO_LEVEL"}]
    hold_back(scalp, h, day, cfg, style="SCALP")
    assert scalp[0]["direction"] == "BUY"
