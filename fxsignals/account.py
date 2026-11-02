"""Optional planning amount + risk settings entered in the dashboard, and live re-sizing of signals.

Nothing is read from TradingView or any broker. The amount is whatever the user types
("if I trade with $100, what lot size?"); when it is empty, signals carry no lot size and the
risk limits still work in percentage terms. Saved to state/account.json, overriding config.yaml.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .history import atomic_write_json
from .instruments import instrument
from .risk import apply_portfolio_limits

FIELDS = {
    # name: (type, min, max)
    "account_balance": (float, 1.0, 1e9),
    "risk_per_trade_amount": (float, 0.01, 1e7),  # fixed money risked per trade; overrides the % when set
    "risk_per_trade_pct": (float, 0.1, 5.0),
    "max_open_positions": (int, 1, 10),
    "max_daily_loss_pct": (float, 0.5, 20.0),
    "daily_loss_used_pct": (float, 0.0, 100.0),
    "max_currency_exposure_pct": (float, 0.5, 20.0),
    "max_total_risk_pct": (float, 0.5, 20.0),
}
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"}


def _path(cfg: dict) -> Path:
    return Path(cfg["paths"]["state_dir"]) / "account.json"


def load_account(cfg: dict) -> dict:
    saved = {}
    p = _path(cfg)
    if p.exists():
        try:
            saved = json.loads(p.read_text())
        except json.JSONDecodeError:
            saved = {}
    risk = {**cfg["risk"], **{k: v for k, v in saved.items() if k in FIELDS or k == "account_currency"}}
    if "account_balance" not in saved:  # only an amount the user typed is ever used for sizing
        risk["account_balance"] = None
    if "risk_per_trade_amount" not in saved:
        risk["risk_per_trade_amount"] = None
    risk.setdefault("max_total_risk_pct", risk["risk_per_trade_pct"] * risk["max_open_positions"])
    risk["source"] = "dashboard" if saved else "config.yaml"
    risk["updated_at"] = saved.get("updated_at")
    return risk


def validate(data: dict) -> tuple[dict, list[str]]:
    clean, errors = {}, []
    for k, (typ, lo, hi) in FIELDS.items():
        if k not in data or data[k] in (None, ""):
            continue
        try:
            v = typ(float(data[k]))
        except (TypeError, ValueError):
            errors.append(f"{k} must be a number")
            continue
        if not (lo <= v <= hi) or (isinstance(v, float) and not math.isfinite(v)):
            errors.append(f"{k} must be between {lo} and {hi}")
            continue
        clean[k] = v
    if "account_currency" in data and data["account_currency"]:
        c = str(data["account_currency"]).upper()
        if c in CURRENCIES:
            clean["account_currency"] = c
        else:
            errors.append(f"account_currency must be one of {', '.join(sorted(CURRENCIES))}")
    return clean, errors


def save_account(cfg: dict, data: dict) -> tuple[Optional[dict], list[str]]:
    clean, errors = validate(data)
    if errors:
        return None, errors
    clean["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(_path(cfg), clean)
    return load_account(cfg), []


def resize_signals(signals: list[dict], risk: dict) -> dict:
    """Recompute lots and portfolio allocation for the given account, from each signal's stop pips and pip value.

    Returns a summary: how many trades fit, total money at risk, and the worst case if every stop is hit.
    """
    balance, pct = risk.get("account_balance"), risk["risk_per_trade_pct"]
    fixed = risk.get("risk_per_trade_amount")
    if not balance and not fixed:
        return _limits_without_amount(signals, risk)
    step, min_lot = risk.get("lot_step", 0.01), risk.get("min_lot", 0.01)
    budget = fixed if fixed else balance * pct / 100.0
    for s in signals:
        ps = dict(s.get("position_size") or {})
        pv, pips = ps.get("pip_value_per_lot"), ps.get("stop_pips")
        if not pv or not pips:
            continue
        lots = math.floor(budget / (pips * pv) / step + 1e-9) * step
        note = None
        # Always give a size: the standard minimum and what it risks, plus the micro/cent-account size
        # (0.001-lot steps) that fits the budget, so a small account still gets a usable recommendation.
        min_risk = min_lot * pips * pv
        micro = math.floor(budget / (pips * pv) / 0.001 + 1e-9) * 0.001
        ps.update(min_lot=min_lot, min_lot_risk=round(min_risk, 2),
                  min_lot_risk_pct=round(100 * min_risk / balance, 2) if balance else None,
                  micro_lots=round(micro, 3), micro_risk=round(micro * pips * pv, 2))
        tolerance = risk.get("min_lot_tolerance", 1.25)
        if lots < min_lot and min_risk <= budget * tolerance:
            # only slightly over budget: use the broker minimum and say so
            lots = min_lot
            note = (f"Uses the minimum {min_lot} lot, risking {min_risk:.2f} ({ps['min_lot_risk_pct']}%) — "
                    f"slightly above your {budget:.2f} per trade.")
        elif lots < min_lot:
            lots = 0.0
            note = (f"Standard minimum {min_lot} lot risks {min_risk:.2f} ({ps['min_lot_risk_pct']}%), well above your "
                    f"{budget:.2f} per trade. Micro/cent account: {micro:.3f} lot risks {micro * pips * pv:.2f}.")
        ps.update(lots=round(lots, 2), units=int(round(lots * instrument(s["symbol"]).contract_size)),
                  risk_amount=round(budget, 2), actual_risk_amount=round(lots * pips * pv, 2),
                  account_currency=risk.get("account_currency"), note=note)
        s["position_size"] = ps
    tradable = [s for s in signals if (s.get("position_size") or {}).get("lots")]
    for s in signals:
        if s not in tradable:
            s["portfolio"] = {"allocated": False, "reason": (s.get("position_size") or {}).get("note") or "No position size"}
    apply_portfolio_limits(tradable, risk)
    total_cap = risk.get("max_total_risk_pct", pct * risk["max_open_positions"])
    # With a fixed money risk the user chose the per-trade loss directly; the trade-count limit
    # then bounds the total (fixed x max trades), and a warning is shown if that is large vs the amount.
    if not fixed:
        used = 0.0
        for s in tradable:
            if not s["portfolio"].get("allocated"):
                continue
            r = 100 * s["position_size"]["actual_risk_amount"] / balance
            if used + r > total_cap + 1e-9:
                s["portfolio"] = {"allocated": False, "reason": f"Total open risk would exceed {total_cap}% of balance"}
                continue
            used += r
    for s in signals:
        s["risk_status"] = "ALLOWED" if (s.get("portfolio") or {}).get("allocated") else "BLOCKED"
        if s.get("status") != "ACTIVE" and s["risk_status"] == "ALLOWED":
            s["risk_status"] = "ALLOWED_WHEN_ACTIVE"
    allowed = [s for s in signals if (s.get("portfolio") or {}).get("allocated")]
    at_risk = sum(s["position_size"]["actual_risk_amount"] for s in allowed)
    daily_left = max(0.0, risk["max_daily_loss_pct"] - risk.get("daily_loss_used_pct", 0.0))
    warning = None
    if fixed and balance and fixed / balance > 0.02:
        worst = fixed * risk["max_open_positions"]
        warning = (f"{fixed:.2f} per trade is {100 * fixed / balance:.0f}% of the {balance:.2f} amount; "
                   f"{risk['max_open_positions']} losing trades would lose {worst:.2f} ({100 * worst / balance:.0f}%). "
                   "Most traders keep this at 1–2%.")
    return {
        "balance": balance,
        "currency": risk.get("account_currency"),
        "risk_per_trade_pct": round(100 * budget / balance, 2) if balance else None,
        "risk_per_trade_amount": round(budget, 2),
        "risk_mode": "FIXED_AMOUNT" if fixed else "PERCENT",
        "warning": warning,
        "trades_allowed_now": len(allowed),
        "max_open_positions": risk["max_open_positions"],
        "worst_case_all_trades": round(budget * risk["max_open_positions"], 2),
        "total_risk_amount": round(at_risk, 2),
        "total_risk_pct": round(100 * at_risk / balance, 2) if balance else None,
        "max_total_risk_pct": None if fixed else total_cap,
        "daily_loss_left_pct": round(daily_left, 2),
        "daily_loss_left_amount": round(balance * daily_left / 100, 2) if balance else None,
        "balance_if_all_stopped": round(balance - at_risk, 2) if balance else None,
        "source": risk.get("source"),
        "updated_at": risk.get("updated_at"),
    }


def _limits_without_amount(signals: list[dict], risk: dict) -> dict:
    """No amount entered: no lot sizes, but trade-count / correlation / daily-loss limits still apply (in %)."""
    for s in signals:
        ps = dict(s.get("position_size") or {})
        ps.update(lots=None, units=None, risk_amount=None, actual_risk_amount=None,
                  note="Enter an amount in the dashboard to see a recommended lot size")
        s["position_size"] = ps
    apply_portfolio_limits(signals, risk)
    pct = risk["risk_per_trade_pct"]
    total_cap = risk.get("max_total_risk_pct", pct * risk["max_open_positions"])
    used = 0.0
    for s in signals:
        if (s.get("portfolio") or {}).get("allocated"):
            if used + pct > total_cap + 1e-9:
                s["portfolio"] = {"allocated": False, "reason": f"Total open risk would exceed {total_cap}%"}
            else:
                used += pct
        allowed = (s.get("portfolio") or {}).get("allocated")
        s["risk_status"] = ("ALLOWED" if s.get("status") == "ACTIVE" else "ALLOWED_WHEN_ACTIVE") if allowed else "BLOCKED"
    allowed = [s for s in signals if (s.get("portfolio") or {}).get("allocated")]
    return {
        "balance": None, "currency": risk.get("account_currency"), "risk_per_trade_pct": pct,
        "risk_per_trade_amount": None, "trades_allowed_now": len(allowed),
        "max_open_positions": risk["max_open_positions"], "total_risk_amount": None,
        "total_risk_pct": round(used, 2), "max_total_risk_pct": total_cap,
        "daily_loss_left_pct": round(max(0.0, risk["max_daily_loss_pct"] - risk.get("daily_loss_used_pct", 0.0)), 2),
        "daily_loss_left_amount": None, "balance_if_all_stopped": None,
        "source": risk.get("source"), "updated_at": risk.get("updated_at"),
    }
