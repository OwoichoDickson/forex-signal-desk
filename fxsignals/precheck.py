""""Check before I enter": is a signal still worth taking right now?

Uses the price from the latest scan (at most ~10 minutes old, its age is stated) — the bot never
opens its own TradingView session, so it cannot disturb a running scan.
"""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from .history import still_valid
from .instruments import instrument
from .notify import in_words, local_hm

MIN_RR = 2.0


def latest_price(cfg: dict, symbol: str) -> tuple[Optional[float], Optional[str]]:
    """(last price, scan time) for ``symbol`` from the newest report."""
    files = sorted(Path(cfg["paths"]["output_dir"]).glob("forex_signals_*.json"))
    if not files:
        return None, None
    try:
        rep = json.loads(files[-1].read_text())
    except (json.JSONDecodeError, OSError):
        return None, None
    for row in rep.get("signals", []) + rep.get("watchlist", []):
        if row.get("symbol") == symbol and (row.get("live") or {}).get("last") is not None:
            return row["live"]["last"], rep.get("scanned_at")
    return None, rep.get("scanned_at")


def check(rec: dict, price: Optional[float], price_at: Optional[str], now: datetime, tz: str = "UTC") -> tuple[bool, str]:
    """(ok to take, explanation). ok is False whenever the signal should no longer be entered."""
    sym = f"<b>{escape(rec['symbol'])} {rec['direction']} {escape(rec['signal_type'].replace('_', ' ').lower())} {rec['entry_price']}</b>"
    buy = rec["direction"] == "BUY"
    end = local_hm(rec.get("expires_at"), tz)
    head_no = f"⛔ <b>Don't take it</b> — {sym}\n"
    if rec.get("outcome") == "OPEN" and rec["signal_type"] != "MARKET_ENTRY":  # market entries count as filled at once
        r = rec.get("open_r")
        return False, (head_no + f"The entry {rec.get('fill_price')} was already reached at {local_hm(rec.get('filled_at'), tz)}"
                       f"{f' and the trade is now {r:+}R' if r is not None else ''}. Entering late changes the risk; wait for the next signal.")
    if rec["signal_type"] == "MARKET_ENTRY" and rec.get("outcome") == "OPEN" and rec.get("tp1_hit"):
        return False, head_no + f"Price already reached TP1 {rec['take_profit_1']} — the move happened without you."
    if not still_valid(rec, now):
        return False, head_no + f"The signal is finished: {escape(rec.get('close_reason') or rec.get('status') or 'no longer valid')}."
    left = (datetime.fromisoformat(rec["expires_at"]) - now).total_seconds() / 60
    if left <= 0:
        return False, head_no + f"Its time ran out at {end}."
    lines = [f"Time left: <b>{in_words(left)}</b> (until {end})."]
    if price is not None:
        inst = instrument(rec["symbol"])
        age = in_words((now - datetime.fromisoformat(price_at)).total_seconds() / 60) if price_at else "?"
        lines.append(f"Price {price} (last scan, {age} ago).")
        if (price >= rec["take_profit_1"]) if buy else (price <= rec["take_profit_1"]):
            return False, head_no + f"Price {price} already reached TP1 {rec['take_profit_1']} — the move happened without you."
        if (price <= rec["stop_loss"]) if buy else (price >= rec["stop_loss"]):
            return False, head_no + f"Price {price} is beyond the stop {rec['stop_loss']}."
        if rec["signal_type"] == "MARKET_ENTRY":
            risk = abs(price - rec["stop_loss"])
            rr = abs(rec["take_profit_2"] - price) / risk if risk else 0
            moved = (price - rec["entry_price"]) * (1 if buy else -1)
            if rr < MIN_RR:
                return False, head_no + (f"Price {price} has run {inst.to_pips(moved):.1f} pips from the entry; from here the "
                                         f"reward is only 1:{rr:.1f} (minimum 1:{MIN_RR:.0f}). Don't chase it.")
            lines.append(f"From this price: stop {inst.to_pips(risk):.1f} pips, reward 1:{rr:.1f}.")
        else:
            gap = inst.to_pips(price - rec["entry_price"])
            reached = (price <= rec["entry_price"]) if buy else (price >= rec["entry_price"])
            lines.append("Price is at the entry level now." if reached else
                         f"Price is {gap:.1f} pips away from the entry. Place the {rec['signal_type'].replace('_', ' ').lower()} "
                         f"at {rec['entry_price']} — don't {'buy' if buy else 'sell'} at market.")
    else:
        lines.append("No recent price available — check the price in your platform first.")
    if rec.get("unconfirmed_since"):
        lines.append(f"ℹ️ The latest scan no longer confirms it ({escape(str(rec.get('unconfirmed_reason')))}). Allowed, but weaker — consider a smaller size.")
    return True, f"✅ <b>OK to take</b> — {sym}\n" + "\n".join(lines) + f"\nStop {rec['stop_loss']} · TP1 {rec['take_profit_1']} · TP2 {rec['take_profit_2']}"


