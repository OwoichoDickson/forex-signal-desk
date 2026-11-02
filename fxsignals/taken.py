"""Which signals the user actually traded ("taken") or skipped.

Kept in its own file (state/taken.json) because two processes write it — the Telegram bot and
the dashboard — while the scanner rewrites the signal history; an exclusive file lock serialises writers.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import filelock
from .history import atomic_write_json


def _path(state_dir) -> Path:
    return Path(state_dir) / "taken.json"


@contextmanager
def _locked(state_dir):
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "w") as lock:
        filelock.lock(lock)
        try:
            yield p
        finally:
            filelock.unlock(lock)


def load(state_dir) -> dict[str, dict]:
    p = _path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def record(state_dir, signal_id: str, taken: bool, via: str, note: Optional[str] = None,
           entry_price: Optional[float] = None, late: Optional[bool] = None) -> dict:
    """Save the answer. ``answered_at`` doubles as the time the user says they took the trade."""
    with _locked(state_dir) as p:
        data = load(state_dir)
        entry = {k: v for k, v in data.get(signal_id, {}).items() if k == "asked"}
        entry.update(taken=taken, answered_at=datetime.now(timezone.utc).isoformat(), via=via)
        if note:
            entry["note"] = note
        if entry_price is not None:
            entry["entry_price"] = entry_price
        if late:
            entry["late"] = True
        data[signal_id] = entry
        atomic_write_json(p, data)
    return entry


def set_entry_price(state_dir, signal_id: str, price: float) -> Optional[dict]:
    """Add the user's own fill price to a trade already marked as taken."""
    with _locked(state_dir) as p:
        data = load(state_dir)
        entry = data.get(signal_id)
        if not entry or entry.get("taken") is not True:
            return None
        entry["entry_price"] = price
        atomic_write_json(p, data)
    return entry


def mark_asked(state_dir, signal_id: str, stage: str) -> bool:
    """Remember that we asked at ``stage`` ('new' / 'filled'); returns False if already asked."""
    with _locked(state_dir) as p:
        data = load(state_dir)
        entry = data.setdefault(signal_id, {})
        asked = entry.setdefault("asked", [])
        if stage in asked:
            return False
        asked.append(stage)
        atomic_write_json(p, data)
    return True


def status(entry: Optional[dict]) -> Optional[bool]:
    """True = took it, False = skipped, None = not answered yet."""
    return None if not entry or "taken" not in entry else bool(entry["taken"])


def my_track_record(history_records: list[dict], taken: dict[str, dict]) -> dict:
    mine = [r for r in history_records if status(taken.get(r.get("signal_id"))) is True]
    done = [r for r in mine if r.get("outcome") in ("WIN_TP2", "LOSS_STOP", "TIMEOUT")]
    rs = [r.get("result_r") or 0.0 for r in done]
    wins = sum(1 for r in done if r["outcome"] == "WIN_TP2")
    return {
        "taken": len(mine),
        "skipped": sum(1 for r in history_records if status(taken.get(r.get("signal_id"))) is False),
        "unanswered": sum(1 for r in history_records if status(taken.get(r.get("signal_id"))) is None),
        "closed": len(done), "wins": wins, "losses": sum(1 for r in done if r["outcome"] == "LOSS_STOP"),
        "win_rate_pct": round(100 * wins / len(done), 1) if done else None,
        "total_r": round(sum(rs), 2),
        "open": sum(1 for r in mine if r.get("outcome") == "OPEN"),
    }
