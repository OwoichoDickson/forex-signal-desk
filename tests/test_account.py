from fxsignals.account import load_account, resize_signals, save_account, validate


def sig(sym, d, pips, pv=10.0, status="ACTIVE"):
    return {"symbol": sym, "direction": d, "status": status,
            "position_size": {"lots": 1.0, "pip_value_per_lot": pv, "stop_pips": pips}}


def test_validate_rejects_bad_input():
    clean, errors = validate({"account_balance": "-5", "risk_per_trade_pct": 50, "account_currency": "XYZ"})
    assert errors and not clean.get("account_balance")
    clean, errors = validate({"account_balance": "2500", "risk_per_trade_pct": "1", "account_currency": "eur"})
    assert not errors and clean == {"account_balance": 2500.0, "risk_per_trade_pct": 1.0, "account_currency": "EUR"}


def test_saved_account_overrides_config(cfg):
    assert load_account(cfg)["source"] == "config.yaml"
    acct, errors = save_account(cfg, {"account_balance": 2500, "risk_per_trade_pct": 0.5, "max_open_positions": 2})
    assert not errors
    a = load_account(cfg)
    assert a["account_balance"] == 2500 and a["risk_per_trade_pct"] == 0.5 and a["max_open_positions"] == 2
    assert a["source"] == "dashboard"
    assert a["account_currency"] == cfg["risk"]["account_currency"]  # unspecified fields keep config values


def test_lots_scale_with_balance(cfg):
    risk = {**load_account(cfg), "account_balance": 2000.0, "risk_per_trade_pct": 1.0}
    s = [sig("EURUSD", "BUY", 20)]
    summary = resize_signals(s, risk)
    # 1% of 2000 = 20; 20 pips x $10/lot -> 0.10 lots
    assert s[0]["position_size"]["lots"] == 0.1 and s[0]["position_size"]["actual_risk_amount"] == 20.0
    assert summary["trades_allowed_now"] == 1 and summary["balance_if_all_stopped"] == 1980.0
    assert s[0]["risk_status"] == "ALLOWED"


def test_small_account_is_blocked_not_oversized(cfg):
    risk = {**load_account(cfg), "account_balance": 100.0, "risk_per_trade_pct": 1.0}
    s = [sig("EURUSD", "BUY", 30)]
    summary = resize_signals(s, risk)
    assert s[0]["position_size"]["lots"] == 0.0 and s[0]["risk_status"] == "BLOCKED"
    assert "well above" in s[0]["portfolio"]["reason"] and "Micro/cent account" in s[0]["portfolio"]["reason"]
    assert s[0]["position_size"]["micro_lots"] == 0.003  # $1 / (30 pips x $0.01 per 0.001 lot)
    assert summary["trades_allowed_now"] == 0 and summary["total_risk_amount"] == 0


def test_minimum_lot_allowed_when_only_slightly_over_budget(cfg):
    # EURGBP case: $2 budget, 16-pip stop at ~$13.4/pip/lot -> 0.01 lot risks ~$2.14 (7% over): allowed at minimum
    risk = {**load_account(cfg), "account_balance": 100.0, "risk_per_trade_pct": 2.0, "max_total_risk_pct": 4.0}
    s = [sig("EURGBP", "SELL", 16, pv=13.4)]
    resize_signals(s, risk)
    ps = s[0]["position_size"]
    assert ps["lots"] == 0.01 and ps["actual_risk_amount"] == 2.14
    assert s[0]["risk_status"] == "ALLOWED" and "slightly above" in ps["note"]


