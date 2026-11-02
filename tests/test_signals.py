from datetime import datetime, timedelta, timezone

from fxsignals import structure as st
from fxsignals.fundamentals import CalendarEvent, PairFundamentals, analyse_pair as fund_pair
from fxsignals.signals import Quote, analyse_pair, decide

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)  # a Wednesday: market open
T = 1_790_000_000


def bull_setup(helpers, **h1_over):
    """H1/H4 bullish, fresh M15 bullish BOS, swing low under price, no resistance overhead."""
    tfa = helpers["tfa"]
    atr = 0.0010
    price = 1.1000
    trigger = st.Event("BOS", "BULLISH", 1.0995, T, 2, "close above swing high 1.0995")
    swings_h1 = [st.Swing(280, T, 1.0982, "L"), st.Swing(270, T, 1.0960, "L")]
    h1 = tfa("H1", price, "BULLISH", atr=atr, swings=swings_h1, **h1_over)
    h4 = tfa("H4", price, "BULLISH", atr=0.0025)
    d1 = tfa("D1", price, "BULLISH", atr=0.0060)
    m15 = tfa("M15", price, "BULLISH", atr=0.0004, events=[trigger], rsi=56)
    m5 = tfa("M5", price, "BULLISH", atr=0.0002)
    return {"M5": m5, "M15": m15, "H1": h1, "H4": h4, "D1": d1}


def bear_setup(helpers):
    tfa = helpers["tfa"]
    price = 1.1000
    trigger = st.Event("BREAKOUT_RETEST", "BEARISH", 1.1004, T, 1, "broke 1.1004, retested and held")
    swings_h1 = [st.Swing(280, T, 1.1018, "H")]
    kw = dict(rsi=42, macd=-0.0004, macd_signal=-0.0002, hist=-0.0002, hist_prev=-0.0001, trend="BEARISH")
    return {
        "M15": tfa("M15", price, "BEARISH", atr=0.0004, events=[trigger], **kw),
        "H1": tfa("H1", price, "BEARISH", atr=0.0010, swings=swings_h1, **kw),
        "H4": tfa("H4", price, "BEARISH", atr=0.0025, **kw),
        "D1": tfa("D1", price, "BEARISH", atr=0.0060, **kw),
    }


QUOTE = Quote(last=1.1000, bid=1.09995, ask=1.10005, update_mode="streaming")
NOFUND = PairFundamentals(available=True)


def test_buy_signal_with_levels_and_rr(cfg, helpers):
    r = decide("EURUSD", bull_setup(helpers), QUOTE, NOFUND, cfg, True)
    assert r["direction"] == "BUY", r.get("reason")
    assert r["signal_type"] == "MARKET_ENTRY" and r["status"] == "ACTIVE" and r["actionable"]
    entry, sl = r["entry_price"], r["stop_loss"]
    assert entry == 1.10005  # buys at the ask
    assert sl == round(1.0982 - 0.25 * 0.0010, 5)  # nearest swing low minus ATR buffer
    risk = entry - sl
    assert abs((r["take_profit_1"] - entry) / risk - 2.0) < 0.01
    assert (r["take_profit_2"] - entry) / risk >= 2.95
    assert r["take_profit_3"] > r["take_profit_2"] > r["take_profit_1"]
    assert r["risk_reward"] == "1:3.0"
    assert r["confidence_score"] >= 65
    assert set(r["details"]["score_breakdown"]) >= {"higher_timeframe_alignment", "technical_indicators", "market_structure", "momentum", "fundamental"}


def test_sell_signal(cfg, helpers):
    q = Quote(last=1.1000, bid=1.09995, ask=1.10005, update_mode="streaming")
    r = decide("EURUSD", bear_setup(helpers), q, NOFUND, cfg, True)
    assert r["direction"] == "SELL", r.get("reason")
    assert r["entry_price"] == 1.09995  # sells at the bid
    assert r["stop_loss"] > r["entry_price"] > r["take_profit_1"] > r["take_profit_2"] > r["take_profit_3"]
    assert r["market_structure"] == "BEARISH_BREAKOUT_RETEST"


def test_conflicting_timeframes_wait(cfg, helpers):
    s = bull_setup(helpers)
    s["H4"] = helpers["tfa"]("H4", 1.1, "BEARISH", trend="BEARISH")
    r = decide("EURUSD", s, QUOTE, NOFUND, cfg, True)
    assert r["direction"] == "WAIT" and r["wait_code"] == "CONFLICT"


def test_overbought_rsi_wait(cfg, helpers):
    r = decide("EURUSD", bull_setup(helpers, rsi=74), QUOTE, NOFUND, cfg, True)
    assert r["direction"] == "WAIT" and r["wait_code"] == "RSI_EXTREME"


def test_macd_against_wait(cfg, helpers):
    r = decide("EURUSD", bull_setup(helpers, macd=0.0001, macd_signal=0.0003, hist=-0.0002), QUOTE, NOFUND, cfg, True)
    assert r["wait_code"] == "MACD_FILTER"


def test_no_trigger_and_no_level_waits(cfg, helpers):
    s = bull_setup(helpers)
    s["M15"].structure.events = []
    r = decide("EURUSD", s, QUOTE, NOFUND, cfg, True)
    assert r["direction"] == "WAIT" and r["wait_code"] == "NO_TRIGGER"


def test_pending_buy_limit_at_demand_zone(cfg, helpers):
    s = bull_setup(helpers)
    s["M15"].structure.events = []
    s["H1"].structure.zones = [st.Zone("DEMAND", 1.0985, 1.0990, T)]
    s["H1"].structure._swings.append(st.Swing(285, T, 1.0975, "L"))
    r = decide("EURUSD", s, QUOTE, NOFUND, cfg, True)
    assert r["direction"] == "BUY" and r["signal_type"] == "BUY_LIMIT", r.get("reason")
    assert r["entry_price"] == 1.099 and r["status"] == "PENDING" and r["actionable"] is False
    assert r["stop_loss"] < 1.0985  # beyond the zone, not an arbitrary distance


