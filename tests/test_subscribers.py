from datetime import datetime, timezone
from pathlib import Path

from fxsignals import subscribers
from fxsignals import taken as taken_store
from fxsignals.bot import cmd_text, handle_callback, handle_follower
from fxsignals.history import SignalHistory
from fxsignals.notify import for_follower
from fxsignals.scanner import notify_history

SIG = {"symbol": "EURUSD", "direction": "BUY", "signal_type": "MARKET_ENTRY", "entry_price": 1.1000, "stop_loss": 1.0980,
       "take_profit_1": 1.1040, "take_profit_2": 1.1060, "take_profit_3": 1.1080, "atr": 0.001, "status": "ACTIVE",
       "confidence_score": 82, "risk_reward": "1:3.0", "economic_risk": "LOW", "market_structure": "BULLISH_CHOCH",
       "position_size": {"lots": 0.05, "actual_risk_amount": 10.0, "account_currency": "USD"}}


class TG:
    """Owner chat 42; records what goes where."""
    chat_id = "42"

    def __init__(self, *_a, **_k):
        self.owner, self.others, self.answers, self.state_dir = [], [], [], None

    def send(self, text, buttons=None):
        self.owner.append((text, buttons))
        return True

    def send_to(self, cid, text, buttons=None):
        self.others.append((str(cid), text))
        return True

    def answer_callback(self, cid, text):
        self.answers.append(text)

    def replace_buttons(self, chat, mid, buttons):
        pass


def msg(cid, text="/start", username="friend"):
    return {"chat": {"id": cid, "type": "private", "first_name": "Ana", "username": username}, "text": text}


def test_request_approve_and_remove(cfg):
    state = cfg["paths"]["state_dir"]
    tg = TG()
    handle_follower(cfg, tg, msg(77))
    assert subscribers.get(state, 77)["status"] == "pending"
    assert "wants to receive" in tg.owner[-1][0] and tg.owner[-1][1][0][0][1] == "approve:77"
    assert "Request sent" in tg.others[-1][1]
    handle_follower(cfg, tg, msg(77))  # asking twice does not bother the owner again
    assert len(tg.owner) == 1 and "waiting" in tg.others[-1][1]
    stranger = {"id": "x", "data": "approve:77", "message": {"chat": {"id": 77}, "message_id": 1}}
    handle_callback(cfg, tg, stranger)  # only the owner can approve
    assert subscribers.get(state, 77)["status"] == "pending"
    handle_callback(cfg, tg, {**stranger, "message": {"chat": {"id": 42}, "message_id": 1}})
    assert subscribers.approved(state) == ["77"] and "approved" in tg.others[-1][1]
    cmd_text(cfg, tg, "/users")
    assert "@friend" in tg.owner[-1][0]
    cmd_text(cfg, tg, "/remove @friend")
    assert subscribers.approved(state) == [] and "removed" in tg.owner[-1][0]
    n = len(tg.owner)
    handle_follower(cfg, tg, msg(77))  # removed people are not re-offered to the owner
    assert len(tg.owner) == n


def test_follower_cannot_log_owner_trades(cfg):
    state = Path(cfg["paths"]["state_dir"])
    h = SignalHistory(state / "signal_history.json", {"MARKET_ENTRY": 60})
    rec = h.register(dict(SIG), datetime.now(timezone.utc))
    h.save()
    subscribers.request(state, {"id": 77})
    subscribers.decide(state, 77, "approved")
    tg = TG()
    handle_callback(cfg, tg, {"id": "c", "data": f"took:{rec['signal_id']}", "message": {"chat": {"id": 77}, "message_id": 1}})
    handle_follower(cfg, tg, msg(77, "/took EURUSD"))
    assert rec["signal_id"] not in taken_store.load(state)


def test_followers_get_signals_without_owner_details(cfg, monkeypatch):
    state = Path(cfg["paths"]["state_dir"])
    subscribers.request(state, {"id": 77})
    subscribers.decide(state, 77, "approved")
    subscribers.request(state, {"id": 88})  # pending: gets nothing
    tg = TG()
    tg.state_dir = state
    from fxsignals.notify import Telegram
    tg.broadcast = lambda text: Telegram.broadcast(tg, text)
    tg.enabled = True
    monkeypatch.setattr("fxsignals.scanner.Telegram", lambda cfg: tg)
    h = SignalHistory(state / "signal_history.json", {"MARKET_ENTRY": 60})
    rec = h.register(dict(SIG), datetime.now(timezone.utc))
    notify_history(cfg, h, [rec])
    notify_history(cfg, h, [rec])
    assert len(tg.owner) == 1 and "Lot size" in tg.owner[0][0]
    assert [c for c, _ in tg.others] == ["77"]
    text = tg.others[0][1]
    assert "EURUSD" in text and "Stop loss" in text
    for gone in ("Lot size", "Did you take", "Tap 🔎", "Open dashboard"):
        assert gone not in text


