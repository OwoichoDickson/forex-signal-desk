from datetime import datetime, timedelta, timezone

from fxsignals import notify
from fxsignals.health import HealthMonitor, classify_error, data_problem
from fxsignals.history import SignalHistory
from fxsignals.notify import Telegram, fmt_health, fmt_outcome, fmt_signal
from fxsignals.scanner import notify_history

WED = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
SIG = {"symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000, "stop_loss": 1.0980,
       "take_profit_1": 1.1040, "take_profit_2": 1.1060, "take_profit_3": 1.1080, "atr": 0.001, "status": "ACTIVE",
       "confidence_score": 82, "risk_reward": "1:3.0", "economic_risk": "LOW", "market_structure": "BULLISH_CHOCH_AT_ORDER_BLOCK"}


class FakeTG:
    """Records messages instead of calling Telegram."""
    def __init__(self, *_a, **_k):
        self.sent = []
        self.enabled = True

    def send(self, text, buttons=None):
        FakeTG.log.append(text)
        return True

    def flush(self):
        return 0

    def broadcast(self, text, *a):
        return 0


def test_signal_message_has_levels_and_no_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    msg = fmt_signal({**SIG, "expires_at": "2026-11-04T13:00:00+00:00",
                      "position_size": {"lots": 0.05, "actual_risk_amount": 10.0, "account_currency": "USD"}},
                     "https://forex.example.com")
    for part in ("🟢 BUY", "EURUSD", "Entry: <b>1.1</b>", "Stop loss", "TP2", "Lot size: <b>0.05</b>", "Enter now", "forex.example.com"):
        assert part in msg
    assert "SECRET" not in msg


def test_outcome_and_health_messages():
    win = fmt_outcome({**SIG, "outcome": "WIN_TP2", "result_r": 3.0, "fill_price": 1.1, "created_at": WED.isoformat()})
    loss = fmt_outcome({**SIG, "outcome": "LOSS_STOP", "result_r": -1.0, "fill_price": 1.1, "created_at": WED.isoformat()})
    assert "✅ WIN" in win and "+3.0R" in win and "❌ LOSS" in loss and "-1.0R" in loss
    assert "problem" in fmt_health("TradingView closed", WED.isoformat()).lower()
    assert "back to normal" in fmt_health(None, None)


def test_each_signal_and_result_notified_once(cfg, tmp_path, monkeypatch):
    FakeTG.log = []
    monkeypatch.setattr("fxsignals.scanner.Telegram", FakeTG)
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    rec = h.register(dict(SIG), WED)
    notify_history(cfg, h, [rec])
    notify_history(cfg, h, [rec])  # second scan: same signal must not be re-sent
    assert len(FakeTG.log) == 1 and "EURUSD" in FakeTG.log[0]
    h.records[0].update(outcome="LOSS_STOP", result_r=-1.0, fill_price=1.1, outcome_at=WED.isoformat())
    notify_history(cfg, h, [])
    notify_history(cfg, h, [])
    assert len(FakeTG.log) == 2 and "LOSS" in FakeTG.log[-1]


def test_disabled_telegram_marks_handled_without_sending(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    h = SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60})
    rec = h.register(dict(SIG), WED)
    notify_history(cfg, h, [rec])
    assert "new" in h.records[0]["notified_events"]  # enabling Telegram later will not replay it


def test_offline_messages_are_queued_then_flushed(cfg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    tg = Telegram(cfg)
    online = {"up": False}
    delivered = []

    def fake_call(method, data=None):
        if not online["up"]:
            raise OSError("network down")
        delivered.append(data["text"])
        return {"ok": True}
    monkeypatch.setattr(tg, "_call", fake_call)
    assert tg.send("first") is False and tg.send("second") is False
    online["up"] = True
    assert tg.flush() == 2
    assert delivered[0].startswith("first") and "delayed" in delivered[0]
    assert tg.flush() == 0


def test_error_classification():
    assert "TradingView Desktop is closed" in classify_error("connect ECONNREFUSED 127.0.0.1:9222", True)
    assert "No internet" in classify_error("anything", False)
    assert "stopped responding" in classify_error("tool call timed out", True)


def test_data_problem_detects_mostly_missing_data():
    rep = {"signals": [], "watchlist": [{"wait_code": "STALE_DATA"}] * 15 + [{"wait_code": "NO_TREND"}] * 5}
    assert "15 of 20" in data_problem(rep)
    assert data_problem({"signals": [], "watchlist": [{"wait_code": "NO_TREND"}] * 20}) is None


def test_health_alerts_once_reminds_and_recovers(cfg, monkeypatch):
    sent, pings = [], []
    monkeypatch.setattr(notify, "heartbeat", lambda ok, msg="": pings.append(ok))
    monkeypatch.setattr("fxsignals.health.heartbeat", lambda ok, msg="": pings.append(ok))
    hm = HealthMonitor(cfg)
    hm.tg = type("T", (), {"send": lambda self, t: sent.append(t) or True, "flush": lambda self: 0})()
    hm.problem("TradingView closed")
    hm.problem("TradingView closed")  # same problem within 2 h: no duplicate alert
    assert len(sent) == 1 and pings == [False, False]
    hm.state["alerted_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    hm.problem("TradingView closed")  # reminder after 2 h
    assert len(sent) == 2
    hm.alive("weekend")  # skipped tick keeps the problem open
    assert hm.state["problem"] == "TradingView closed" and len(sent) == 2
    hm.ok()
    assert "back to normal" in sent[-1] and hm.state["problem"] is None and pings[-1] is True
