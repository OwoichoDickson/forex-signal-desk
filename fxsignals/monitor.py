"""Follow every open signal after it is issued and turn each change into a Telegram update.

Pending orders are cancelled when the reason for the trade is gone (trend flip, H1 change of
character against it, high-impact news). Running trades are never closed by the app — the stop
loss stays the plan — but every development is reported: entry reached, progress in R, going
against, back on track, TP1 (move stop to break-even), structural/news warnings, final result.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .history import FINAL_OUTCOMES, OPEN_STATES, effective_status
from .notify import (check_button, escape, fmt_event, fmt_filled, fmt_outcome, fmt_signal, in_words, local_hm,
                     taken_tag, took_buttons)

OPPOSITE = {"BUY": "BEARISH", "SELL": "BULLISH"}


def _hours_since(ts: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(ts)).total_seconds() / 3600


def review_open_signals(history, results: list[dict], now: datetime) -> None:
    """Compare every open signal with the latest scan and record what changed on the record itself."""
    by_symbol = {r["symbol"]: r for r in results}
    for rec in history.records:
        if rec.get("outcome") in FINAL_OUTCOMES or rec.get("status") in ("CANCELLED", "SUPERSEDED", "INVALIDATED"):
            continue
        running = rec.get("outcome") == "OPEN"
        if not running and effective_status(rec, now) not in OPEN_STATES:
            continue
        res = by_symbol.get(rec["symbol"])
        if not res:
            continue
        against = OPPOSITE[rec["direction"]]
        tb = res.get("timeframe_bias") or {tf: (t or {}).get("bias") for tf, t in ((res.get("details") or {}).get("technicals") or {}).items()}
        h1 = ((res.get("details") or {}).get("technicals") or {}).get("H1") or {}
        lb = h1.get("last_bos_choch") or {}
        # A limit order waits for price to pull back *against* the trade, so H1 turning that way is expected:
        # only the H4 trend decides for pullback orders. Breakout / market setups also need H1.
        pullback = rec["signal_type"].endswith("LIMIT") and not running
        reasons = []
        if tb.get("H4") == against or (tb.get("H1") == against and not pullback):
            reasons.append(("trend", f"trend turned {against.lower()} (H1 {tb.get('H1')}, H4 {tb.get('H4')})"))
        if (not pullback and lb.get("kind") == "CHOCH" and lb.get("direction") == against
                and lb.get("bars_ago", 99) < _hours_since(rec["created_at"], now)):
            reasons.append(("structure", f"H1 change of character {against.lower()} at {lb.get('price')}"))
        if res.get("wait_code") == "NEWS_BLACKOUT":
            reasons.append(("news", res.get("reason") or "high-impact news soon"))

        if not running:
            if reasons:
                why = "; ".join(r for _, r in reasons)
                rec.update(status="CANCELLED", outcome="NOT_FILLED", outcome_at=now.isoformat(), closed_at=now.isoformat(),
                           actionable=False, cancel_reason=why, close_reason=f"cancelled — {why}")
                continue
            confirmed = res.get("direction") == rec["direction"]
            if confirmed and rec.get("unconfirmed_since"):
                rec["reconfirmed_at"] = now.isoformat()
                rec.pop("unconfirmed_since", None)
            elif not confirmed and not rec.get("unconfirmed_since"):
                rec["unconfirmed_since"] = now.isoformat()
                rec["unconfirmed_reason"] = res.get("reason")
        else:
            warns = rec.setdefault("warnings", [])
            for kind, why in reasons:
                kind = "news" if kind == "news" else "structure"
                if not any(w["kind"] == kind for w in warns):
                    warns.append({"kind": kind, "reason": why, "at": now.isoformat()})


def events_for(rec: dict, answer: Optional[bool], url: Optional[str], sent: frozenset = frozenset(),
               now: Optional[datetime] = None, tz: str = "UTC", remind_minutes: int = 30) -> list[tuple[str, str, Optional[list]]]:
    """All updates this record warrants, as (event key, message, buttons). Keys are sent at most once;
    ``sent`` (keys already delivered) lets an update depend on an earlier one, e.g. 'back on track'."""
    now = now or datetime.now(timezone.utc)
    ev: list[tuple[str, str, Optional[list]]] = []
    tag = taken_tag(answer)
    sym = f"<b>{escape(rec['symbol'])} {escape(rec['direction'])} {escape(rec['signal_type'].replace('_', ' ').lower())} {rec['entry_price']}</b>"
    ask = took_buttons(rec["signal_id"]) if answer is None else None
    ev.append(("new", fmt_signal(rec, url, tz) + "\n\n<b>Did you take this trade?</b>", ask))
    end = rec.get("expires_at")
    left = (datetime.fromisoformat(end) - now).total_seconds() / 60 if end else None
    waiting = rec.get("fill_price") is None and rec.get("outcome") in (None, "PENDING_FILL") and effective_status(rec, now) in OPEN_STATES
    if waiting and left is not None and 0 < left <= remind_minutes and answer is not False:
        todo = ("If your order isn't filled by then, cancel it at your broker." if answer
                else "Not taken yet? Tap 🔎 to check first — after that time, don't take it.")
        ev.append(("ending", f"⏰ {sym}{tag}\nEnds at <b>{local_hm(end, tz)}</b> (in {in_words(left)}). {todo}",
                   None if answer else check_button(rec["signal_id"])))
    if rec.get("unconfirmed_since"):
        ev.append((f"unconfirmed:{rec['unconfirmed_since']}",
                   f"ℹ️ {sym}{tag}\nLatest scan no longer confirms it: {escape(str(rec.get('unconfirmed_reason')))}.\n"
                   f"The order stays valid until {local_hm(rec['expires_at'], tz)} unless the setup breaks.", None))
    if rec.get("reconfirmed_at"):
        ev.append((f"reconfirmed:{rec['reconfirmed_at']}", f"✅ {sym}{tag}\nLatest scan confirms the setup again.", None))
    if rec.get("fill_price") is not None and rec["signal_type"] != "MARKET_ENTRY":  # market entries fill at once
        ev.append(("filled", fmt_filled(rec) + (tag if answer is not None else ""), ask))
    r = rec.get("open_r")
    archived = answer is not True and end is not None and now >= datetime.fromisoformat(end)  # past its deadline, not taken
    if rec.get("outcome") == "OPEN" and r is not None and not archived:
        if r <= -0.5:
            ev.append(("r_neg", f"⚠️ {sym}{tag}\nGoing against the trade: now <b>{r:+}R</b> — about halfway to the stop "
                                f"({rec['stop_loss']}). The stop is still the plan.", None))
        if r >= 1:
            ev.append(("r_pos1", f"✅ {sym}{tag}\nGoing as planned: now <b>{r:+}R</b> in profit. TP1 {rec['take_profit_1']}, TP2 {rec['take_profit_2']}.", None))
        if r >= 0 and "r_neg" in sent:
            ev.append(("recovered", f"✅ {sym}{tag}\nBack on track: now <b>{r:+}R</b>.", None))
    if rec.get("tp1_hit") and rec.get("outcome") in ("OPEN", "WIN_TP2") and not archived:
        ev.append(("tp1", fmt_event(rec, "breakeven", f"TP1 {rec['take_profit_1']} reached. Moving the stop to the entry "
                                     f"({rec.get('fill_price')}) protects the trade from turning into a loss.", answer), None))
    for w in ([] if archived else rec.get("warnings", [])):
        ev.append((f"warn:{w['kind']}", fmt_event(rec, w["kind"], w["reason"] + " — consider closing early or tightening the stop.", answer), None))
    final = _final_message(rec, sym, tag, answer)
    if final:
        if answer is True and rec.get("fill_price") is None and rec.get("outcome") not in ("WIN_TP2", "LOSS_STOP", "TIMEOUT"):
            final += ("\n⛔ <b>Don't take it anymore.</b> You marked it as taken — if you placed a pending order "
                      "at your broker, <b>cancel it now</b>; if you entered at market, manage it with the stop "
                      f"{rec['stop_loss']}.")
        elif rec.get("fill_price") is None:
            final += "\n⛔ Don't take it anymore."
        ev.append(("final", final, None))
    return ev


def _final_message(rec: dict, sym: str, tag: str, answer: Optional[bool]) -> Optional[str]:
    out, status = rec.get("outcome"), rec.get("status")
    if out in ("WIN_TP2", "LOSS_STOP", "TIMEOUT"):
        return fmt_outcome(rec, answer)
    if status == "CANCELLED":
        return fmt_event(rec, "cancelled", rec.get("cancel_reason") or "setup no longer valid", answer)
    if rec.get("replaced_by"):
        return f"↻ {sym}{tag}\nReplaced by a newer signal on the same pair ({escape(rec.get('close_reason') or '')})."
    if status == "SUPERSEDED":
        return f"↻ {sym}{tag}\nCancelled: an opposite signal appeared on {escape(rec['symbol'])}."
    if status == "INVALIDATED" and rec.get("fill_price") is None:
        return f"❎ {sym}{tag}\nCancelled before entry: price reached the stop level first."
    if out == "NOT_FILLED" or status == "EXPIRED":
        return f"⌛ {sym}{tag}\nExpired without entry — price never reached {rec['entry_price']}."
    return None
