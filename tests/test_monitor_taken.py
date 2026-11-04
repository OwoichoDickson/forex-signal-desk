from datetime import datetime, timedelta, timezone

from fxsignals import taken as taken_store
from fxsignals.bot import handle_callback
from fxsignals.history import SignalHistory
from fxsignals.monitor import events_for, review_open_signals
from fxsignals.scanner import notify_history

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
LIMIT = {"symbol": "EURGBP", "direction": "SELL", "signal_type": "SELL_LIMIT", "entry_price": 0.8354, "stop_loss": 0.8362,
         "take_profit_1": 0.8338, "take_profit_2": 0.8323, "take_profit_3": 0.8315, "atr": 0.0006, "status": "PENDING",
         "confidence_score": 76, "risk_reward": "1:3.8", "economic_risk": "LOW", "market_structure": "BEARISH_PULLBACK_TO_LEVEL"}
MARKET = {**LIMIT, "signal_type": "MARKET_ENTRY", "status": "ACTIVE", "entry_price": 0.8340, "stop_loss": 0.8356,
          "take_profit_1": 0.8308, "take_profit_2": 0.8292}


def hist(tmp_path):
    return SignalHistory(tmp_path / "h.json", {"MARKET_ENTRY": 60, "SELL_LIMIT": 240})


def result(bias_h1="BEARISH", bias_h4="BEARISH", direction="WAIT", code="HIGH_SPREAD", choch=None):
    tech = {"H1": {"bias": bias_h1, "last_bos_choch": choch}, "H4": {"bias": bias_h4}}
    return {"symbol": "EURGBP", "direction": direction, "wait_code": code, "reason": "Spread too wide",
            "timeframe_bias": {"H1": bias_h1, "H4": bias_h4}, "details": {"technicals": tech}}


def test_pullback_order_survives_h1_turning_but_cancels_when_h4_flips(tmp_path):
    h = hist(tmp_path)
    h.register(dict(LIMIT), T0)
    review_open_signals(h, [result(bias_h1="BULLISH")], T0 + timedelta(minutes=20))  # rally into the sell zone: expected
    assert h.records[0]["status"] == "PENDING" and h.records[0]["unconfirmed_since"]
    review_open_signals(h, [result(bias_h4="BULLISH")], T0 + timedelta(minutes=40))
    r = h.records[0]
    assert r["status"] == "CANCELLED" and r["outcome"] == "NOT_FILLED" and "H4 BULLISH" in r["cancel_reason"]


def test_news_cancels_pending_and_warns_running(tmp_path):
    h = hist(tmp_path)
    h.register(dict(LIMIT), T0)
    h.register({**MARKET, "symbol": "EURUSD"}, T0)
    h.records[1].update(outcome="OPEN", fill_price=0.834)
    news = result(code="NEWS_BLACKOUT")
    review_open_signals(h, [news, {**news, "symbol": "EURUSD"}], T0 + timedelta(minutes=10))
    assert h.records[0]["status"] == "CANCELLED"
    assert h.records[1]["status"] != "CANCELLED" and h.records[1]["warnings"][0]["kind"] == "news"


def test_every_change_produces_one_update(tmp_path):
    h = hist(tmp_path)
    rec = h.register(dict(MARKET), T0)
    r = h.records[0]
    keys = lambda sent=frozenset(): [k for k, *_ in events_for(r, None, None, sent, T0 + timedelta(minutes=5))]
    assert keys() == ["new"]
    r.update(outcome="OPEN", fill_price=0.834, open_r=-0.6)
    assert "r_neg" in keys() and "filled" not in keys()  # market entries fill at once: no second question
    r.update(open_r=0.2)
    assert "recovered" in keys(frozenset({"new", "r_neg"}))
    r.update(open_r=1.3, tp1_hit=True)
    assert {"r_pos1", "tp1"} <= set(keys())
    r.update(outcome="WIN_TP2", result_r=3.0)
    assert keys()[-1] == "final"
    assert rec["signal_id"]


def test_limit_fill_asks_again_and_answer_is_shown(tmp_path):
    h = hist(tmp_path)
    h.register(dict(LIMIT), T0)
    r = h.records[0]
    r.update(outcome="OPEN", fill_price=0.8354)
    ev = {k: (t, b) for k, t, b in events_for(r, None, None)}
    assert "filled" in ev and "Did you take this trade?" in ev["filled"][0] and ev["filled"][1]  # buttons
    ev = {k: (t, b) for k, t, b in events_for(r, True, None)}
    assert ev["filled"][1] is None and "YOUR TRADE" in ev["filled"][0]


def test_notify_sends_each_event_once(cfg, tmp_path, monkeypatch):
    sent = []

    class TG:
        enabled = True

        def __init__(self, *_):
            pass

        def send(self, text, buttons=None):
            sent.append((text, buttons))
            return True

        def broadcast(self, text, *a):
            return 0
    monkeypatch.setattr("fxsignals.scanner.Telegram", TG)
    h = hist(tmp_path)
    rec = h.register(dict(LIMIT), T0)
    notify_history(cfg, h, [rec])
    notify_history(cfg, h, [rec])
    assert len(sent) == 1 and sent[0][1][0][0][1] == f"took:{rec['signal_id']}"
    h.records[0].update(status="CANCELLED", outcome="NOT_FILLED", cancel_reason="trend turned bullish")
    notify_history(cfg, h, [])
    notify_history(cfg, h, [])
    assert len(sent) == 2 and "Cancelled" in sent[1][0]


