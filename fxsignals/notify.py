"""Telegram notifications and an external heartbeat (healthchecks.io-style ping URL).

Secrets (bot token, ping URL) come only from the environment / .env and are never logged.
Messages that cannot be delivered (no internet) are kept in state/outbox.json and retried
on the next scan, so nothing is lost while the Mac is offline.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .history import atomic_write_json
from .logging_setup import log

logger = logging.getLogger("fxsignals.notify")
API = "https://api.telegram.org/bot{token}/{method}"
MAX_OUTBOX = 200


def _token() -> Optional[str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def tls_context() -> ssl.SSLContext:
    """Verified TLS using certifi's CA bundle (the python.org build has no system CA store configured)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http(url: str, data: Optional[dict] = None, timeout: float = 10.0) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout, context=tls_context()) as r:  # noqa: S310 - fixed https endpoints
        raw = r.read().decode() or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": True, "raw": raw[:200]}


class Telegram:
    def __init__(self, cfg: dict):
        self.state_dir = Path(cfg["paths"]["state_dir"])
        self.token = _token()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or self._saved_chat_id()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _saved_chat_id(self) -> Optional[str]:
        p = self.state_dir / "telegram.json"
        if p.exists():
            try:
                return str(json.loads(p.read_text()).get("chat_id") or "") or None
            except json.JSONDecodeError:
                return None
        return None

    def _call(self, method: str, data: Optional[dict] = None) -> dict:
        return _http(API.format(token=self.token, method=method), data)

    def discover_chat(self) -> Optional[dict]:
        """Find the chat of whoever last sent the bot a message (e.g. /start) and remember it."""
        res = self._call("getUpdates")
        chats = [u.get("message", {}).get("chat") for u in res.get("result", []) if u.get("message")]
        chats = [c for c in chats if c]
        if not chats:
            return None
        chat = chats[-1]
        atomic_write_json(self.state_dir / "telegram.json",
                          {"chat_id": chat["id"], "username": chat.get("username"), "saved_at": datetime.now(timezone.utc).isoformat()})
        self.chat_id = str(chat["id"])
        return chat

    def send(self, text: str, buttons: Optional[list[list[tuple[str, str]]]] = None) -> bool:
        """Send now; on failure keep it in the outbox for the next attempt.
        ``buttons`` = rows of (label, callback_data) shown under the message."""
        if not self.enabled:
            return False
        self.flush()
        if self._send_now(text, buttons):
            return True
        self._queue(text, buttons)
        return False

    @staticmethod
    def _markup(buttons) -> Optional[str]:
        if not buttons:
            return None
        return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]})

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    def replace_buttons(self, chat_id, message_id, buttons) -> None:
        try:
            self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                  "reply_markup": self._markup(buttons) or json.dumps({"inline_keyboard": []})})
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    def send_to(self, chat_id, text: str, buttons=None) -> bool:
        """Best effort to another chat (followers, approval replies): no outbox, no retries."""
        if not self.token:
            return False
        try:
            data = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}
            if buttons:
                data["reply_markup"] = self._markup(buttons)
            return bool(self._call("sendMessage", data).get("ok"))
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def broadcast(self, text: str) -> int:
        """Send a signal message to every approved follower (owner-only lines removed)."""
        from . import subscribers
        msg = for_follower(text)
        return sum(self.send_to(cid, msg) for cid in subscribers.approved(self.state_dir) if cid != str(self.chat_id))

    def _send_now(self, text: str, buttons=None) -> bool:
        try:
            data = {"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}
            if buttons:
                data["reply_markup"] = self._markup(buttons)
            res = self._call("sendMessage", data)
            if res.get("ok"):
                return True
            log(logger, logging.WARNING, "telegram rejected message", error=str(res.get("description"))[:200])
            return res.get("error_code") == 400  # malformed message: retrying will not help
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(logger, logging.WARNING, "telegram unreachable; message queued", error=type(e).__name__)
            return False

    def _outbox_path(self) -> Path:
        return self.state_dir / "outbox.json"

    def _queue(self, text: str, buttons=None) -> None:
        p = self._outbox_path()
        box = json.loads(p.read_text()) if p.exists() else []
        box.append({"queued_at": datetime.now(timezone.utc).isoformat(), "text": text, "buttons": buttons})
        atomic_write_json(p, box[-MAX_OUTBOX:])

    def flush(self) -> int:
        p = self._outbox_path()
        if not p.exists():
            return 0
        box = json.loads(p.read_text())
        sent = 0
        while box:
            item = box[0]
            note = f"\n<i>(delayed — queued {item['queued_at'][:16].replace('T', ' ')} UTC)</i>"
            if not self._send_now(item["text"] + note, item.get("buttons")):
                break
            box.pop(0)
            sent += 1
            time.sleep(0.05)
        atomic_write_json(p, box)
        return sent


def heartbeat(ok: bool, message: str = "") -> None:
    """Ping the external monitor (HEALTHCHECK_URL). Success pings keep it quiet; '/fail' alerts at once.
    If the Mac goes offline or to sleep, the pings stop and the external service sends the alert."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        _http(url.rstrip("/") + ("" if ok else "/fail"), {"msg": message[:500]} if message else None, timeout=10)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass  # no internet: the missing ping is itself the alert


# ── message formats ───────────────────────────────────────────────────────────
def local_hm(ts: Optional[str], tz: str = "UTC") -> str:
    """'11:35' in the user's timezone (the scan timezone, i.e. this Mac's clock)."""
    if not ts:
        return "?"
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).strftime("%H:%M")


def in_words(minutes: float) -> str:
    m = max(0, int(round(minutes)))
    return f"{m} min" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def deadline_line(s: dict, tz: str = "UTC") -> str:
    """The 'don't take it after' rule shown on every signal."""
    end = local_hm(s.get("expires_at"), tz)
    tz = tz.split("/")[-1].replace("_", " ")  # "Europe/Lisbon" -> "Lisbon"
    if s.get("signal_type") == "MARKET_ENTRY":
        return (f"⏰ <b>Enter before {end}</b> ({escape(tz)} time). After {end}, or if price already reached "
                f"TP1 {s['take_profit_1']}, don't take it — the move is gone.")
    return (f"⏰ <b>Order valid until {end}</b> ({escape(tz)} time). Not filled by {end}, or price reaches "
            f"TP1 {s['take_profit_1']} first → cancel it, don't take it.")


