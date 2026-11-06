"""Forex market session handling (Sunday 17:00 to Friday 17:00, New York time).

Bank holidays are not modelled; staleness checks on live data catch those days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROLL_HOUR = 17  # FX trading day rolls at 17:00 New York


def is_fx_open(now: datetime) -> bool:
    ny = now.astimezone(NY)
    wd = ny.weekday()  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return ny.hour >= ROLL_HOUR
    if wd == 4:  # Friday
        return ny.hour < ROLL_HOUR
    return True


def is_metal_open(now: datetime) -> bool:
    """Gold also pauses for the daily 17:00-18:00 New York maintenance break."""
    if not is_fx_open(now):
        return False
    return now.astimezone(NY).hour != ROLL_HOUR


def is_instrument_open(name: str, now: datetime) -> bool:
    if name.upper().startswith("BTC"):
        return True  # crypto trades 24/7
    return is_metal_open(now) if name.upper().startswith(("XAU", "XAG")) else is_fx_open(now)


def next_open(now: datetime) -> datetime:
    """Next Sunday 17:00 New York open (or ``now`` if the market is already open)."""
    if is_fx_open(now):
        return now
    ny = now.astimezone(NY)
    days_ahead = (6 - ny.weekday()) % 7
    candidate = (ny + timedelta(days=days_ahead)).replace(hour=ROLL_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= ny:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def market_status(now: datetime) -> str:
    return "OPEN" if is_fx_open(now) else "CLOSED"
