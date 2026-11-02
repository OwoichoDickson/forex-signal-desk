"""Scanner health: detect problems, alert once (plus reminders), announce recovery, ping the heartbeat."""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .history import atomic_write_json
from .logging_setup import log
from .notify import Telegram, fmt_health, heartbeat, tls_context

logger = logging.getLogger("fxsignals.health")
REMIND_EVERY = timedelta(hours=2)


def internet_ok(timeout: float = 5.0) -> bool:
    try:
        urllib.request.urlopen("https://api.telegram.org", timeout=timeout, context=tls_context())  # noqa: S310
        return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def classify_error(err: str, online: bool) -> str:
    e = err.lower()
    if not online:
        return "No internet connection on the Mac — scans and alerts are paused."
    if any(k in e for k in ("econnrefused", "cdp", "no tradingview chart target", "not ready", "9222", "connect")):
        return ("TradingView Desktop is closed or not reachable (remote debugging port 9222). "
                "Open it with \"TradingView for Signals\" on your Desktop (the Dock icon starts it without that port), and scans resume.")
    if "server not found" in e or "mcp server" in e:
        return "The TradingView MCP server could not start (check ~/tradingview-mcp)."
    if "timed out" in e or "timeout" in e:
        return "TradingView stopped responding (timeouts). It may be frozen — try restarting it."
    return f"Scan failed: {err[:200]}"


def data_problem(report: dict) -> Optional[str]:
    """A scan that 'worked' but got little usable data is also a problem worth an alert."""
    items = report.get("watchlist", []) + report.get("signals", [])
    if not items:
        return "Scan returned no pairs."
    bad = sum(1 for w in report.get("watchlist", []) if w.get("wait_code") in ("INSUFFICIENT_DATA", "STALE_DATA", "DATA_MISMATCH"))
    if bad >= max(3, len(items) // 2):
        return (f"{bad} of {len(items)} pairs had missing or stale data — TradingView may be disconnected, "
                "logged out, or not updating.")
    return None


class HealthMonitor:
    def __init__(self, cfg: dict):
        self.path = Path(cfg["paths"]["state_dir"]) / "health.json"
        self.tg = Telegram(cfg)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self) -> None:
        atomic_write_json(self.path, self.state)

    def ok(self, note: str = "scan ok") -> None:
        now = datetime.now(timezone.utc)
        if self.state.get("problem"):
            log(logger, logging.INFO, "scanner recovered", problem=self.state["problem"])
            self.tg.send(fmt_health(None, None))
        else:
            self.tg.flush()  # deliver anything queued while offline
        self.state = {"problem": None, "since": None, "last_ok": now.isoformat(), "note": note}
        self._save()
        heartbeat(True, note)

    def alive(self, note: str) -> None:
        """Scheduler is running but did not scan (weekend / too soon): keep the heartbeat, keep any open problem."""
        self.tg.flush()
        heartbeat(True, note)

    def problem(self, text: str) -> None:
        now = datetime.now(timezone.utc)
        same = self.state.get("problem") == text
        since = self.state.get("since") if same else now.isoformat()
        last_alert = self.state.get("alerted_at")
        due = (not same) or not last_alert or now - datetime.fromisoformat(last_alert) >= REMIND_EVERY
        if due:
            self.tg.send(fmt_health(text, since))
            last_alert = now.isoformat()
        log(logger, logging.ERROR, "scanner problem", problem=text, alerted=due)
        self.state = {**self.state, "problem": text, "since": since, "alerted_at": last_alert}
        self._save()
        heartbeat(False, text)


def status(cfg: dict) -> dict:
    p = Path(cfg["paths"]["state_dir"]) / "health.json"
    return json.loads(p.read_text()) if p.exists() else {"problem": None}