ORDER_TYPES = {  # signal type -> what to pick in MT4/MT5/cTrader/TradingView order tickets
    "MARKET_ENTRY": "Market order (Market Execution / Instant) — no pending order",
    "BUY_LIMIT": "Pending order → Buy Limit", "SELL_LIMIT": "Pending order → Sell Limit",
    "BUY_STOP": "Pending order → Buy Stop", "SELL_STOP": "Pending order → Sell Stop",
}


def order_instruction(s: dict) -> str:
    """Exactly what to choose in the broker's order window."""
    kind = ORDER_TYPES.get(s["signal_type"], s["signal_type"].replace("_", " "))
    side = s["direction"].capitalize()
    if s["signal_type"] == "MARKET_ENTRY":
        return f"{kind}: press <b>{side}</b> now (price near {s['entry_price']})"
    return f"{kind} at <b>{s['entry_price']}</b>"


OWNER_ONLY = ("Tap 🔎", "Open dashboard", "Lot size:", "Did you take this trade?")


def for_follower(text: str) -> str:
    """A signal message without the owner's lot size, buttons hints and private dashboard link."""
    text = text.replace("Not taken yet? Tap 🔎 to check first — after that time, don't take it.", "After that time, don't take it.")
    lines = [ln for ln in text.split("\n") if not any(k in ln for k in OWNER_ONLY)]
    return "\n".join(lines).strip()


