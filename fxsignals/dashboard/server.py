"""Local dashboard: a stdlib HTTP server exposing the latest report, history and a scan trigger."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import taken as taken_store
from ..account import load_account, resize_signals, save_account
from ..health import status as health_status
from ..history import SignalHistory, on_board, repeats, still_valid
from ..logging_setup import log
from ..report import blocker_stats, merge_open_signals, refresh_statuses
from ..scanner import ScanBusy, run_scan

logger = logging.getLogger("fxsignals.dashboard")
STATIC = Path(__file__).parent / "static"


def taken_fields(entry) -> dict:
    """The user's own answer, the time they logged it and their entry price."""
    took = taken_store.status(entry)
    entry = entry or {}
    return {"taken": took, "taken_at": entry.get("answered_at") if took is not None else None,
            "taken_price": entry.get("entry_price"), "taken_late": bool(entry.get("late"))}


def serve(cfg: dict) -> None:
    out_dir = Path(cfg["paths"]["output_dir"])
    state_dir = Path(cfg["paths"]["state_dir"])
    scan_state = {"running": False, "last_error": None, "last_finished": None}

    def latest() -> dict:
        files = sorted(out_dir.glob("forex_signals_*.json"))
        if not files:
            return {"account": resize_signals([], load_account(cfg)), "risk_settings": load_account(cfg)}
        now = datetime.now(timezone.utc)
        report = refresh_statuses(json.loads(files[-1].read_text()), now)
        # Every signal that is still valid stays on the board, not only those the latest scan found.
        answers = taken_store.load(state_dir)
        merge_open_signals(report, SignalHistory(state_dir / "signal_history.json", cfg["signals"]["expiry_minutes"]), now, answers)
        # Always size with the balance currently saved, not the one at scan time.
        risk = load_account(cfg)
        report["account"] = resize_signals(report.get("signals", []), risk)
        report["risk_settings"] = risk
        for s in report.get("signals", []):
            s["actionable"] = s.get("status") == "ACTIVE" and bool((s.get("portfolio") or {}).get("allocated"))
            s.update(taken_fields(answers.get(s.get("signal_id"))))
        return report

    def scan_bg(force: bool) -> None:
        scan_state.update(running=True, last_error=None)
        try:
            asyncio.run(run_scan(cfg, force=force))
        except ScanBusy as e:
            scan_state["last_error"] = str(e)
        except Exception as e:  # noqa: BLE001
            scan_state["last_error"] = str(e)
            log(logger, logging.ERROR, "dashboard scan failed", error=str(e))
        finally:
            scan_state.update(running=False, last_finished=datetime.now(timezone.utc).isoformat())

    admins = {e.strip().lower() for e in (cfg["dashboard"].get("admin_emails") or [])}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # route access logs away from stderr noise
            pass

        def identity(self) -> dict:
            """Who is asking. The server only listens on 127.0.0.1, so a request is remote exactly when it
            came through the Cloudflare tunnel, which adds Cf-Connecting-Ip (and the Access login email)."""
            remote = self.headers.get("Cf-Connecting-Ip") is not None
            email = (self.headers.get("Cf-Access-Authenticated-User-Email") or "").strip().lower() or None
            return {"remote": remote, "email": email, "admin": (not remote) or (email is not None and email in admins)}

        def require_admin(self) -> bool:
            who = self.identity()
            if not who["admin"]:
                log(logger, logging.WARNING, "blocked owner-only action", path=self.path, email=who["email"])
                self._json({"error": "Only the owner can do this. Your view is read-only; the planner works in your browser."}, 403)
                return False
            return True

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            """Send with gzip when the client accepts it (the tunnel carries ~350 KB reports otherwise)."""
            if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 6)
                gz = True
            else:
                gz = False
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, "application/json", json.dumps(obj, default=str).encode())

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                # no-store: always serve the latest dashboard after updates
                self._send(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
            elif u.path == "/api/latest":
                rep = latest()
                if not self.identity()["admin"]:
                    # viewers never see the owner's amount; their planner runs in their own browser
                    risk = {**load_account(cfg), "account_balance": None, "risk_per_trade_amount": None}
                    rep["account"] = resize_signals(rep.get("signals", []), risk)
                    rep["risk_settings"] = {k: v for k, v in risk.items() if k not in ("updated_at",)}
                self._json(rep)
            elif u.path == "/api/history":
                h = SignalHistory(state_dir / "signal_history.json", cfg["signals"]["expiry_minutes"])
                now = datetime.now(timezone.utc)
                answers = taken_store.load(state_dir)
                rep = repeats(h.records)
                rows = [{**r, "repeat_of": rep.get(r.get("signal_id")), "still_valid": still_valid(r, now),
                         "on_board": on_board(r, now, taken_store.status(answers.get(r.get("signal_id")))),
                         **taken_fields(answers.get(r.get("signal_id")))}
                        for r in h.recent(2000, now)]
                self._json({"signals": rows, "summary": {**h.track_record(), "total_generated": len(h.records)},
                            "mine": taken_store.my_track_record(h.records, answers)})
            elif u.path == "/api/health":
                self._json(health_status(cfg))
            elif u.path == "/api/backtest":
                files = sorted((state_dir / "backtests").glob("backtest_*.json"))
                if not files:
                    self._json({"available": False, "schedule": cfg.get("backtest_schedule")})
                else:
                    r = json.loads(files[-1].read_text())
                    r.pop("trades", None)
                    self._json({"available": True, "schedule": cfg.get("backtest_schedule"), **r})
            elif u.path == "/api/blockers":
                hours = int(parse_qs(u.query).get("hours", ["24"])[0] or 24)
                self._json(blocker_stats(str(state_dir), max(1, min(hours, 24 * 30))))
            elif u.path == "/api/scan-status":
                sched = state_dir / "scheduler.json"
                self._json({**scan_state, "schedule": json.loads(sched.read_text()) if sched.exists() else None})
            elif u.path == "/api/account":
                if self.require_admin():
                    self._json(load_account(cfg))
            elif u.path == "/api/me":
                self._json(self.identity())
            elif u.path == "/api/chart":
                pair = parse_qs(u.query).get("pair", [""])[0].upper()
                f = state_dir / "chart_data.json"
                data = json.loads(f.read_text()) if f.exists() else {}
                self._json(data.get(pair) or {"error": "no chart data for pair"}, 200 if pair in data else 404)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path in ("/api/account", "/api/scan", "/api/taken") and not self.require_admin():
                return
            if u.path == "/api/taken":
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 2_000)
                    data = json.loads(self.rfile.read(n) or b"{}")
                    sid, took = str(data["signal_id"]), bool(data["taken"])
                    price = float(data["entry_price"]) if data.get("entry_price") not in (None, "") else None
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    return self._json({"saved": False, "error": "signal_id and taken required; entry_price must be a number"}, 400)
                h = SignalHistory(state_dir / "signal_history.json", cfg["signals"]["expiry_minutes"])
                rec = next((r for r in h.records if r.get("signal_id") == sid), None)
                late = took and rec is not None and not still_valid(rec, datetime.now(timezone.utc))
                return self._json({"saved": True, **taken_store.record(state_dir, sid, took, via="dashboard",
                                                                        entry_price=price if took else None, late=late)})
            if u.path == "/api/account":
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                    data = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    return self._json({"saved": False, "errors": ["invalid JSON"]}, 400)
                acct, errors = save_account(cfg, data)
                if errors:
                    return self._json({"saved": False, "errors": errors}, 400)
                log(logger, logging.INFO, "account settings updated", balance=acct["account_balance"],
                    risk_pct=acct["risk_per_trade_pct"])
                return self._json({"saved": True, "account": acct})
            if u.path != "/api/scan":
                return self._json({"error": "not found"}, 404)
            if scan_state["running"]:
                return self._json({"started": False, "reason": "scan already running"}, 409)
            force = parse_qs(u.query).get("force", ["0"])[0] == "1"
            threading.Thread(target=scan_bg, args=(force,), daemon=True).start()
            self._json({"started": True}, 202)

    host, port = cfg["dashboard"]["host"], cfg["dashboard"]["port"]
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard: http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
