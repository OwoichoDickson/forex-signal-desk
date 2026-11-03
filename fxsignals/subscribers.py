"""People other than the owner who asked to receive signals (owner approves each one in Telegram).

state/telegram_subscribers.json = {chat_id: {name, username, status, requested_at, decided_at}}
status: pending | approved | denied. Followers only receive signal messages; their taps and
commands never touch the owner's trade log.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import filelock
from .history import atomic_write_json

FILE = "telegram_subscribers.json"
SENT = "telegram_sent.json"   # follower messages that /delete can still remove
DELETE_WINDOW_H = 48         # Telegram only lets a bot delete its messages for 48 hours


def _path(state_dir) -> Path:
    return Path(state_dir) / FILE


def load(state_dir) -> dict:
    p = _path(state_dir)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def get(state_dir, chat_id) -> Optional[dict]:
    return load(state_dir).get(str(chat_id))


def request(state_dir, chat: dict) -> dict:
    """Add a new pending request (or return the existing entry unchanged)."""
    subs = load(state_dir)
    cid = str(chat["id"])
    if cid not in subs:
        name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("title") or cid
        subs[cid] = {"name": name, "username": chat.get("username"), "status": "pending",
                     "requested_at": datetime.now(timezone.utc).isoformat()}
        atomic_write_json(_path(state_dir), subs)
    return subs[cid]


def decide(state_dir, chat_id, status: str) -> Optional[dict]:
    subs = load(state_dir)
    entry = subs.get(str(chat_id))
    if entry is None:
        return None
    entry.update(status=status, decided_at=datetime.now(timezone.utc).isoformat())
    atomic_write_json(_path(state_dir), subs)
    return entry


def remove(state_dir, chat_id) -> Optional[dict]:
    subs = load(state_dir)
    entry = subs.pop(str(chat_id), None)
    if entry is not None:
        atomic_write_json(_path(state_dir), subs)
    return entry


def find(state_dir, who: str) -> Optional[str]:
    """Chat id from '123456', '@username' or 'username'."""
    who = who.strip().lstrip("@").lower()
    for cid, e in load(state_dir).items():
        if who == cid or who == (e.get("username") or "").lower():
            return cid
    return None


def approved(state_dir) -> list[str]:
    return [cid for cid, e in load(state_dir).items() if e.get("status") == "approved"]


def label(cid: str, e: dict) -> str:
    return f"{e.get('name') or cid}" + (f" (@{e['username']})" if e.get("username") else "") + f" · id {cid}"


def _sent_path(state_dir) -> Path:
    return Path(state_dir) / SENT


def _update_sent(state_dir, change) -> list[dict]:
    p = _sent_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "w") as lk:
        filelock.lock(lk)
        try:
            try:
                rows = json.loads(p.read_text()) if p.exists() else []
            except json.JSONDecodeError:
                rows = []
            cutoff = datetime.now(timezone.utc).timestamp() - DELETE_WINDOW_H * 3600
            rows = change([r for r in rows if datetime.fromisoformat(r["at"]).timestamp() > cutoff])
            atomic_write_json(p, rows)
            return rows
        finally:
            filelock.unlock(lk)


def record_sent(state_dir, rows: list[dict]) -> None:
    if rows:
        at = datetime.now(timezone.utc).isoformat()
        _update_sent(state_dir, lambda old: old + [{**r, "at": at} for r in rows])


def sent(state_dir) -> list[dict]:
    """Follower messages from the last 48 hours, oldest first."""
    return _update_sent(state_dir, lambda old: old)


def forget_sent(state_dir, rows: list[dict]) -> None:
    gone = {(str(r["chat"]), r["msg"]) for r in rows}
    _update_sent(state_dir, lambda old: [r for r in old if (str(r["chat"]), r["msg"]) not in gone])