def fmt_signal(s: dict, public_url: Optional[str], tz: str = "UTC") -> str:
    arrow = "🟢 BUY" if s["direction"] == "BUY" else "🔴 SELL"
    ps = s.get("position_size") or {}
    lots = f"\nLot size: <b>{ps['lots']}</b> (risk {ps.get('actual_risk_amount')} {ps.get('account_currency', '')})" if ps.get("lots") else ""
    status = s.get("status")
    when = "Enter now" if status == "ACTIVE" else "Pending order — waits for price" if status == "PENDING" else "Market closed — re-check at the open"
    link = f"\n<a href=\"{escape(public_url)}\">Open dashboard</a>" if public_url else ""
    return (
        f"<b>{arrow} {escape(s['symbol'])}</b> — {escape(s['signal_type'].replace('_', ' '))}\n"
        f"{when} · confidence <b>{s['confidence_score']}</b>\n"
        f"📝 In your platform: {order_instruction(s)}\n\n"
        f"Entry: <b>{s['entry_price']}</b>\nStop loss: <b>{s['stop_loss']}</b>\n"
        f"TP1: {s['take_profit_1']}\nTP2: <b>{s['take_profit_2']}</b>\nTP3: {s['take_profit_3']}\n"
        f"R:R {escape(str(s['risk_reward']))} · news risk {escape(str(s.get('economic_risk')))}{lots}\n\n"
        f"{escape(s.get('market_structure') or '').replace('_', ' ').lower()}\n"
        f"{deadline_line(s, tz)}\nTap 🔎 <b>Check before I enter</b> right before you trade.{link}"
    )


def fmt_outcome(r: dict, answer: Optional[bool] = None) -> str:
    win = r["outcome"] == "WIN_TP2"
    head = "✅ WIN" if win else "❌ LOSS" if r["outcome"] == "LOSS_STOP" else "⏹ CLOSED (time limit)"
    return (f"<b>{head} — {escape(r['symbol'])} {escape(r['direction'])}</b>{taken_tag(answer)}\n"
            f"Result: <b>{r.get('result_r'):+}R</b> · entry {r.get('fill_price')} → "
            f"{'TP2 ' + str(r['take_profit_2']) if win else 'stop ' + str(r['stop_loss']) if r['outcome'] == 'LOSS_STOP' else 'closed'}\n"
            f"Signal from {escape(r['created_at'][:16].replace('T', ' '))} UTC")


def took_buttons(signal_id: str) -> list[list[tuple[str, str]]]:
    return [[("✅ I took it", f"took:{signal_id}"), ("❌ Skipped", f"skip:{signal_id}")],
            [("🔎 Check before I enter", f"check:{signal_id}")]]


def check_button(signal_id: str) -> list[list[tuple[str, str]]]:
    return [[("🔎 Check before I enter", f"check:{signal_id}")]]


def taken_tag(answer: Optional[bool]) -> str:
    return " · <b>YOUR TRADE</b>" if answer is True else " · (you skipped it)" if answer is False else ""


def fmt_filled(r: dict) -> str:
    return (f"🎯 <b>{escape(r['symbol'])} {escape(r['direction'])} — entry reached</b>\n"
            f"Price reached the {escape(r['signal_type'].replace('_', ' ').lower())} level {r.get('fill_price')}.\n"
            f"Stop {r['stop_loss']} · TP2 {r['take_profit_2']}\n\n<b>Did you take this trade?</b>")


def fmt_event(r: dict, kind: str, reason: str, answer: Optional[bool]) -> str:
    heads = {
        "cancelled": "❎ <b>Cancelled</b> — setup no longer valid",
        "structure": "⚠️ <b>Warning</b> — structure turned against the trade",
        "news": "⚠️ <b>Warning</b> — high-impact news soon",
        "breakeven": "💡 <b>TP1 reached</b> — consider moving your stop to break-even",
    }
    return (f"{heads[kind]}\n<b>{escape(r['symbol'])} {escape(r['direction'])} {escape(r['signal_type'].replace('_', ' ').lower())} "
            f"{r['entry_price']}</b>{taken_tag(answer)}\n{escape(reason)}")


def fmt_health(problem: Optional[str], since: Optional[str]) -> str:
    if problem:
        return f"⚠️ <b>Forex scanner problem</b>\n{escape(problem)}\nSince {escape((since or '')[:16].replace('T', ' '))} UTC"
    return "✅ <b>Forex scanner back to normal</b> — scans are running again."
