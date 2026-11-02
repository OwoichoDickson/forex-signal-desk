from fxsignals.instruments import RateBook, instrument, pip_value_per_lot
from fxsignals.risk import apply_portfolio_limits, position_size, rr_ratio


def test_pip_conventions():
    assert instrument("EURUSD").pip_size == 0.0001
    assert instrument("USDJPY").pip_size == 0.01
    assert instrument("GBPJPY").pip_size == 0.01
    assert instrument("XAUUSD").pip_size == 0.1 and instrument("XAUUSD").contract_size == 100
    assert round(instrument("USDJPY").to_pips(157.567 - 157.267), 6) == 30.0
    assert round(instrument("EURUSD").to_pips(1.0850 - 1.0820), 6) == 30.0
    assert round(instrument("XAUUSD").to_pips(3880.0 - 3875.0), 6) == 50.0


def test_jpy_pip_value_converts_through_usdjpy():
    book = RateBook({"USDJPY": 150.0})
    # 0.01 * 100,000 = 1,000 JPY per pip per lot -> 1000/150 USD
    assert abs(pip_value_per_lot(instrument("USDJPY"), "USD", book) - 1000 / 150) < 1e-9
    assert abs(pip_value_per_lot(instrument("EURJPY"), "USD", book) - 1000 / 150) < 1e-9


def test_pip_values_differ_by_quote_currency():
    book = RateBook({"EURUSD": 1.10, "USDCHF": 0.80, "USDCAD": 1.40, "AUDUSD": 0.65, "GBPUSD": 1.30, "USDJPY": 150.0})
    assert pip_value_per_lot(instrument("EURUSD"), "USD", book) == 10.0
    assert abs(pip_value_per_lot(instrument("USDCHF"), "USD", book) - 12.5) < 1e-9
    assert abs(pip_value_per_lot(instrument("EURAUD"), "USD", book) - 6.5) < 1e-9
    assert abs(pip_value_per_lot(instrument("XAUUSD"), "USD", book) - 10.0) < 1e-9
    # EUR account: cross through USD
    assert abs(pip_value_per_lot(instrument("GBPJPY"), "EUR", book) - (1000 / 150) / 1.10) < 1e-9


def test_position_size_jpy_pair():
    book = RateBook({"USDJPY": 150.0})
    ps = position_size("USDJPY", 150.00, 149.70, 10_000, 1.0, "USD", book)
    # risk $100, 30 pips, $6.667/pip/lot -> 0.5 lots
    assert ps["stop_pips"] == 30.0
    assert ps["lots"] == 0.5
    assert ps["actual_risk_amount"] <= 100.0


def test_position_size_rounds_down_and_flags_too_small():
    book = RateBook({"EURUSD": 1.1})
    ps = position_size("EURUSD", 1.1000, 1.0973, 10_000, 1.0, "USD", book)  # 27 pips
    assert ps["lots"] == 0.37 and ps["actual_risk_amount"] <= 100
    tiny = position_size("EURUSD", 1.1000, 1.0000, 100, 1.0, "USD", book)
    assert tiny["lots"] == 0.0 and tiny["note"]


def test_missing_conversion_rate_gives_null_size():
    ps = position_size("EURJPY", 160.0, 159.5, 10_000, 1.0, "USD", RateBook({}))
    assert ps["lots"] is None and "conversion" in ps["note"]


def test_rr_ratio():
    assert abs(rr_ratio(1.1000, 1.0970, 1.1060) - 2.0) < 1e-9
    assert abs(rr_ratio(150.00, 150.30, 149.10) - 3.0) < 1e-9


def _sig(sym, d):
    return {"symbol": sym, "direction": d}


def test_portfolio_limits_positions_and_correlation(cfg):
    r = {**cfg["risk"], "max_open_positions": 3, "max_currency_exposure_pct": 2.0, "risk_per_trade_pct": 1.0}
    sigs = [_sig("EURUSD", "BUY"), _sig("GBPUSD", "BUY"), _sig("AUDUSD", "BUY"), _sig("USDJPY", "SELL"), _sig("EURGBP", "BUY")]
    apply_portfolio_limits(sigs, r)
    alloc = [s["portfolio"]["allocated"] for s in sigs]
    # third short-USD trade would put 3% on USD -> blocked by correlation guard
    assert alloc[:3] == [True, True, False]
    assert "USD" in sigs[2]["portfolio"]["reason"]
    assert sigs[3]["portfolio"]["allocated"] is False


def test_single_trade_never_blocked_by_currency_cap(cfg):
    r = {**cfg["risk"], "risk_per_trade_pct": 3.0, "max_currency_exposure_pct": 2.0, "max_daily_loss_pct": 6.0,
         "daily_loss_used_pct": 0.0, "max_open_positions": 3}
    sigs = [_sig("EURGBP", "SELL"), _sig("EURUSD", "SELL")]
    apply_portfolio_limits(sigs, r)
    assert sigs[0]["portfolio"]["allocated"] is True
    assert sigs[1]["portfolio"]["allocated"] is False and "EUR" in sigs[1]["portfolio"]["reason"]  # stacking EUR short


def test_daily_loss_limit_blocks_everything(cfg):
    r = {**cfg["risk"], "daily_loss_used_pct": 3.0}
    sigs = [_sig("EURUSD", "BUY")]
    apply_portfolio_limits(sigs, r)
    assert sigs[0]["portfolio"]["allocated"] is False and "Daily loss limit" in sigs[0]["portfolio"]["reason"]
    sigs = [_sig("EURUSD", "BUY")]
    apply_portfolio_limits(sigs, {**cfg["risk"], "risk_per_trade_pct": 5.0, "max_daily_loss_pct": 3.0, "daily_loss_used_pct": 0.0})
    assert "larger than the daily loss limit" in sigs[0]["portfolio"]["reason"]
