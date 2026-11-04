"""Signal history: duplicate prevention, expiry, invalidation and tracking."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .logging_setup import log
from .market_hours import is_fx_open, next_open

logger = logging.getLogger("fxsignals.history")

OPEN_STATES = {"ACTIVE", "PENDING", "MARKET_CLOSED"}


def style_of(r: dict) -> str:
    """SCALP (short, closed after max_hold_minutes) or INTRADAY (the normal signals)."""
    return r.get("style") or "INTRADAY"


def _h1_agrees(h1, t: int, lo: float, hi: float) -> bool:
    """Does the H1 bar containing this M5 bar span its high and low? Unknown hour -> trust the M5 bar."""
    for j in range(len(h1.t) - 1, -1, -1):
        if h1.t[j] <= t < h1.t[j] + 3600:
            tol = 1e-9 + 0.0002 * h1.c[j]  # tiny feed differences between the two loads
            return h1.l[j] - tol <= lo and hi <= h1.h[j] + tol
        if h1.t[j] + 3600 <= t:
            return True
    return True


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def effective_status(sig: dict, now: datetime) -> str:
    """Status as of ``now``: an open signal past its expiry is EXPIRED, never ACTIVE."""
    status = sig.get("status")
    if status in OPEN_STATES and sig.get("expires_at") and now >= _parse(sig["expires_at"]):
        return "EXPIRED"
    if status == "MARKET_CLOSED" and is_fx_open(now):
        return "PENDING_REVALIDATION"
    return status


FINAL_OUTCOMES = {"WIN_TP2", "LOSS_STOP", "BREAKEVEN", "TIMEOUT", "NOT_FILLED"}
CLOSED_OUTCOMES = ("WIN_TP2", "LOSS_STOP", "BREAKEVEN", "TIMEOUT")  # entered and finished


def still_valid(rec: dict, now: datetime) -> bool:
    """A signal stays on the board while its order can still fill or its trade is still running."""
    if rec.get("outcome") == "OPEN":
        return True  # entered: shown until TP2, stop or time limit
    if rec.get("outcome") in FINAL_OUTCOMES:
        return False
    return effective_status(rec, now) in OPEN_STATES | {"PENDING_REVALIDATION"}


def on_board(rec: dict, now: datetime, taken: Optional[bool] = None) -> bool:
    """Open list vs archive: a signal stays on the board until its entry deadline ('don't take after');
    after that it is archived, unless you took it — then it stays until its trade finishes."""
    if not still_valid(rec, now):
        return False
    if taken is True:
        return True
    end = rec.get("expires_at")
    return end is None or now < datetime.fromisoformat(end)


def display_status(rec: dict, now: datetime) -> str:
    if rec.get("outcome") == "OPEN":
        return "RUNNING"
    if rec.get("outcome") == "PENDING_FILL" and effective_status(rec, now) in OPEN_STATES:
        return "PENDING"
    return effective_status(rec, now)


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.chmod(tmp, 0o644)  # mkstemp creates 0600; reports are meant to be readable
    os.replace(tmp, path)


class SignalHistory:
    def __init__(self, path: Path, expiry_minutes: dict[str, int]):
        self.path = path
        self.expiry = expiry_minutes
        self.records: list[dict] = []
        if path.exists():
            try:
                self.records = json.loads(path.read_text()).get("signals", [])
            except (json.JSONDecodeError, OSError):
                self.records = []

    def save(self) -> None:
        atomic_write_json(self.path, {"updated_at": datetime.now(timezone.utc).isoformat(), "signals": self.records[-2000:]})

    def open_records(self, now: datetime) -> list[dict]:
        return [r for r in self.records if effective_status(r, now) in OPEN_STATES]

    def track_outcomes(self, bars: dict, now: datetime, max_days: int = 5, confirm: Optional[dict] = None) -> None:
        """Follow every signal on M5 candles: fill, then stop vs TP2 (main target), first touch wins.

        A bar touching both the stop and the target counts as a stop (conservative). Once TP1 is reached
        the plan moves the stop to the entry, so from the next bar on the stop is the fill price and a
        return to it is BREAKEVEN (0R) instead of a full loss. TP1/TP3 touches are recorded.
        ``confirm`` (H1 candles per pair, loaded separately) must also reach a stop/target an M5 bar touches:
        a bad M5 bar (e.g. another symbol's price caught while the chart switched) is ignored. Pending orders never filled before expiry are NOT_FILLED. This measures whether
        the published levels worked; it ignores spread/slippage after entry.
        """
        for r in self.records:
            if r.get("outcome") not in (None, "OPEN", "PENDING_FILL"):
                continue
            c = bars.get(r["symbol"])
            created = _parse(r["created_at"]).timestamp()
            buy = r["direction"] == "BUY"
            if c is None or not c.t:
                continue
            idx = [i for i, t in enumerate(c.t) if t >= created - 300]  # include the bar the signal formed in
            if not idx:
                continue
            fill_i = None
            if r.get("filled_at"):
                fill_i = next((i for i in idx if c.t[i] >= _parse(r["filled_at"]).timestamp()), None)
            elif r["signal_type"] == "MARKET_ENTRY":
                fill_i = idx[0]
                r["fill_price"] = r["entry_price"] if c.t[fill_i] <= created else c.o[fill_i]
            else:
                deadline = _parse(r["expires_at"]).timestamp()
                e = r["entry_price"]
                limit = "LIMIT" in r["signal_type"]
                for i in idx:
                    if c.t[i] > deadline:
                        break
                    hit = (c.l[i] <= e <= c.h[i]) if limit else (c.h[i] >= e if buy else c.l[i] <= e)
                    if hit:
                        fill_i, r["fill_price"] = i, e
                        break
                if fill_i is None:
                    r["outcome"] = "NOT_FILLED" if now.timestamp() > deadline else "PENDING_FILL"
                    if r["outcome"] == "NOT_FILLED":
                        r["outcome_at"] = now.isoformat()
                    continue
            if fill_i is None:
                continue
            r.setdefault("filled_at", datetime.fromtimestamp(c.t[fill_i], timezone.utc).isoformat())
            r["outcome"] = "OPEN"
            fill = r["fill_price"]
            risk = abs(fill - r["stop_loss"]) or 1e-12
            at_entry = False  # stop moved to the entry (TP1 reached on an earlier bar)
            h1 = (confirm or {}).get(r["symbol"])
            close_by = c.t[fill_i] + r["max_hold_minutes"] * 60 if r.get("max_hold_minutes") else None
            for i in range(fill_i, len(c.t)):
                lo, hi = c.l[i], c.h[i]
                if close_by is not None and c.t[i] >= close_by:
                    # scalp time limit: closed at the open of the first bar after it (no stop or target before)
                    px = c.o[i]
                    r.update(outcome="TIMEOUT", outcome_at=datetime.fromtimestamp(c.t[i], timezone.utc).isoformat(),
                             result_r=round(((px - fill) if buy else (fill - px)) / risk, 2), exit_price=px)
                    break
                if h1 is not None and not _h1_agrees(h1, c.t[i], lo, hi):
                    log(logger, logging.WARNING, "M5 bar ignored: H1 does not confirm it", symbol=r["symbol"],
                        bar=datetime.fromtimestamp(c.t[i], timezone.utc).isoformat(), low=lo, high=hi)
                    continue
                fav = hi if buy else lo
                stop = fill if at_entry else r["stop_loss"]
                stop_hit = (lo <= stop) if buy else (hi >= stop)
                tp2_hit = (hi >= r["take_profit_2"]) if buy else (lo <= r["take_profit_2"])
                if (fav >= r["take_profit_1"]) if buy else (fav <= r["take_profit_1"]):
                    r["tp1_hit"] = True
                if (fav >= r["take_profit_3"]) if buy else (fav <= r["take_profit_3"]):
                    r["tp3_hit"] = True
                if stop_hit or tp2_hit:
                    win = tp2_hit and not stop_hit
                    exit_px = r["take_profit_2"] if win else stop
                    r.update(outcome="WIN_TP2" if win else "BREAKEVEN" if at_entry else "LOSS_STOP",
                             outcome_at=datetime.fromtimestamp(c.t[i], timezone.utc).isoformat(),
                             result_r=round(((exit_px - fill) if buy else (fill - exit_px)) / risk, 2))
                    break
                if r.get("tp1_hit"):
                    at_entry = True
            else:
                if now.timestamp() - created > max_days * 86400:
                    last = c.c[-1]
                    r.update(outcome="TIMEOUT", outcome_at=now.isoformat(),
                             result_r=round(((last - fill) if buy else (fill - last)) / risk, 2))
                else:
                    last = c.c[-1]
                    r["open_r"] = round(((last - fill) if buy else (fill - last)) / risk, 2)

    def track_record(self) -> dict:
        done = [r for r in self.records if r.get("outcome") in CLOSED_OUTCOMES]
        rs = [r.get("result_r") or 0.0 for r in done]
        wins = sum(1 for r in done if r["outcome"] == "WIN_TP2")
        n = len(done)
        return {
            "closed_trades": n,
            "wins_tp2": wins,
            "losses_stop": sum(1 for r in done if r["outcome"] == "LOSS_STOP"),
            "breakeven": sum(1 for r in done if r["outcome"] == "BREAKEVEN"),
            "timeouts": sum(1 for r in done if r["outcome"] == "TIMEOUT"),
            "win_rate_pct": round(100 * wins / n, 1) if n else None,
            "avg_r": round(sum(rs) / n, 2) if n else None,
            "total_r": round(sum(rs), 2),
            "tp1_touch_rate_pct": round(100 * sum(1 for r in done if r.get("tp1_hit")) / n, 1) if n else None,
            "open": sum(1 for r in self.records if r.get("outcome") == "OPEN"),
            "awaiting_fill": sum(1 for r in self.records if r.get("outcome") == "PENDING_FILL"),
            "not_filled": sum(1 for r in self.records if r.get("outcome") == "NOT_FILLED"),
            "note": ("Live track record of published levels (stop vs TP2, first touch, stop moved to entry after TP1, "
                     "no costs after entry). "
                     + ("Fewer than 30 closed trades — not yet meaningful." if n < 30 else "")).strip(),
        }

    def supersede_older(self, now: datetime) -> list[dict]:
        """One open order per pair and direction: the newest unfilled signal replaces older unfilled ones.
        Entered trades (outcome OPEN) are never touched — they run to their stop or target."""
        newest: dict[tuple, dict] = {}
        replaced = []
        for r in sorted(self.records, key=lambda r: r.get("created_at") or ""):
            if r.get("outcome") in ("OPEN",) or r.get("outcome") in FINAL_OUTCOMES:
                continue
            if effective_status(r, now) not in OPEN_STATES:
                continue
            key = (style_of(r), r["symbol"], r["direction"])
            old = newest.get(key)
            if old is not None:
                old.update(status="SUPERSEDED", closed_at=now.isoformat(), actionable=False,
                           close_reason=f"replaced by newer signal {r['signal_id']} ({r['signal_type']} at {r['entry_price']})",
                           outcome="NOT_FILLED", outcome_at=now.isoformat(), replaced_by=r["signal_id"])
                replaced.append(old)
            newest[key] = r
        return replaced

    def refresh(self, prices: dict[str, float], now: datetime, bars: Optional[dict] = None,
                confirm: Optional[dict] = None) -> list[dict]:
        """Expire / invalidate open signals using M5 candles (when given, checked against H1) and the latest prices."""
        if bars:
            self.track_outcomes(bars, now, confirm=confirm)
        changed = self.supersede_older(now)
        for r in self.records:
            if r.get("status") not in OPEN_STATES:
                continue
            if r.get("outcome") in ("LOSS_STOP", "WIN_TP2", "BREAKEVEN", "TIMEOUT"):
                hit = r["outcome"] == "WIN_TP2"
                why = ("TP2 reached" if hit else "stop at entry reached after TP1 (break-even)"
                       if r["outcome"] == "BREAKEVEN" else "time limit reached" if r["outcome"] == "TIMEOUT"
                       else "stop-loss reached")
                r.update(status="TARGET_HIT" if hit else "INVALIDATED", closed_at=now.isoformat(), actionable=False,
                         close_reason=f"{why} at {r['outcome_at']}")
                changed.append(r)
                continue
            if r.get("outcome") == "OPEN" and r.get("tp1_hit") and r["signal_type"] == "MARKET_ENTRY":
                r.update(status="TARGET_HIT", closed_at=now.isoformat(), actionable=False,
                         close_reason="TP1 reached; no longer a fresh entry (outcome still tracked to TP2/stop)")
                changed.append(r)
                continue
            new, why = None, None
            if effective_status(r, now) == "EXPIRED":
                new, why = "EXPIRED", "expiry time reached"
            else:
                p = prices.get(r["symbol"])
                if p is not None:
                    buy = r["direction"] == "BUY"
                    if (p <= r["stop_loss"]) if buy else (p >= r["stop_loss"]):
                        new, why = "INVALIDATED", f"price {p} crossed stop {r['stop_loss']}"
                    elif (p >= r["take_profit_1"]) if buy else (p <= r["take_profit_1"]):
                        if r["signal_type"] == "MARKET_ENTRY":
                            new, why = "TARGET_HIT", f"price {p} reached TP1 {r['take_profit_1']}"
                        else:
                            new, why = "EXPIRED", f"price {p} reached TP1 before the pending entry; setup missed"
            if new:
                r.update(status=new, closed_at=now.isoformat(), close_reason=why, actionable=False)
                changed.append(r)
        return changed

    def blocked(self, symbol: str, direction: str, now: datetime, cooldown_hours: float,
                style: str = "INTRADAY") -> Optional[tuple[str, str]]:
        """Why a new signal on this pair must not be published: a trade on it is still running (one idea is
        never sent twice), or the same idea was stopped out within the cooldown. Returns (code, reason)."""
        for r in self.records:
            if r["symbol"] != symbol or style_of(r) != style:
                continue
            if r.get("outcome") == "OPEN":
                return ("ALREADY_RUNNING", f"A {r['direction']} trade from {r['created_at'][11:16]} UTC on {symbol} is still "
                                           f"running — no new signal until it reaches its stop or target")
            if (r["direction"] == direction and r.get("outcome") == "LOSS_STOP" and r.get("outcome_at")
                    and now - _parse(r["outcome_at"]) < timedelta(hours=cooldown_hours)):
                until = _parse(r["outcome_at"]) + timedelta(hours=cooldown_hours)
                return ("COOLDOWN_AFTER_LOSS", f"The last {symbol} {direction} was stopped out at {r['outcome_at'][11:16]} UTC — "
                                               f"no new {direction} until {until:%H:%M} UTC")
        return None

    def register(self, sig: dict, now: datetime) -> dict:
        """Add a new signal or merge it into an equivalent open one (no duplicates)."""
        key = f"{sig['symbol']}|{sig['direction']}|{sig['signal_type']}"
        if style_of(sig) != "INTRADAY":
            key = f"{style_of(sig)}|{key}"
        tol = 0.5 * (sig.get("atr") or 0)
        for r in self.records:
            if r.get("status") not in OPEN_STATES or effective_status(r, now) not in OPEN_STATES:
                continue
            if r["symbol"] == sig["symbol"] and r["direction"] != sig["direction"] and style_of(r) == style_of(sig):
                r.update(status="SUPERSEDED", closed_at=now.isoformat(), close_reason="opposite signal generated", actionable=False)
                continue
            if r.get("key") == key and abs(r["entry_price"] - sig["entry_price"]) <= tol:
                # Same setup: keep original id/created/expiry (expiry is never extended), refresh live fields.
                keep = {k: r[k] for k in ("signal_id", "created_at", "expires_at", "key")}
                r.update({**sig, **keep, "updated_at": now.isoformat(), "scan_count": r.get("scan_count", 1) + 1})
                return {**r, "duplicate_of_existing": True}
        start = now if is_fx_open(now) else next_open(now)
        minutes = sig.get("entry_minutes") or self.expiry.get(sig["signal_type"], 60)
        rec = {
            **sig,
            "signal_id": uuid.uuid4().hex[:12],
            "key": key,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (start + timedelta(minutes=minutes)).isoformat(),
            "scan_count": 1,
        }
        self.records.append(rec)
        self.supersede_older(now)  # same idea on the same pair: keep only this newest order
        return {**rec, "duplicate_of_existing": False}

    def recent(self, limit: int = 200, now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        out = []
        for r in self.records[-limit:][::-1]:
            out.append({**r, "status": effective_status(r, now), "actionable": effective_status(r, now) == "ACTIVE" and r.get("actionable", False)})
        return out