def test_for_follower_keeps_deadline_advice():
    out = for_follower("⏰ X\nEnds at 11:00. Not taken yet? Tap 🔎 to check first — after that time, don't take it.")
    assert out.endswith("After that time, don't take it.")


def test_hold_or_close_for_running_trades(cfg):
    from datetime import timedelta
    from fxsignals.precheck import manage
    now = datetime.now(timezone.utc)
    rec = {**SIG, "signal_id": "x", "created_at": now.isoformat(), "outcome": "OPEN", "fill_price": 1.1000}
    assert "<b>Hold</b>" in manage(rec, 1.1005, now.isoformat(), now)
    assert "move your stop to the entry" in manage({**rec, "tp1_hit": True}, 1.1045, now.isoformat(), now)
    warned = manage({**rec, "warnings": [{"kind": "structure", "reason": "trend turned bearish"}]}, 1.099, now.isoformat(), now)
    assert "consider closing early" in warned and "trend turned bearish" in warned
    assert "close it" in manage({**rec, "outcome": "TIMEOUT", "result_r": 0.3}, None, None, now + timedelta(days=6))
    assert "your trade is <b>+0.50R</b>" in manage(rec, 1.1010, now.isoformat(), now, my_price=1.1000)
    assert "cancel it" in manage({**rec, "outcome": "NOT_FILLED", "fill_price": None, "status": "CANCELLED"}, None, None, now)


def test_hold_command_owner_and_follower(cfg):
    state = Path(cfg["paths"]["state_dir"])
    h = SignalHistory(state / "signal_history.json", {"MARKET_ENTRY": 60})
    rec = h.register(dict(SIG), datetime.now(timezone.utc))
    h.records[-1].update(outcome="OPEN", fill_price=1.1)
    h.save()
    tg = TG()
    cmd_text(cfg, tg, "/hold")
    assert "None of your logged trades" in tg.owner[-1][0]  # running, but not taken by the owner
    taken_store.record(state, rec["signal_id"], True, via="test", entry_price=1.1)
    cmd_text(cfg, tg, "/hold")
    assert "EURUSD" in tg.owner[-1][0]
    cmd_text(cfg, tg, "/check EURUSD")  # taken + running: hold-or-close, not "don't take it"
    assert "Hold" in tg.owner[-1][0] and "Don't take it" not in tg.owner[-1][0]
    subscribers.request(state, {"id": 77})
    subscribers.decide(state, 77, "approved")
    handle_follower(cfg, tg, msg(77, "/hold EURUSD"))
    assert tg.others[-1][0] == "77" and "Hold" in tg.others[-1][1]


def test_losing_trade_is_flagged():
    from fxsignals.precheck import manage
    now = datetime.now(timezone.utc)
    rec = {**SIG, "signal_id": "x", "created_at": now.isoformat(), "outcome": "OPEN", "fill_price": 1.1000}
    out = manage(rec, 1.0990, now.isoformat(), now)  # half way to the 1.0980 stop
    assert out.startswith("🔴") and "-0.50R" in out and "50% of the way" in out and "10.0 pips left" in out
    assert manage(rec, 1.1010, now.isoformat(), now).startswith("🟢")


def test_signals_past_deadline_go_to_archive(cfg):
    from datetime import timedelta
    from fxsignals.bot import archive_text
    from fxsignals.history import on_board
    from fxsignals.monitor import events_for
    now = datetime.now(timezone.utc)
    rec = {**SIG, "signal_id": "x", "created_at": (now - timedelta(hours=2)).isoformat(),
           "expires_at": (now - timedelta(hours=1)).isoformat(), "outcome": "OPEN", "fill_price": 1.1, "open_r": -0.6,
           "warnings": [{"kind": "structure", "reason": "trend turned"}]}
    assert not on_board(rec, now)                 # past its deadline, not taken: archived
    assert on_board(rec, now, taken=True)         # you took it: stays until the trade finishes
    assert on_board({**rec, "expires_at": (now + timedelta(minutes=5)).isoformat()}, now)
    keys = [k for k, *_ in events_for(rec, None, None, frozenset({"new"}), now)]
    assert "r_neg" not in keys and "warn:structure" not in keys          # no more noise for archived signals
    assert "r_neg" in [k for k, *_ in events_for(rec, True, None, frozenset({"new"}), now)]
    h = SignalHistory(Path(cfg["paths"]["state_dir"]) / "signal_history.json", {"MARKET_ENTRY": 60})
    h.records.append(rec)
    assert "Archive" in archive_text(h, now, {}) and "still running -0.6R" in archive_text(h, now, {})
