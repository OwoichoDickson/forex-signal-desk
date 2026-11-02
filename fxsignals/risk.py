"""Position sizing and portfolio-level risk limits (analysis only — nothing is executed)."""
from __future__ import annotations

import math
from typing import Optional

from .instruments import RateBook, instrument, pip_value_per_lot


def position_size(pair: str, entry: float, stop: float, balance: float, risk_pct: float, account_ccy: str,
                  book: RateBook, lot_step: float = 0.01, min_lot: float = 0.01) -> dict:
    inst = instrument(pair)
    stop_pips = inst.to_pips(entry - stop)
    pv = pip_value_per_lot(inst, account_ccy, book)
    risk_amount = balance * risk_pct / 100.0
    if pv is None or stop_pips <= 0:
        return {"lots": None, "risk_amount": round(risk_amount, 2), "pip_value_per_lot": None,
                "stop_pips": round(stop_pips, 1), "note": f"No {inst.quote}->{account_ccy} conversion rate available"}
    raw = risk_amount / (stop_pips * pv)
    lots = math.floor(raw / lot_step + 1e-9) * lot_step
    note = None
    if lots < min_lot:
        lots, note = 0.0, f"Stop too wide for {risk_pct}% risk at minimum lot {min_lot}"
    actual = lots * stop_pips * pv
    return {
        "lots": round(lots, 2),
        "units": int(round(lots * inst.contract_size)),
        "risk_amount": round(risk_amount, 2),
        "actual_risk_amount": round(actual, 2),
        "pip_value_per_lot": round(pv, 4),
        "stop_pips": round(stop_pips, 1),
        "account_currency": account_ccy,
        "note": note,
    }


def apply_portfolio_limits(ranked: list[dict], risk_cfg: dict) -> None:
    """Annotate ranked signals with whether they fit the portfolio limits (in rank order).

    Limits: max simultaneous positions, max daily loss already used, and the maximum
    risk stacked on any single currency in one direction (correlation guard).
    """
    max_pos = risk_cfg["max_open_positions"]
    per_trade = risk_cfg["risk_per_trade_pct"]
    # The currency cap limits *stacking*; a single trade must always fit, whatever risk % was chosen.
    cap = max(risk_cfg["max_currency_exposure_pct"], risk_cfg["risk_per_trade_pct"])
    daily_left = risk_cfg["max_daily_loss_pct"] - risk_cfg.get("daily_loss_used_pct", 0.0)
    exposure: dict[str, float] = {}
    taken = 0
    for s in ranked:
        base, quote = s["symbol"][:3], s["symbol"][3:6]
        sign = 1 if s["direction"] == "BUY" else -1
        legs = {base: sign * per_trade, quote: -sign * per_trade}
        if daily_left < per_trade:
            if risk_cfg.get("daily_loss_used_pct", 0.0) <= 0:
                why = (f"Risk per trade ({per_trade}%) is larger than the daily loss limit "
                       f"({risk_cfg['max_daily_loss_pct']}%) — lower the risk or raise the limit")
            else:
                why = f"Daily loss limit: only {daily_left:.1f}% left today, a trade risks {per_trade}%"
            s["portfolio"] = {"allocated": False, "reason": why}
            continue
        if taken >= max_pos:
            s["portfolio"] = {"allocated": False, "reason": f"Max {max_pos} simultaneous positions"}
            continue
        breach = [c for c, r in legs.items() if abs(exposure.get(c, 0.0) + r) > cap + 1e-9]
        if breach:
            s["portfolio"] = {"allocated": False,
                              "reason": f"Correlated exposure on {', '.join(breach)} would exceed {cap}% of balance"}
            continue
        for c, r in legs.items():
            exposure[c] = exposure.get(c, 0.0) + r
        taken += 1
        s["portfolio"] = {"allocated": True, "reason": None}


def currency_exposure(signals: list[dict], per_trade: float) -> dict[str, float]:
    exp: dict[str, float] = {}
    for s in signals:
        if not (s.get("portfolio") or {}).get("allocated"):
            continue
        sign = 1 if s["direction"] == "BUY" else -1
        for c, r in ((s["symbol"][:3], sign * per_trade), (s["symbol"][3:6], -sign * per_trade)):
            exp[c] = round(exp.get(c, 0.0) + r, 2)
    return exp


def rr_ratio(entry: float, stop: float, target: float) -> Optional[float]:
    risk = abs(entry - stop)
    return None if risk == 0 else abs(target - entry) / risk