def test_bot_records_tap_from_linked_chat_only(cfg, tmp_path):
    h = SignalHistory(__import__("pathlib").Path(cfg["paths"]["state_dir"]) / "signal_history.json", {"SELL_LIMIT": 240})
    rec = h.register(dict(LIMIT), T0)
    h.save()
    calls = []

    class TG:
        chat_id = "42"

        def answer_callback(self, cid, text):
            calls.append(("answer", text))

        def replace_buttons(self, chat, mid, buttons):
            calls.append(("buttons", buttons))

        def send(self, text, buttons=None):
            calls.append(("send", text))
    cq = {"id": "c1", "data": f"took:{rec['signal_id']}", "message": {"chat": {"id": 42}, "message_id": 7}}
    handle_callback(cfg, TG(), cq)
    assert taken_store.status(taken_store.load(cfg["paths"]["state_dir"])[rec["signal_id"]]) is True
    assert calls[0][0] == "answer" and calls[0][1].startswith("✅ Took it at")
    assert any(k == "send" and "Reply with your entry price" in t for k, t in calls)
    handle_callback(cfg, TG(), {**cq, "id": "c2", "data": f"skip:{rec['signal_id']}", "message": {"chat": {"id": 99}, "message_id": 1}})
    assert taken_store.status(taken_store.load(cfg["paths"]["state_dir"])[rec["signal_id"]]) is True  # stranger ignored


def test_my_track_record_counts_only_taken(tmp_path):
    recs = [{"signal_id": "a", "outcome": "WIN_TP2", "result_r": 3.0}, {"signal_id": "b", "outcome": "LOSS_STOP", "result_r": -1.0},
            {"signal_id": "c", "outcome": "WIN_TP2", "result_r": 3.0}, {"signal_id": "d", "outcome": "OPEN"}]
    answers = {"a": {"taken": True}, "b": {"taken": True}, "c": {"taken": False}}
    s = taken_store.my_track_record(recs, answers)
    assert (s["taken"], s["skipped"], s["unanswered"], s["wins"], s["losses"], s["total_r"]) == (2, 1, 1, 1, 1, 2.0)


# ── deadlines, "check before I enter", entry time/price logging ──────────────
from fxsignals.bot import cmd_text  # noqa: E402
from fxsignals.precheck import check  # noqa: E402


def test_reminder_before_end_and_cancel_advice_when_taken_but_not_filled(tmp_path):
    h = hist(tmp_path)
    h.register(dict(LIMIT), T0)  # 240 min -> ends 16:00
    r = h.records[0]
    keys = lambda now, answer=None: [k for k, *_ in events_for(r, answer, None, frozenset(), now)]
    assert "ending" not in keys(T0 + timedelta(minutes=100))
    assert "ending" in keys(T0 + timedelta(minutes=215))
    assert "ending" not in keys(T0 + timedelta(minutes=215), answer=False)  # skipped: no reminder
    r.update(status="EXPIRED", close_reason="price reached TP1 before the pending entry; setup missed")
    final = dict((k, t) for k, t, _ in events_for(r, True, None, frozenset(), T0 + timedelta(minutes=60)))["final"]
    assert "cancel it now" in final and "Don't take it anymore" in final


def test_check_before_entering(tmp_path):
    h = hist(tmp_path)
    h.register(dict(LIMIT), T0)
    r = h.records[0]
    ok, msg = check(r, 0.8345, T0.isoformat(), T0 + timedelta(minutes=5))
    assert ok and "OK to take" in msg and "pips away" in msg
    ok, msg = check(r, 0.8330, T0.isoformat(), T0 + timedelta(minutes=5))  # below TP1 for a sell
    assert not ok and "TP1" in msg
    ok, msg = check(r, 0.8345, T0.isoformat(), T0 + timedelta(minutes=300))
    assert not ok and "Don't take it" in msg
    m = h.register({**MARKET, "symbol": "EURUSD"}, T0)
    ok, msg = check(m, 0.8318, T0.isoformat(), T0 + timedelta(minutes=5))  # ran 22 pips: reward too small now
    assert not ok and "Don't chase" in msg
    m.update(outcome="OPEN", fill_price=0.834)  # market entries are tracked as filled at once: still judged by price
    ok, msg = check(m, 0.8341, T0.isoformat(), T0 + timedelta(minutes=5))
    assert ok and "reward 1:" in msg


def test_took_command_and_price_reply_are_logged(cfg):
    from pathlib import Path
    state = Path(cfg["paths"]["state_dir"])
    h = SignalHistory(state / "signal_history.json", {"SELL_LIMIT": 240})
    rec = h.register(dict(LIMIT), datetime.now(timezone.utc))
    h.save()
    out = []
    tg = type("T", (), {"send": lambda self, t, b=None: out.append(t) or True})()
    cmd_text(cfg, tg, "/took eurgbp")
    assert "you took" in out[-1] and "Reply with your entry price" in out[-1]
    cmd_text(cfg, tg, "0.83545")
    e = taken_store.load(state)[rec["signal_id"]]
    assert e["taken"] is True and e["entry_price"] == 0.83545 and e["answered_at"]
    cmd_text(cfg, tg, "/took EURGBP 0.8356")
    assert taken_store.load(state)[rec["signal_id"]]["entry_price"] == 0.8356
