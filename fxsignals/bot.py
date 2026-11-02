"""Telegram bot listener: records "I took it" / "Skipped" taps and answers /mytrades and /open.

Long-polls getUpdates (only the linked chat is accepted). Runs as its own background service so
taps are recorded within seconds, independent of the 10-minute scan cycle.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional

from . import subscribers
from . import taken as taken_store
from .history import SignalHistory, atomic_write_json, display_status, on_board, still_valid
from .instruments import instrument
from .logging_setup import log
from .notify import API, Telegram, _http, local_hm, took_buttons
from .precheck import check, latest_price, manage

logger = logging.getLogger("fxsignals.bot")
HELP = ("I send every signal and every change to it.\n"
        "Tap <b>✅ I took it</b> or <b>❌ Skipped</b> under a signal — the time you tap is logged as your entry time, "
        "then reply with your entry price (e.g. <code>1.34059</code>) to log that too.\n"
        "Tap <b>🔎 Check before I enter</b> right before you trade: I tell you if it is still OK or if you should leave it.\n\n"
        "/check — is each open signal still OK to take?\n/check GBPUSD — one pair\n"
        "/hold — already in a trade? Hold or close, for each running trade\n/hold GBPUSD — one pair\n"
        "/took GBPUSD 1.34059 — log that you took it now (price optional)\n/skip GBPUSD — log that you skipped it\n"
        "/open — signals you can still take\n/archive — signals past their deadline and how they ended\n/mytrades — your taken trades and results\n"
        "/users — people who get your signals\n/remove @name — stop sending to someone\n/help — this message")
FOLLOWER_HELP = ("You receive the signals from this desk here. Use them on a <b>demo account</b> — results so far are not "
                 "proven, and this is not financial advice.\n"
                 "/check — is a signal still OK to take?\n/hold — already in a trade? Hold or close\n"
                 "/stop — stop receiving signals")


def _history(cfg: dict) -> SignalHistory:
    return SignalHistory(Path(cfg["paths"]["state_dir"]) / "signal_history.json", cfg["signals"]["expiry_minutes"])


def _tz(cfg: dict) -> str:
    return cfg["scan"].get("scan_timezone", "UTC")


def _awaiting_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["state_dir"]) / "telegram_awaiting_price.json"


def _set_awaiting(cfg: dict, signal_id: Optional[str]) -> None:
    atomic_write_json(_awaiting_path(cfg), {"signal_id": signal_id})


def _awaiting(cfg: dict) -> Optional[str]:
    p = _awaiting_path(cfg)
    return json.loads(p.read_text()).get("signal_id") if p.exists() else None


def check_message(cfg: dict, rec: dict, now: datetime) -> str:
    price, at = latest_price(cfg, rec["symbol"])
    return check(rec, price, at, now, _tz(cfg))[1]


def hold_message(cfg: dict, rec: dict, now: datetime, my_price: Optional[float] = None) -> str:
    price, at = latest_price(cfg, rec["symbol"])
    return manage(rec, price, at, now, _tz(cfg), my_price)


def running(h: SignalHistory) -> list[dict]:
    return [r for r in h.records if r.get("outcome") == "OPEN"]


def hold_reply(cfg: dict, h: SignalHistory, parts: list[str], now: datetime, answers: Optional[dict] = None) -> str:
    """/hold [PAIR]: hold-or-close for running trades (``answers`` = the owner's log, for their own fill price)."""
    if len(parts) > 1:
        rows = [r for r in h.records if r.get("symbol") == parts[1].upper()][-1:]
        if not rows:
            return f"No signal found for {escape(parts[1].upper())}."
    else:
        rows = running(h)
        if answers is not None:  # the owner: only the trades they logged as taken
            rows = [r for r in rows if taken_store.status(answers.get(r["signal_id"])) is True]
            if not rows:
                return ("None of your logged trades is running. Took one? Log it with <code>/took GBPUSD 1.34059</code>, "
                        "or ask about any pair with <code>/hold GBPUSD</code>.")
        if not rows:
            return "No trade is running right now. Use <code>/hold GBPUSD</code> to see how a pair's last signal ended."
    mine = lambda r: ((answers or {}).get(r["signal_id"]) or {}).get("entry_price")  # noqa: E731
    msgs = sorted((hold_message(cfg, r, now, mine(r)) for r in rows), key=lambda m: not m.startswith("🔴"))  # losing first
    bad = sum(m.startswith("🔴") or "going against you" in m.split("\n")[0] for m in msgs)
    top = (f"🔴 <b>{bad} of your {len(msgs)} trades going against you</b>\n\n" if bad else f"🟢 None of your {len(msgs)} trades is going against you\n\n") if len(msgs) > 1 else ""
    return top + "\n\n".join(msgs) + "\n\n<i>What the signal's plan says — not financial advice.</i>"


ARCHIVE_LABEL = {"WIN_TP2": "✅ won", "LOSS_STOP": "❌ lost", "TIMEOUT": "⏹ time limit", "NOT_FILLED": "— not entered"}


def archive_text(h: SignalHistory, now: datetime, answers: Optional[dict] = None, pair: Optional[str] = None,
                 n: int = 12, tz: str = "UTC") -> str:
    """The last ``n`` signals past their deadline (or finished), newest first, with how each ended."""
    rows = [r for r in h.records if not on_board(r, now, taken_store.status((answers or {}).get(r["signal_id"])))
            and (pair is None or r.get("symbol") == pair.upper())][-n:]
    if not rows:
        return "The archive is empty." if pair is None else f"No archived signal for {escape(pair.upper())}."
    lines = []
    for r in reversed(rows):
        if r.get("outcome") == "OPEN":
            res = f"▶ still running {r['open_r']:+}R" if r.get("open_r") is not None else "▶ still running"
        else:
            res = ARCHIVE_LABEL.get(r.get("outcome"), escape((r.get("close_reason") or display_status(r, now)).split(" — ")[0].lower()))
            if r.get("result_r") is not None:
                res += f" {r['result_r']:+}R"
        mine = {True: " · ✅ you took it", False: " · skipped"}.get(taken_store.status((answers or {}).get(r["signal_id"])), "") if answers is not None else ""
        lines.append(f"• {escape(r['symbol'])} {r['direction']} {escape(r['signal_type'].replace('_', ' ').lower())} "
                     f"{r['entry_price']} — {r['created_at'][5:10]} {local_hm(r['created_at'], tz)} · {res}{mine}")
    return "🗄 <b>Archive</b> — past their deadline, don't take these\n" + "\n".join(lines) + "\n\nOne pair: <code>/archive GBPUSD</code>"


def log_taken(cfg: dict, rec: dict, took: bool, via: str, price: Optional[float] = None) -> str:
    """Record the answer (now = the time taken) and tell the user what was logged."""
    now = datetime.now(timezone.utc)
    late = took and not still_valid(rec, now)
    taken_store.record(cfg["paths"]["state_dir"], rec["signal_id"], took, via=via, entry_price=price, late=late)
    hm = local_hm(now.isoformat(), _tz(cfg))
    name = f"{escape(rec['symbol'])} {rec['direction']} {rec['entry_price']}"
    if not took:
        _set_awaiting(cfg, None)
        return f"❌ Logged: you skipped {name}."
    text = f"✅ Logged: you took <b>{name}</b> at <b>{hm}</b>" + (f" at price {price}." if price else ".")
    if late:
        text += f"\n⚠️ This signal had already finished ({escape(rec.get('close_reason') or display_status(rec, now))}) — it was taken late."
    if price is None:
        _set_awaiting(cfg, rec["signal_id"])
        text += "\nReply with your entry price (e.g. <code>" + str(rec["entry_price"]) + "</code>) to log it too."
    return text


def handle_follower(cfg: dict, tg: Telegram, msg: dict) -> None:
    """A message from someone who is not the owner: request access, or manage an existing one."""
    state_dir = cfg["paths"]["state_dir"]
    chat = msg["chat"]
    cid = str(chat["id"])
    text = (msg.get("text") or "").strip().lower().split("@")[0]
    entry = subscribers.get(state_dir, cid)
    if entry and entry["status"] == "denied":
        return None  # stay silent: no spam for the owner or them
    if entry and entry["status"] == "approved":
        if text == "/stop":
            subscribers.remove(state_dir, cid)
            tg.send(f"👤 {escape(subscribers.label(cid, entry))} stopped receiving signals.")
            return tg.send_to(cid, "You won't receive signals anymore. Send /start to ask again.")
        parts, now, h = (msg.get("text") or "").split(), datetime.now(timezone.utc), _history(cfg)
        if text.split()[:1] == ["/hold"]:
            return tg.send_to(cid, hold_reply(cfg, h, parts, now))
        if text.split()[:1] == ["/archive"]:
            return tg.send_to(cid, archive_text(h, now, None, parts[1] if len(parts) > 1 else None, tz=_tz(cfg)))
        if text.split()[:1] == ["/check"]:
            rows = [_pick(h, parts[1], now)] if len(parts) > 1 else [r for r in h.records if still_valid(r, now)]
            rows = [r for r in rows if r]
            return tg.send_to(cid, "\n\n".join(check_message(cfg, r, now) for r in rows) or "No signal to check right now.")
        return tg.send_to(cid, FOLLOWER_HELP)
    if entry:
        return tg.send_to(cid, "⏳ Your request is waiting for the owner's approval.")
    entry = subscribers.request(state_dir, chat)
    tg.send(f"👤 <b>{escape(subscribers.label(cid, entry))}</b> wants to receive your signals.",
            [[("✅ Approve", f"approve:{cid}"), ("⛔ Deny", f"deny:{cid}")]])
    log(logger, logging.INFO, "access requested", chat=cid)
    return tg.send_to(cid, "👋 Request sent to the owner. You'll get a message here once you're approved.")


def handle_access(cfg: dict, tg: Telegram, cq: dict, action: str, cid: str) -> None:
    state_dir = cfg["paths"]["state_dir"]
    entry = subscribers.decide(state_dir, cid, "approved" if action == "approve" else "denied")
    if entry is None:
        return tg.answer_callback(cq["id"], "Request not found")
    who = escape(subscribers.label(cid, entry))
    msg = cq.get("message") or {}
    if action == "approve":
        tg.answer_callback(cq["id"], "Approved")
        tg.replace_buttons(msg["chat"]["id"], msg["message_id"], [[("✅ Approved", "noop:")]])
        tg.send_to(cid, "✅ You're approved — new signals will arrive here.\n\n" + FOLLOWER_HELP)
        tg.send(f"✅ {who} will now receive your signals. /remove {cid} to stop.")
    else:
        tg.answer_callback(cq["id"], "Denied")
        tg.replace_buttons(msg["chat"]["id"], msg["message_id"], [[("⛔ Denied", "noop:")]])
    log(logger, logging.INFO, "access decided", chat=cid, action=action)


def handle_callback(cfg: dict, tg: Telegram, cq: dict) -> None:
    chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
    if chat_id != str(tg.chat_id):
        return tg.answer_callback(cq["id"], "Not authorised")
    action, _, signal_id = (cq.get("data") or "").partition(":")
    if action in ("approve", "deny"):
        return handle_access(cfg, tg, cq, action, signal_id)
    if action not in ("took", "skip", "check"):
        return tg.answer_callback(cq["id"], "Already recorded")
    rec = next((r for r in _history(cfg).records if r.get("signal_id") == signal_id), None)
    if rec is None:
        return tg.answer_callback(cq["id"], "Signal not found")
    if action == "check":
        tg.answer_callback(cq["id"], "Checking…")
        return tg.send(check_message(cfg, rec, datetime.now(timezone.utc)))
    took = action == "took"
    reply = log_taken(cfg, rec, took, via="telegram")
    hm = local_hm(datetime.now(timezone.utc).isoformat(), _tz(cfg))
    label = f"✅ Took it at {hm}" if took else "❌ Skipped"
    tg.answer_callback(cq["id"], label)
    msg = cq.get("message") or {}
    tg.replace_buttons(msg["chat"]["id"], msg["message_id"], [[(label + " (tap to change)", f"undo:{signal_id}")]])
    tg.send(reply)
    log(logger, logging.INFO, "trade answer recorded", signal_id=signal_id, taken=took)


def handle_undo(cfg: dict, tg: Telegram, cq: dict, signal_id: str) -> None:
    msg = cq.get("message") or {}
    tg.answer_callback(cq["id"], "Choose again")
    tg.replace_buttons(msg["chat"]["id"], msg["message_id"], took_buttons(signal_id))


def _pick(h: SignalHistory, pair: str, now: datetime) -> Optional[dict]:
    """The pair's still-valid signal, else its most recent one."""
    rows = [r for r in h.records if r.get("symbol") == pair.upper()]
    valid = [r for r in rows if still_valid(r, now)]
    return (valid or rows or [None])[-1]


def _price(text: str) -> Optional[float]:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def cmd_text(cfg: dict, tg: Telegram, text: str) -> None:
    parts = (text or "").strip().split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    now = datetime.now(timezone.utc)
    h = _history(cfg)
    state_dir = cfg["paths"]["state_dir"]
    answers = taken_store.load(state_dir)
    if cmd and not cmd.startswith("/") and _price(cmd) is not None:
        sid = _awaiting(cfg)
        rec = next((r for r in h.records if r.get("signal_id") == sid), None)
        if rec is None:
            return tg.send("Which trade is that price for? Use /took PAIR PRICE, e.g. <code>/took GBPUSD 1.34059</code>.")
        price = _price(cmd)
        if abs(price - rec["entry_price"]) > 0.05 * rec["entry_price"]:
            return tg.send(f"{price} is far from the {escape(rec['symbol'])} entry {rec['entry_price']} — please send it again.")
        taken_store.set_entry_price(state_dir, sid, price)
        _set_awaiting(cfg, None)
        diff = instrument(rec["symbol"]).to_pips(price - rec["entry_price"])
        return tg.send(f"📝 Entry price <b>{price}</b> logged for {escape(rec['symbol'])} {rec['direction']} "
                       f"({diff:.1f} pips from the signal's entry {rec['entry_price']}).")
    if cmd == "/check":
        rows = [_pick(h, parts[1], now)] if len(parts) > 1 else [r for r in h.records if still_valid(r, now)]
        rows = [r for r in rows if r]
        if not rows:
            return tg.send("No signal to check right now." if len(parts) == 1 else f"No signal found for {escape(parts[1].upper())}.")
        # a trade you already took gets hold-or-close instead of "don't take it"
        return tg.send("\n\n".join(
            hold_message(cfg, r, now, (answers.get(r["signal_id"]) or {}).get("entry_price"))
            if taken_store.status(answers.get(r["signal_id"])) is True and r.get("fill_price") is not None
            else check_message(cfg, r, now) for r in rows))
    if cmd == "/hold":
        return tg.send(hold_reply(cfg, h, parts, now, answers))
    if cmd in ("/took", "/skip"):
        if len(parts) < 2:
            return tg.send(f"Send it like this: <code>{cmd} GBPUSD{' 1.34059' if cmd == '/took' else ''}</code>")
        rec = _pick(h, parts[1], now)
        if rec is None:
            return tg.send(f"No signal found for {escape(parts[1].upper())}.")
        price = _price(parts[2]) if cmd == "/took" and len(parts) > 2 else None
        return tg.send(log_taken(cfg, rec, cmd == "/took", via="telegram", price=price))
    if cmd == "/users":
        subs = subscribers.load(state_dir)
        if not subs:
            return tg.send("Nobody else receives your signals. When someone sends /start to the bot, you get Approve / Deny buttons.")
        icon = {"approved": "✅", "pending": "⏳", "denied": "⛔"}
        return tg.send("👥 <b>People</b>\n" + "\n".join(f"{icon.get(e['status'], '•')} {escape(subscribers.label(c, e))}"
                                                     for c, e in subs.items()) + "\n\n/remove @name or id — stop sending to someone")
    if cmd == "/remove":
        cid = subscribers.find(state_dir, parts[1]) if len(parts) > 1 else None
        if cid is None:
            return tg.send("Who? Send it like this: <code>/remove @name</code> or <code>/remove 123456</code> (see /users).")
        entry = subscribers.decide(state_dir, cid, "denied")  # denied, so a new /start doesn't ask you again
        tg.send_to(cid, "You won't receive signals from this bot anymore.")
        return tg.send(f"⛔ {escape(subscribers.label(cid, entry))} removed — they won't get signals anymore.")
    if cmd == "/open":
        rows = [r for r in h.records if on_board(r, now, taken_store.status(answers.get(r["signal_id"])))]
        if not rows:
            return tg.send("No signal is open right now. Past ones: /archive")
        lines = []
        for r in rows:
            a = taken_store.status(answers.get(r["signal_id"]))
            mine = "✅ taken" if a is True else "❌ skipped" if a is False else "❓ not answered"
            prog = f" · now {r['open_r']:+}R" if r.get("outcome") == "OPEN" and r.get("open_r") is not None else ""
            lines.append(f"<b>{escape(r['symbol'])} {r['direction']}</b> {escape(r['signal_type'].replace('_', ' ').lower())} "
                         f"{r['entry_price']} · {display_status(r, now)}{prog} · until {local_hm(r.get('expires_at'), _tz(cfg))} · {mine}")
        return tg.send("📋 <b>Open signals</b>\n" + "\n".join(lines) + "\n\nPast their deadline: /archive")
    if cmd == "/archive":
        return tg.send(archive_text(h, now, answers, parts[1] if len(parts) > 1 else None, tz=_tz(cfg)))
    if cmd == "/mytrades":
        s = taken_store.my_track_record(h.records, answers)
        mine = [r for r in h.records if taken_store.status(answers.get(r.get("signal_id"))) is True][-8:]
        lines = []
        for r in reversed(mine):
            a = answers[r["signal_id"]]
            res = f"{r['result_r']:+}R" if r.get("result_r") is not None else display_status(r, now).lower().replace("_", " ")
            lines.append(f"• {escape(r['symbol'])} {r['direction']} — taken {local_hm(a.get('answered_at'), _tz(cfg))} "
                         f"{a['answered_at'][5:10]}" + (f" at {a['entry_price']}" if a.get("entry_price") else "")
                         + (" (late)" if a.get("late") else "") + f" · {res}")
        return tg.send(f"📒 <b>Your trades</b>\nTaken {s['taken']} · skipped {s['skipped']} · not answered {s['unanswered']}\n"
                       f"Closed {s['closed']}: ✅ {s['wins']} won · ❌ {s['losses']} lost · win rate {s['win_rate_pct'] or '—'}%\n"
                       f"Total {s['total_r']:+}R · running now {s['open']}" + ("\n\n" + "\n".join(lines) if lines else ""))
    return tg.send(HELP)


def run(cfg: dict) -> None:
    tg = Telegram(cfg)
    offset_path = Path(cfg["paths"]["state_dir"]) / "telegram_offset.json"
    offset = json.loads(offset_path.read_text()).get("offset", 0) if offset_path.exists() else 0
    log(logger, logging.INFO, "telegram bot started", enabled=tg.enabled)
    if tg.enabled:  # the "/" menu in Telegram
        try:
            tg._call("setMyCommands", {"commands": json.dumps([
                {"command": c, "description": d} for c, d in (
                    ("check", "Is each open signal still OK to take?"), ("hold", "In a trade? Hold or close"), ("took", "Log a trade you took: /took PAIR PRICE"),
                    ("skip", "Log a signal you skipped: /skip PAIR"), ("open", "Signals you can still take"), ("archive", "Past signals and results"),
                    ("mytrades", "Your taken trades and results"), ("users", "People who get your signals"),
                    ("remove", "Stop sending to someone: /remove @name"), ("help", "How this works"))]),
                "scope": json.dumps({"type": "chat", "chat_id": int(tg.chat_id)})})
            tg._call("setMyCommands", {"commands": json.dumps([  # everyone else
                {"command": "start", "description": "Ask to receive signals"}, {"command": "check", "description": "Is a signal still OK to take?"},
                {"command": "hold", "description": "In a trade? Hold or close"},
                {"command": "archive", "description": "Past signals and results"}, {"command": "stop", "description": "Stop receiving signals"}])})
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
    while True:
        if not tg.enabled:  # not configured yet: check again later instead of crash-looping
            time.sleep(60)
            tg = Telegram(cfg)
            continue
        try:
            res = _http(API.format(token=tg.token, method="getUpdates"),
                        {"timeout": 30, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=45)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(10)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            try:
                if u.get("callback_query"):
                    cq = u["callback_query"]
                    action, _, sid = (cq.get("data") or "").partition(":")
                    if action == "noop":
                        tg.answer_callback(cq["id"], "Done")
                    elif action == "undo":
                        handle_undo(cfg, tg, cq, sid)
                    else:
                        handle_callback(cfg, tg, cq)
                elif u.get("message") and str(u["message"]["chat"]["id"]) == str(tg.chat_id):
                    cmd_text(cfg, tg, u["message"].get("text", ""))
                elif u.get("message") and u["message"]["chat"].get("type") == "private":
                    handle_follower(cfg, tg, u["message"])
            except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                log(logger, logging.ERROR, "bot update failed", error=str(e))
        offset_path.write_text(json.dumps({"offset": offset}))
