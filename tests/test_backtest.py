from fxsignals.backtest import summarise


def test_summary_flags_small_samples():
    s = summarise([{"outcome": "TARGET", "r": 3.0}, {"outcome": "STOP", "r": -1.05}, {"outcome": "OPEN_AT_END", "r": 0.4}])
    assert s["trades"] == 2  # open trade excluded
    assert s["statistically_meaningful"] is False
    assert "No profitability claim" in s["verdict"]
    assert s["max_drawdown_r"] == 1.05


def test_summary_large_sample():
    trades = [{"outcome": "TARGET", "r": 2.0}] * 40 + [{"outcome": "STOP", "r": -1.0}] * 70
    s = summarise(trades)
    assert s["trades"] == 110 and s["statistically_meaningful"]
    assert abs(s["expectancy_r"] - (80 - 70) / 110) < 1e-3
    assert s["profit_factor"] == round(80 / 70, 2)
