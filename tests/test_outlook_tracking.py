from datetime import datetime, timedelta, timezone

from fxsignals.history import SignalHistory
from fxsignals.models import Candles
from fxsignals.outlook import build_outlook, currency_strength, lean_score, stance_label, strength_pairings

WED = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def wait_result(code="RSI_MOMENTUM", biases=None, **extra):
    biases = biases or {"M5": "BEARISH", "M15": "NEUTRAL", "H1": "BEARISH", "H4": "BEARISH", "D1": "BEARISH"}
    tech = {tf: {"bias": b, "rsi": 53.6, "ema_20": 157.46, "ema_50": 157.40, "macd": 0.01, "macd_signal": 0.02} for tf, b in biases.items()}
    return {"symbol": "USDJPY", "direction": "WAIT", "wait_code": code, "reason": "H1 RSI 53.6 lacks bearish momentum",
            "timeframe_bias": biases, "live": {"tv_ratings": {"H1": "SELL", "H4": "SELL", "D1": "STRONG_SELL"}},
            "details": {"technicals": tech, "key_levels": {"support": [{"price": 157.2, "tf": "H1", "touches": 2, "type": "level"}],
                                                          "resistance": [{"price": 157.9, "tf": "H4", "touches": 3, "type": "level"}]}},
            **extra}


def test_lean_score_weights_and_labels():
    assert lean_score({"D1": "BULLISH", "H4": "BULLISH", "H1": "BULLISH", "M15": "BULLISH", "M5": "BULLISH"}) == 100
    assert lean_score({"H4": "BEARISH", "H1": "BULLISH"}) == 0
    assert stance_label(70) == "STRONG_BULLISH" and stance_label(-30) == "BEARISH" and stance_label(10) == "NEUTRAL"


def test_outlook_for_wait_gives_recommendation_watch_and_alerts():
    o = build_outlook(wait_result())
    assert o["lean"] == "STRONG_BEARISH" and o["action"] == "WAIT"
    assert "RSI (53.6) to move below 50" in o["watch_for"][0]
    assert o["alert_levels"][0] == {"price": 157.2, "why": "breakdown trigger (close below support)"}
    assert o["tv_ratings_agree"] is True
    assert "No trade now" in o["summary"]


def test_outlook_flags_tradingview_disagreement():
    r = wait_result()
    r["live"]["tv_ratings"] = {"H1": "BUY", "H4": "STRONG_BUY", "D1": "BUY"}
    o = build_outlook(r)
    assert o["tv_ratings_agree"] is False and "disagree" in o["summary"]


def test_outlook_for_signal_points_to_signal():
    r = wait_result(direction="BUY")
    r.update(direction="BUY", signal_type="BUY_LIMIT", entry_price=157.2,
             timeframe_bias={tf: "BULLISH" for tf in ("M5", "M15", "H1", "H4", "D1")})
    for t in r["details"]["technicals"].values():
        t["bias"] = "BULLISH"
    o = build_outlook(r)
    assert o["action"].startswith("BUY") and o["watch_for"] == [] and "Trade signal" in o["summary"]


def _c(sym, closes, tf="H1"):
    step = 3600 if tf == "H1" else 86400
    return Candles(sym, tf, [i * step for i in range(len(closes))], closes, closes, closes, closes, [0] * len(closes))


def test_currency_strength_and_pairings():
    up = [1.0] * 10 + [1.01] * 20      # +1 % over the window
    down = [1.0] * 10 + [0.99] * 20
    flat = [1.0] * 30
    candles = {"EURUSD": {"H1": _c("EURUSD", up), "D1": _c("EURUSD", up, "D1")},
               "USDJPY": {"H1": _c("USDJPY", down), "D1": _c("USDJPY", down, "D1")},
               "EURJPY": {"H1": _c("EURJPY", flat), "D1": _c("EURJPY", flat, "D1")}}
    s = currency_strength(candles)
    order = [r["currency"] for r in s]
    assert order[0] == "EUR" and order[-1] == "USD"
    pairs = strength_pairings(s, ["EURUSD", "USDJPY", "EURJPY"], {"EURUSD": {"lean": "BULLISH"}})
    eu = next(p for p in pairs if p["pair"] == "EURUSD")
    assert eu["favoured"] == "BULLISH" and eu["outlook_agrees"] is True


def _m5(rows, start):
    return Candles("EURUSD", "M5", [start + i * 300 for i in range(len(rows))],
                   [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [0] * len(rows))


SIG = {"symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000, "stop_loss": 1.0980,
       "take_profit_1": 1.1040, "take_profit_2": 1.1060, "take_profit_3": 1.1080, "atr": 0.001, "status": "ACTIVE"}


def test_tracking_win_to_tp2(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    h.register(dict(SIG), WED)
    t0 = int(WED.timestamp())
    bars = _m5([(1.1, 1.1010, 1.0995, 1.1005), (1.1005, 1.1045, 1.1000, 1.1040), (1.104, 1.1065, 1.1035, 1.1060)], t0)
    h.refresh({"EURUSD": 1.106}, WED + timedelta(minutes=15), bars={"EURUSD": bars})
    r = h.records[0]
    assert r["outcome"] == "WIN_TP2" and r["tp1_hit"] and r["result_r"] == 3.0
    assert r["status"] == "TARGET_HIT"
    tr = h.track_record()
    assert tr["closed_trades"] == 1 and tr["wins_tp2"] == 1 and tr["win_rate_pct"] == 100.0


def test_tracking_same_bar_counts_as_stop(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    h.register(dict(SIG), WED)
    bars = _m5([(1.1, 1.1070, 1.0970, 1.1000)], int(WED.timestamp()))
    h.refresh({}, WED + timedelta(minutes=5), bars={"EURUSD": bars})
    assert h.records[0]["outcome"] == "LOSS_STOP" and h.records[0]["result_r"] == -1.0
    assert h.records[0]["status"] == "INVALIDATED"


def test_pending_not_filled_then_expired(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"BUY_LIMIT": 60})
    h.register({**SIG, "signal_type": "BUY_LIMIT", "entry_price": 1.0990, "status": "PENDING"}, WED)
    bars = _m5([(1.1, 1.1010, 1.0995, 1.1005)] * 3, int(WED.timestamp()))
    h.refresh({}, WED + timedelta(minutes=15), bars={"EURUSD": bars})
    assert h.records[0]["outcome"] == "PENDING_FILL"
    h.refresh({}, WED + timedelta(minutes=90), bars={"EURUSD": bars})
    assert h.records[0]["outcome"] == "NOT_FILLED" and h.records[0]["status"] == "EXPIRED"


def test_pending_limit_fills_then_tracks(tmp_path):
    h = SignalHistory(tmp_path / "h.json", {"BUY_LIMIT": 240})
    h.register({**SIG, "signal_type": "BUY_LIMIT", "entry_price": 1.0990, "stop_loss": 1.0970,
                "take_profit_1": 1.1030, "take_profit_2": 1.1050, "take_profit_3": 1.1070, "status": "PENDING"}, WED)
    t0 = int(WED.timestamp())
    bars = _m5([(1.1, 1.1005, 1.0988, 1.0995), (1.0995, 1.1020, 1.0990, 1.1015)], t0)
    h.refresh({}, WED + timedelta(minutes=10), bars={"EURUSD": bars})
    r = h.records[0]
    assert r["outcome"] == "OPEN" and r["fill_price"] == 1.0990 and r["open_r"] == 1.25
