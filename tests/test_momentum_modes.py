import copy

from fxsignals import structure as st
from fxsignals.fundamentals import PairFundamentals
from fxsignals.signals import Quote, decide

T = 1_790_000_000
Q = Quote(last=1.1000, bid=1.09995, ask=1.10005, update_mode="streaming")
NOFUND = PairFundamentals(available=True)
# H1 mid-pullback: RSI under 50 and MACD below its signal, as happens in a healthy pullback
PULLBACK_H1 = dict(rsi=44.0, macd=-0.0001, macd_signal=0.0001, hist=-0.0002, hist_prev=-0.0001)


def setup(helpers, h4_over=None, h1_over=None):
    tfa = helpers["tfa"]
    h1 = tfa("H1", 1.1000, "BULLISH", atr=0.0010, swings=[st.Swing(280, T, 1.0960, "L")], **{**PULLBACK_H1, **(h1_over or {})})
    h1.structure.order_blocks = [st.OrderBlock("BULLISH", 1.0982, 1.0990, T, "CHOCH", T + 3600, 2.0, mitigated=False)]
    return {"M15": tfa("M15", 1.1, "BULLISH", atr=0.0004), "H1": h1,
            "H4": tfa("H4", 1.1, "BULLISH", atr=0.0025, **(h4_over or {})), "D1": tfa("D1", 1.1, "BULLISH", atr=0.006)}


def test_adaptive_allows_order_block_pullback_when_h4_is_strong(cfg, helpers):
    r = decide("EURUSD", setup(helpers), Q, NOFUND, cfg, True)
    assert r["direction"] == "BUY" and r["signal_type"] == "BUY_LIMIT", r.get("reason")
    assert r["details"]["momentum_rules"].startswith("PULLBACK")
    assert r["details"]["score_breakdown"]["momentum"] > 0  # scored on H4, not the pulled-back H1


def test_strict_mode_blocks_the_same_pullback(cfg, helpers):
    c = copy.deepcopy(cfg)
    c["analysis"]["momentum_mode"] = "strict"
    r = decide("EURUSD", setup(helpers), Q, NOFUND, c, True)
    assert r["direction"] == "WAIT" and r["wait_code"] in ("RSI_MOMENTUM", "MACD_FILTER")
    assert "H1" in r["reason"]


def test_pullback_needs_strong_h4(cfg, helpers):
    r = decide("EURUSD", setup(helpers, h4_over={"rsi": 46.0}), Q, NOFUND, cfg, True)
    assert r["wait_code"] == "RSI_MOMENTUM" and "H4" in r["reason"]
    r = decide("EURUSD", setup(helpers, h4_over={"macd": 0.0001, "macd_signal": 0.0003, "hist": -0.0002}), Q, NOFUND, cfg, True)
    assert r["wait_code"] == "MACD_FILTER" and "H4" in r["reason"]
    r = decide("EURUSD", setup(helpers, h4_over={"rsi": 78.0}), Q, NOFUND, cfg, True)
    assert r["wait_code"] == "RSI_EXTREME"


def test_collapsing_h1_is_a_reversal_not_a_pullback(cfg, helpers):
    r = decide("EURUSD", setup(helpers, h1_over={"rsi": 24.0}), Q, NOFUND, cfg, True)
    assert r["wait_code"] == "RSI_MOMENTUM" and "reversal" in r["reason"]


def test_breakout_entries_keep_strict_h1_rules(cfg, helpers):
    s = setup(helpers)
    s["H1"].structure.order_blocks = []
    s["H1"].structure._swings.append(st.Swing(285, T, 1.0982, "L"))  # valid stop, so momentum decides
    s["M15"].structure.events = [st.Event("BOS", "BULLISH", 1.0995, T, 1, "close above swing high")]
    r = decide("EURUSD", s, Q, NOFUND, cfg, True)
    assert r["direction"] == "WAIT" and "H1" in r["reason"]
