from fxsignals.backtest import DEFAULT_COSTS
from fxsignals.entry_test import simulate
from fxsignals.models import Candles

CFG = {"signals": {"min_risk_reward": 2.0, "expiry_minutes": {"BUY_LIMIT": 240, "MARKET_ENTRY": 60}}}
COSTS = {**DEFAULT_COSTS, "spread_pips": 0.0, "slippage_pips": 0.0}


def bars(rows):
    t = [3600 * k for k in range(len(rows))]
    o, h, l, c = zip(*rows)
    return Candles("EURUSD", "H1", t, list(o), list(h), list(l), list(c), [0] * len(rows))


# Buy limit at 1.1000 (stop 1.0980, target 1.1060, ATR 20 pips). Price dips to 1.1004 only, then rallies to target.
H1 = bars([(1.1010, 1.1012, 1.1008, 1.1010), (1.1008, 1.1011, 1.1004, 1.1008), (1.1008, 1.1030, 1.1007, 1.1028),
           (1.1028, 1.1062, 1.1027, 1.1060), (1.1060, 1.1061, 1.1050, 1.1055)])
SIG = [{"i": 0, "time": "t", "direction": "BUY", "type": "BUY_LIMIT", "entry": 1.1000, "sl": 1.0980, "tp": 1.1060,
        "tp1": 1.1040, "atr": 0.0020, "setup": "X"}]


def test_at_level_misses_but_front_run_fills_and_wins():
    at = simulate("EURUSD", H1, SIG, "at_level", CFG, COSTS)
    assert at["trades"] == [] and at["missed"] == 1 and at["missed_would_have_won"] == 1
    fr = simulate("EURUSD", H1, SIG, "front_0.25atr", CFG, COSTS)  # entry 1.1005: risk 25, reward 55 pips
    assert len(fr["trades"]) == 1 and abs(fr["trades"][0]["r"] - 2.2) < 1e-6


def test_entry_too_close_to_target_is_refused():
    sig = [{**SIG[0], "tp": 1.1045}]  # at 1.1008 (0.40 ATR) reward 37 < 2 x risk 28
    assert simulate("EURUSD", H1, sig, "front_0.40atr", CFG, COSTS)["refused_rr"] == 1


def test_split_takes_half_at_market():
    sp = simulate("EURUSD", H1, [{**SIG[0], "sl": 1.0990}], "split", CFG, COSTS)
    # market half at 1.1008: risk 18, reward 52 pips -> 0.5 x 2.89R; the limit half never fills
    assert len(sp["trades"]) == 1 and abs(sp["trades"][0]["r"] - 0.5 * 52 / 18) < 1e-3
    # with the original stop the market half would be only 1:1.86, so it is not taken
    assert simulate("EURUSD", H1, SIG, "split", CFG, COSTS)["trades"] == []