def test_trade_count_and_total_risk_caps(cfg):
    risk = {**load_account(cfg), "account_balance": 10000.0, "risk_per_trade_pct": 1.0, "max_open_positions": 3,
            "max_total_risk_pct": 2.0, "max_currency_exposure_pct": 5.0}
    s = [sig("EURUSD", "BUY", 20), sig("USDJPY", "BUY", 20), sig("AUDNZD", "SELL", 20), sig("GBPCHF", "BUY", 20)]
    summary = resize_signals(s, risk)
    allowed = [x["symbol"] for x in s if x["risk_status"] == "ALLOWED"]
    assert allowed == ["EURUSD", "USDJPY"]  # 2 x 1% hits the 2% total cap
    assert "Total open risk" in s[2]["portfolio"]["reason"]
    assert summary["total_risk_pct"] <= 2.0


def test_daily_loss_used_blocks_new_trades(cfg):
    risk = {**load_account(cfg), "daily_loss_used_pct": 3.0, "max_daily_loss_pct": 3.0}
    s = [sig("EURUSD", "BUY", 20)]
    summary = resize_signals(s, risk)
    assert s[0]["risk_status"] == "BLOCKED" and summary["daily_loss_left_pct"] == 0


def test_pending_signal_marked_allowed_when_active(cfg):
    s = [sig("EURUSD", "BUY", 20, status="PENDING")]
    resize_signals(s, {**load_account(cfg), "account_balance": 1000.0})
    assert s[0]["risk_status"] == "ALLOWED_WHEN_ACTIVE"


def test_no_amount_means_no_lot_sizes_but_limits_still_apply(cfg):
    risk = load_account(cfg)
    assert risk["account_balance"] is None  # never assumed: only a typed amount is used
    s = [sig("EURUSD", "BUY", 20), sig("GBPUSD", "BUY", 20), sig("AUDUSD", "BUY", 20), sig("NZDUSD", "BUY", 20)]
    summary = resize_signals(s, risk)
    assert all(x["position_size"]["lots"] is None for x in s)
    assert summary["balance"] is None and summary["trades_allowed_now"] == 2  # USD exposure cap (2%)
    assert [x["risk_status"] for x in s] == ["ALLOWED", "ALLOWED", "BLOCKED", "BLOCKED"]


def test_clearing_the_amount(cfg):
    save_account(cfg, {"account_balance": 100})
    assert load_account(cfg)["account_balance"] == 100
    save_account(cfg, {"account_balance": ""})
    assert load_account(cfg)["account_balance"] is None


def test_fixed_dollar_risk_per_trade(cfg):
    risk = {**load_account(cfg), "account_balance": None, "risk_per_trade_amount": 10.0}
    s = [sig("EURUSD", "BUY", 20), sig("USDJPY", "SELL", 25, pv=6.35)]
    summary = resize_signals(s, risk)
    # $10 / (20 pips x $10) = 0.05 lots; $10 / (25 x 6.35) = 0.062 -> 0.06
    assert s[0]["position_size"]["lots"] == 0.05 and s[0]["position_size"]["actual_risk_amount"] == 10.0
    assert s[1]["position_size"]["lots"] == 0.06 and s[1]["position_size"]["actual_risk_amount"] <= 10.0
    assert summary["risk_mode"] == "FIXED_AMOUNT" and summary["worst_case_all_trades"] == 30.0
    assert summary["warning"] is None


def test_fixed_risk_large_vs_amount_warns_but_is_not_blocked(cfg):
    risk = {**load_account(cfg), "account_balance": 100.0, "risk_per_trade_amount": 10.0}
    s = [sig("EURUSD", "BUY", 20)]
    summary = resize_signals(s, risk)
    assert s[0]["risk_status"] == "ALLOWED" and s[0]["position_size"]["lots"] == 0.05
    assert summary["risk_per_trade_pct"] == 10.0 and "10%" in summary["warning"]


def test_hundred_dollars_example(cfg):
    risk = {**load_account(cfg), "account_balance": 100.0, "risk_per_trade_pct": 1.0}
    s = [sig("EURUSD", "BUY", 10)]
    resize_signals(s, risk)
    # $1 risk / (10 pips x $10 per lot) = 0.01 lots — the broker minimum
    assert s[0]["position_size"]["lots"] == 0.01 and s[0]["position_size"]["actual_risk_amount"] == 1.0