MAX_DAYS = 5  # history.track_outcomes closes a trade as TIMEOUT after this long


def manage(rec: dict, price: Optional[float], price_at: Optional[str], now: datetime, tz: str = "UTC",
           my_price: Optional[float] = None) -> str:
    """"Hold or close?" for a trade that was taken: what the signal's plan says right now.
    ``my_price`` (the user's own fill) is used for the R figure when known."""
    sym = f"<b>{escape(rec['symbol'])} {rec['direction']} {escape(rec['signal_type'].replace('_', ' ').lower())} {rec['entry_price']}</b>"
    buy = rec["direction"] == "BUY"
    out = rec.get("outcome")
    plan = f"Stop {rec['stop_loss']} · TP1 {rec['take_profit_1']} · TP2 {rec['take_profit_2']}"
    if out == "WIN_TP2":
        return f"🏁 <b>Finished — target reached</b> — {sym}\nTP2 {rec['take_profit_2']} was hit ({rec.get('result_r'):+}R). If it is still open in your platform, the plan says <b>close it</b>."
    if out == "LOSS_STOP":
        return f"🏁 <b>Finished — stopped out</b> — {sym}\nThe stop {rec['stop_loss']} was hit. If it is still open in your platform, the plan says <b>close it</b>."
    if out == "BREAKEVEN":
        return f"🏁 <b>Finished — break-even</b> — {sym}\nTP1 was reached, then price came back to the entry (the moved stop, 0R). If it is still open in your platform, the plan says <b>close it</b>."
    if out == "TIMEOUT":
        return f"🏁 <b>Finished — time limit</b> — {sym}\nNo stop or target after {MAX_DAYS} days ({rec.get('result_r'):+}R). The plan says <b>close it</b>."
    if out in (None, "PENDING_FILL", "NOT_FILLED") or rec.get("fill_price") is None:
        if still_valid(rec, now):
            return f"⏳ <b>Not entered yet</b> — {sym}\nThe entry {rec['entry_price']} has not been reached. Keep the pending order until {local_hm(rec.get('expires_at'), tz)}, then cancel it."
        return f"⛔ <b>Never entered</b> — {sym}\nThe signal ended before the entry was reached ({escape(rec.get('close_reason') or out or 'finished')}). If you have a pending order, <b>cancel it</b>."
    fill = my_price or rec["fill_price"]
    risk = abs(fill - rec["stop_loss"]) or 1e-12
    lines, r = [], rec.get("open_r")
    if price is not None:
        age = in_words((now - datetime.fromisoformat(price_at)).total_seconds() / 60) if price_at else "?"
        r = ((price - fill) if buy else (fill - price)) / risk
        lines.append(f"Price {price} (last scan, {age} ago) · {'your' if my_price else 'the'} trade is <b>{r:+.2f}R</b> from {fill}.")
    elif rec.get("open_r") is not None:
        lines.append(f"The trade is {rec['open_r']:+}R (last scan).")
    ends = datetime.fromisoformat(rec["created_at"]).timestamp() + MAX_DAYS * 86400
    lines.append(f"Time limit: {local_hm(datetime.fromtimestamp(ends, now.tzinfo).isoformat(), tz)} on "
                 f"{datetime.fromtimestamp(ends, now.tzinfo):%d %b} — close it then if no stop or target was hit.")
    warns = rec.get("warnings") or []
    if warns:
        why = "; ".join(escape(w["reason"]) for w in warns[-2:])
        against = f" — going against you ({r:+.2f}R)" if r is not None and r < 0 else ""
        head = f"⚠️ <b>Hold with caution — consider closing early</b>{against} — {sym}\n{why}.\nThe plan allows closing early or tightening the stop."
    elif rec.get("tp1_hit"):
        head = (f"🟢 <b>Hold — move your stop to the entry</b> — {sym}\nTP1 {rec['take_profit_1']} was reached. "
                f"With the stop at {fill} the trade can no longer lose; let it run to TP2 {rec['take_profit_2']}.")
    elif r is not None and r < 0:
        to_stop = instrument(rec["symbol"]).to_pips(abs((price if price is not None else fill) - rec["stop_loss"]))
        head = (f"🔴 <b>Going against you ({r:+.2f}R) — hold, the stop is the plan</b> — {sym}\n"
                f"{to_stop:.1f} pips left to the stop {rec['stop_loss']} ({min(100, -r * 100):.0f}% of the way there). "
                f"The setup hasn't changed, so the plan is to keep the stop, not to close early.")
    else:
        head = f"🟢 <b>Hold</b> — {sym}\nNothing has changed: keep the stop and the target."
    if rec.get("unconfirmed_since") and not warns:
        lines.append(f"ℹ️ The latest scan no longer confirms the setup ({escape(str(rec.get('unconfirmed_reason')))}). Not a reason to close by itself.")
    return head + "\n" + "\n".join(lines) + f"\n{plan}"
