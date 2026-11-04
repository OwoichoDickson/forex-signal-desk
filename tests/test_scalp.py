"""⚡ Scalp signals: H1+M15 direction, fresh M5 trigger, tight levels, closed after max_hold_minutes."""
from datetime import datetime, timedelta, timezone

from fxsignals import structure as st
from fxsignals.fundamentals import PairFundamentals
from fxsignals.history import SignalHistory
from fxsignals.models import Candles
from fxsignals.notify import fmt_signal
from fxsignals.scanner import hold_back
from fxsignals.signals import Quote, decide_scalp

T = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
QUOTE = Quote(last=1.1000, bid=1.09998, ask=1.10002, update_mode="streaming")


def setup(helpers, m15_bias="BULLISH", trigger_ago=2, m15_rsi=58.0, ema20_m15=None):
    tfa = helpers["tfa"]
    ev = st.Event("BOS", "BULLISH", 1.0997, int(T.timestamp()), trigger_ago, "close above M5 swing high")
    m15 = tfa("M15", 1.1000, m15_bias, atr=0.0006, rsi=m15_rsi, ema20=ema20_m15,
              swings=[st.Swing(290, T, 1.0995, "L")])
    return {"M5": tfa("M5", 1.1000, "BULLISH", atr=0.0002, events=[ev]), "M15": m15,
            "H1": tfa("H1", 1.1000, "BULLISH"), "H4": tfa("H4", 1.1000, "BULLISH", atr=0.0025)}


def test_scalp_buy_has_tight_levels_and_a_time_limit(cfg, helpers):
    s = decide_scalp("EURUSD", setup(helpers), QUOTE, PairFundamentals(available=True), cfg, True)
    assert s and s["style"] == "SCALP" and s["direction"] == "BUY" and s["signal_type"] == "MARKET_ENTRY"
    risk = s["entry_price"] - s["stop_loss"]
    assert 0 < risk <= 1.5 * 0.0006 + 1e-9  # within max_sl_atr x ATR(M15)
    assert abs((s["take_profit_2"] - s["entry_price"]) / risk - 1.5) < 0.02
    assert s["max_hold_minutes"] == 120 and s["entry_minutes"] == 15
    assert "close it within 120 minutes" in fmt_signal(s, None)


def test_no_scalp_without_agreement_trigger_or_when_chasing(cfg, helpers):
    fund = PairFundamentals(available=True)
    assert decide_scalp("EURUSD", setup(helpers, m15_bias="BEARISH"), QUOTE, fund, cfg, True) is None
    assert decide_scalp("EURUSD", setup(helpers, trigger_ago=12), QUOTE, fund, cfg, True) is None  # stale M5 trigger
    assert decide_scalp("EURUSD", setup(helpers, m15_rsi=74), QUOTE, fund, cfg, True) is None    # overbought
    assert decide_scalp("EURUSD", setup(helpers, ema20_m15=1.1000 - 1.3 * 0.0006), QUOTE, fund, cfg, True) is None


def _m5(rows, start):
    return Candles("EURUSD", "M5", [start + i * 300 for i in range(len(rows))],
                   [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [0] * len(rows))


SCALP = {"symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000, "stop_loss": 1.0995,
         "take_profit_1": 1.1005, "take_profit_2": 1.10075, "take_profit_3": 1.1010, "atr": 0.0006, "status": "ACTIVE",
         "style": "SCALP", "max_hold_minutes": 120, "entry_minutes": 15}


def test_scalp_closed_at_the_time_limit(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    h.register(dict(SCALP), T)
    assert h.records[0]["key"].startswith("SCALP|") and h.records[0]["expires_at"] == (T + timedelta(minutes=15)).isoformat()
    bars = _m5([(1.1, 1.1003, 1.0998, 1.1002)] * 24 + [(1.1002, 1.1003, 1.1000, 1.1001)], int(T.timestamp()))
    h.refresh({}, T + timedelta(minutes=125), bars={"EURUSD": bars})
    r = h.records[0]
    assert r["outcome"] == "TIMEOUT" and r["exit_price"] == 1.1002 and r["result_r"] == 0.4


def test_scalps_and_normal_signals_do_not_block_each_other(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    h.register(dict(SCALP), T)
    h.records[0].update(outcome="OPEN", fill_price=1.1)
    normal = {**SCALP, "style": None, "confidence_score": 80, "details": {}}
    scalp = {**SCALP, "confidence_score": 80, "details": {}}
    cfg = {"trade_hours_utc": [7, 16], "cooldown_after_loss_hours": 4}
    hold_back([normal], h, T, cfg)
    hold_back([scalp], h, T, cfg, style="SCALP")
    assert normal["direction"] == "BUY" and scalp["wait_code"] == "ALREADY_RUNNING"
    h.register({**SCALP, "style": None, "stop_loss": 1.098}, T)  # a normal signal on the same pair keeps the scalp
    assert h.records[0].get("status") != "SUPERSEDED" and len(h.records) == 2
