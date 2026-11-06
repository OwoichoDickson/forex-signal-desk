from fxsignals.config import load_config
from fxsignals.fundamentals import PairFundamentals
from fxsignals.notify import fmt_signal
from fxsignals.signals import analyse_timeframe, decide_qmr
from tests.test_qm_shadow import _h1

CFG = load_config()


def test_qmr_signal_is_a_limit_at_the_qm_level_with_stop_beyond_the_head():
    c = _h1([1.080, 1.100, 1.090, 1.120, 1.085])
    cfg = {**CFG, "qmr": {**CFG["qmr"], "enabled": True}}
    s = decide_qmr("EURUSD", c, {"H1": analyse_timeframe(c, cfg)}, None, PairFundamentals(available=False), cfg, True)
    assert s["style"] == "QMR" and s["direction"] == "SELL" and s["signal_type"] == "SELL_LIMIT"
    assert s["entry_price"] == 1.104 and s["stop_loss"] > 1.124
    risk = s["stop_loss"] - s["entry_price"]
    assert abs((s["entry_price"] - s["take_profit_2"]) - 2 * risk) < 1e-4
    assert "🧪 QMR" in fmt_signal({**s, "signal_id": "x"}, None)
    off = {**cfg, "qmr": {**cfg["qmr"], "enabled": False}}
    assert decide_qmr("EURUSD", c, {"H1": analyse_timeframe(c, off)}, None, PairFundamentals(available=False), off, True) is None


def test_report_keeps_the_trade_style():
    from fxsignals.report import _signal_out
    assert _signal_out({"style": "QMR", "entry_minutes": 720})["style"] == "QMR"
    assert _signal_out({"style": "SCALP", "max_hold_minutes": 120})["max_hold_minutes"] == 120


def test_qmr_score_counts_its_points():
    from fxsignals.signals import QMR_POINTS, qmr_score
    top, got = qmr_score(True, 1.1, 1.0, 40, 0.8, 14, 0.15)
    assert top == 40 + 10 * len(QMR_POINTS) and len(got) == len(QMR_POINTS)
    low, got = qmr_score(True, 1.0, 1.1, 10, 2.5, 8, 0.4)  # EMAs against, weak H4, wide stop, London, trending
    assert (low, got) == (40, [])