def test_resistance_overhead_gives_poor_rr(cfg, helpers):
    s = bull_setup(helpers)
    s["H4"].structure.levels = [st.Level(1.1012, "RESISTANCE", 3, T)]
    r = decide("EURUSD", s, QUOTE, NOFUND, cfg, True)
    assert r["direction"] == "WAIT" and r["wait_code"] == "POOR_RR"


def test_stop_too_wide_waits(cfg, helpers):
    s = bull_setup(helpers)
    s["H1"].structure._swings = [st.Swing(280, T, 1.0950, "L")]  # 5 ATR away
    r = decide("EURUSD", s, QUOTE, NOFUND, cfg, True)
    assert r["wait_code"] == "STOP_TOO_WIDE"


def test_news_blackout_blocks_trade(cfg, helpers):
    ev = [CalendarEvent(NOW + timedelta(minutes=30), "USD", "Non Farm Payrolls", 1, None, 65.0, 29.0)]
    fund = fund_pair("EURUSD", ev, None, NOW)
    assert fund.economic_risk == "HIGH"
    r = decide("EURUSD", bull_setup(helpers), QUOTE, fund, cfg, True)
    assert r["direction"] == "WAIT" and r["wait_code"] == "NEWS_BLACKOUT"
    assert "Non Farm Payrolls" in r["reason"]


def test_event_for_unrelated_currency_does_not_block(cfg, helpers):
    ev = [CalendarEvent(NOW + timedelta(minutes=30), "JPY", "BoJ Rate Decision", 1, None, None, None)]
    fund = fund_pair("EURUSD", ev, None, NOW)
    r = decide("EURUSD", bull_setup(helpers), QUOTE, fund, cfg, True)
    assert r["direction"] == "BUY"


def test_high_spread_waits(cfg, helpers):
    q = Quote(last=1.1000, bid=1.0997, ask=1.1003, update_mode="streaming")  # 6 pips
    r = decide("EURUSD", bull_setup(helpers), q, NOFUND, cfg, True)
    assert r["wait_code"] == "HIGH_SPREAD"


def test_market_closed_is_not_actionable(cfg, helpers):
    r = decide("EURUSD", bull_setup(helpers), QUOTE, NOFUND, cfg, False)
    assert r["direction"] == "BUY" and r["status"] == "MARKET_CLOSED" and r["actionable"] is False
    assert any("Market closed" in w for w in r["details"]["warnings"])


def test_correlated_indicators_alone_cannot_make_strong_signal(cfg, helpers):
    s = bull_setup(helpers)
    s["M15"].structure.events = [st.Event("REJECTION", "BULLISH", 1.0999, T, 1, "bullish pin bar")]
    s["H1"].structure.levels = [st.Level(1.0999, "SUPPORT", 1, T)]  # pin at a level: weakest trigger
    s["H1"].structure.trend = "RANGING"
    s["H4"].structure.trend = "RANGING"
    r = decide("EURUSD", s, QUOTE, NOFUND, cfg, True)
    assert r.get("confidence_score") is None or r["confidence_score"] <= 74


def test_jpy_signal_levels_use_jpy_pips(cfg, helpers):
    tfa = helpers["tfa"]
    price, atr = 157.50, 0.20
    trig = st.Event("BOS", "BULLISH", 157.45, T, 1, "close above swing high")
    s = {"M15": tfa("M15", price, "BULLISH", atr=0.08, events=[trig]),
         "H1": tfa("H1", price, "BULLISH", atr=atr, swings=[st.Swing(280, T, 157.20, "L")]),
         "H4": tfa("H4", price, "BULLISH", atr=0.5), "D1": tfa("D1", price, "BULLISH", atr=1.2)}
    q = Quote(last=price, bid=157.495, ask=157.505, update_mode="streaming")
    r = decide("USDJPY", s, q, NOFUND, cfg, True)
    assert r["direction"] == "BUY"
    assert r["entry_price"] == 157.505 and r["stop_loss"] == 157.15
    assert r["details"]["stop_distance_pips"] == 35.5
    assert r["details"]["spread_pips"] == 1.0


def test_insufficient_data_waits(cfg, helpers):
    c = helpers["make_candles"](helpers["zigzag"](30))
    r = analyse_pair("EURUSD", {"H1": c, "H4": None}, None, NOFUND, cfg, NOW, True)
    assert r["direction"] == "WAIT" and r["wait_code"] == "INSUFFICIENT_DATA"


def test_price_mismatch_is_rejected(cfg, helpers):
    mk, zz = helpers["make_candles"], helpers["zigzag"]
    cs = {tf: mk(zz(300), tf) for tf in ("M5", "M15", "H1", "H4", "D1")}
    r = analyse_pair("EURUSD", cs, Quote(last=1.30), NOFUND, cfg, NOW, True)
    assert r["wait_code"] == "DATA_MISMATCH"


def test_end_to_end_on_synthetic_candles_never_contradicts_trend(cfg, helpers):
    mk, zz = helpers["make_candles"], helpers["zigzag"]
    cs = {tf: mk(zz(300), tf) for tf in ("M5", "M15", "H1", "H4", "D1")}
    r = analyse_pair("EURUSD", cs, None, NOFUND, cfg, NOW, True)
    assert r["direction"] in ("BUY", "WAIT")
    assert r["timeframe_bias" if r["direction"] == "WAIT" else "trend"] is not None
    if r["direction"] == "WAIT":
        assert r["timeframe_bias"]["H1"] == "BULLISH"
