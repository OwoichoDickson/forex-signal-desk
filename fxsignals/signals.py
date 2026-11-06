"""Multi-timeframe analysis, BUY/SELL/WAIT decision, entry/stop/target levels and confidence score."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import indicators as ind
from . import structure as st
from .fundamentals import PairFundamentals, catalyst_text
from .instruments import Instrument, instrument
from .models import Candles

TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
TRIGGER_POINTS = {"BREAKOUT_RETEST": 10, "BOS": 8, "CHOCH": 8, "LIQUIDITY_SWEEP": 7, "REJECTION": 6}


@dataclass
class Quote:
    last: Optional[float]
    bid: Optional[float] = None
    ask: Optional[float] = None
    change_pct: Optional[float] = None
    update_mode: Optional[str] = None
    ratings: dict = field(default_factory=dict)


@dataclass
class TFAnalysis:
    tf: str
    bars: int
    close: float
    ema9: Optional[float]
    ema20: Optional[float]
    ema50: Optional[float]
    ema200: Optional[float]
    rsi: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]
    macd_hist_prev: Optional[float]
    atr: Optional[float]
    atr_pct_rank: Optional[float]
    adx: Optional[float]
    plus_di: Optional[float]
    minus_di: Optional[float]
    bb_upper: Optional[float]
    bb_middle: Optional[float]
    bb_lower: Optional[float]
    vwap: Optional[float]
    structure: st.StructureReport
    bias: str
    votes: int
    last_t: int = 0
    ret_1m: Optional[float] = None  # D1 only: % change over ~1 month (21 trading days)
    ret_3m: Optional[float] = None  # D1 only: % change over ~3 months (63 trading days)

    def summary(self, inst: Instrument) -> dict:
        r = inst.round_price
        return {
            "bars": self.bars, "close": r(self.close), "bias": self.bias,
            "ema_9": r(self.ema9), "ema_20": r(self.ema20), "ema_50": r(self.ema50), "ema_200": r(self.ema200),
            "rsi": _r(self.rsi, 2), "macd": _r(self.macd, inst.price_decimals + 1),
            "macd_signal": _r(self.macd_signal, inst.price_decimals + 1), "macd_hist": _r(self.macd_hist, inst.price_decimals + 1),
            "atr": _r(self.atr, inst.price_decimals + 1), "adx": _r(self.adx, 2),
            "bb_upper": r(self.bb_upper), "bb_middle": r(self.bb_middle), "bb_lower": r(self.bb_lower),
            "vwap_tick_volume": r(self.vwap),
            "structure_trend": self.structure.trend, "swing_labels": self.structure.swing_labels,
            "consolidating": self.structure.consolidating,
            "return_1m_pct": _r(self.ret_1m, 2), "return_3m_pct": _r(self.ret_3m, 2),
            "recent_events": [
                {"kind": e.kind, "direction": e.direction, "price": r(e.price), "bars_ago": e.bars_ago, "detail": e.detail}
                for e in sorted(self.structure.events, key=lambda e: e.bars_ago)[:4]
            ],
            "last_bos_choch": next(({"kind": e.kind, "direction": e.direction, "price": r(e.price), "bars_ago": e.bars_ago}
                                    for e in sorted(self.structure.events, key=lambda e: e.bars_ago)
                                    if e.kind in ("BOS", "CHOCH")), None),
            "order_blocks": [ob_dict(o, inst) for o in self.structure.order_blocks],
        }


def ob_dict(o: st.OrderBlock, inst: Instrument) -> dict:
    return {"kind": o.kind, "low": inst.round_price(o.low), "high": inst.round_price(o.high), "formed_by": o.formed_by,
            "fresh": o.fresh, "price_inside": o.price_inside, "displacement_atr": o.displacement_atr,
            "time": o.t, "break_time": o.break_t, "mitigated_time": o.mitigated_t}


def _r(v: Optional[float], d: int) -> Optional[float]:
    return None if v is None else round(v, d)


def analyse_timeframe(c: Candles, cfg: dict) -> TFAnalysis:
    a = cfg["analysis"]
    closes = c.c
    e9, e20, e50, e200 = (ind.ema(closes, n) for n in (9, 20, 50, 200))
    m = ind.macd(closes, 12, 26, 9)
    atr_s = ind.atr(c.h, c.l, closes, 14)
    adx_s = ind.adx(c.h, c.l, closes, 14)
    bb = ind.bollinger(closes, 20, 2.0)
    vwap = ind.session_vwap(c.t, c.h, c.l, closes, c.v) if c.tf in ("M5", "M15", "H1") else [None]
    rep = st.analyze(c, atr_s, ind.last(adx_s.adx), width=a["swing_width"].get(c.tf, 3), ob_cfg=a.get("order_block"))
    t = TFAnalysis(
        tf=c.tf, bars=len(c), close=closes[-1], last_t=c.t[-1],
        ema9=ind.last(e9), ema20=ind.last(e20), ema50=ind.last(e50), ema200=ind.last(e200),
        rsi=ind.last(ind.rsi(closes, 14)),
        macd=ind.last(m.macd), macd_signal=ind.last(m.signal), macd_hist=ind.last(m.hist), macd_hist_prev=ind.last(m.hist, 1),
        atr=ind.last(atr_s), atr_pct_rank=ind.percentile_rank(atr_s),
        adx=ind.last(adx_s.adx), plus_di=ind.last(adx_s.plus_di), minus_di=ind.last(adx_s.minus_di),
        bb_upper=ind.last(bb.upper), bb_middle=ind.last(bb.middle), bb_lower=ind.last(bb.lower),
        vwap=ind.last(vwap), structure=rep, bias="NEUTRAL", votes=0,
    )
    if c.tf == "D1":
        t.ret_1m = (closes[-1] / closes[-22] - 1) * 100 if len(closes) > 21 else None
        t.ret_3m = (closes[-1] / closes[-64] - 1) * 100 if len(closes) > 63 else None
    t.bias, t.votes = timeframe_bias(t)
    return t


def timeframe_bias(t: TFAnalysis) -> tuple[str, int]:
    votes = 0
    if t.ema20 is not None and t.ema50 is not None:
        votes += 1 if t.ema20 > t.ema50 else -1
        votes += 1 if t.close > t.ema50 else -1
    if t.ema50 is not None and t.ema200 is not None:
        votes += 1 if t.ema50 > t.ema200 else -1
    if t.macd_hist is not None:
        votes += 1 if t.macd_hist > 0 else -1
    votes += {"BULLISH": 2, "BEARISH": -2}.get(t.structure.trend, 0)
    if votes >= 3 and t.structure.trend != "BEARISH":
        return "BULLISH", votes
    if votes <= -3 and t.structure.trend != "BULLISH":
        return "BEARISH", votes
    return "NEUTRAL", votes


@dataclass
class Plan:
    signal_type: str
    entry: float
    stop: float
    tps: list[float]
    rr: list[float]
    trigger: Optional[st.Event]
    structure_label: str
    confluence: int
    ob: Optional[st.OrderBlock] = None
    ob_tf: Optional[str] = None
    pullback: bool = False  # True: momentum rules taken from H4 instead of H1


class Wait(Exception):
    def __init__(self, reason: str, code: str):
        super().__init__(reason)
        self.reason, self.code = reason, code


def analyse_pair(pair: str, candles: dict[str, Optional[Candles]], quote: Optional[Quote], fund: PairFundamentals,
                 cfg: dict, now: datetime, market_open: bool) -> dict[str, Any]:
    """Return a schema-shaped dict: a BUY/SELL signal or a WAIT entry with the reasons."""
    inst = instrument(pair)
    a = cfg["analysis"]
    base = {"symbol": pair, "economic_risk": fund.economic_risk, "headlines": fund.headlines,
            "catalyst": catalyst_text(fund, pair)}
    tfa: dict[str, TFAnalysis] = {}
    missing: dict[str, int] = {}
    for tf in TIMEFRAMES:
        c = candles.get(tf)
        if c is None or len(c) < a["min_bars"]:
            missing[tf] = 0 if c is None else len(c)
            continue
        tfa[tf] = analyse_timeframe(c, cfg)
    if "H1" in missing or "H4" in missing:
        detail = ", ".join(f"{tf} ({n} bars)" for tf, n in missing.items())
        return _wait(base, "INSUFFICIENT_DATA", f"Insufficient candle data: {detail}", tfa, inst, None, None)
    res = decide(pair, tfa, quote, fund, cfg, market_open)
    res["scalp"] = decide_scalp(pair, tfa, quote, fund, cfg, market_open)
    res["qmr"] = decide_qmr(pair, candles.get("H1"), tfa, quote, fund, cfg, market_open)
    return res


# QMR score: 40 + 10 per point. The points are the conditions that did better on the first half of the lab
# history (Jan–Jun 2026). On the second half, which they were not chosen on, the top half of scores lost
# −0.01R a trade against −0.11R for the rest, and 0–1 points lost in both halves: weak evidence, not proof.
QMR_POINTS = (
    ("H1 EMA20 vs EMA50 points the trade's way", lambda f: f["h1_with"]),
    ("H4 ADX 35 or more (strong H4 move)", lambda f: (f["h4_adx"] or 0) >= 35),
    ("stop under 2 ATR", lambda f: f["risk_atr"] < 2.0),
    ("stop under 1 ATR", lambda f: f["risk_atr"] < 1.0),
    ("13:00 UTC or later (New York session)", lambda f: f["hour"] >= 13),
    ("H1 drifting (efficiency 0.1–0.2)", lambda f: f["er"] is not None and 0.1 <= f["er"] < 0.2),
)


def qmr_score(buy: bool, ema20, ema50, h4_adx, risk_atr: float, hour: int, er) -> tuple[int, list[str]]:
    """(confidence 40–100, the points it earned) for a Quasimodo, the same in the lab and live."""
    f = {"h1_with": ema20 is not None and ema50 is not None and (ema20 > ema50) == buy,
         "h4_adx": h4_adx, "risk_atr": risk_atr, "hour": hour, "er": er}
    got = [name for name, test in QMR_POINTS if test(f)]
    return 40 + 10 * len(got), got


def decide_qmr(pair: str, h1c: Optional[Candles], tfa: dict[str, TFAnalysis], quote: Optional[Quote],
               fund: PairFundamentals, cfg: dict, market_open: bool) -> Optional[dict[str, Any]]:
    """🧪 QMR (Quasimodo reversal) on H1, sent as its own style: a limit order at the QM level (left shoulder)
    after structure broke, stop beyond the head, targets in R. It lost money in the strategy lab, so every
    message says it is being tested. Returns None when there is no fresh pattern."""
    q = cfg.get("qmr") or {}
    h1 = tfa.get("H1")
    if not q.get("enabled") or h1c is None or h1 is None or not h1.atr or fund.blackout_events:
        return None
    a, inst, atr = cfg["analysis"], instrument(pair), h1.atr
    width = a["swing_width"].get("H1", 3)
    pats = st.find_quasimodo(h1c, getattr(h1.structure, "_swings", None) or st.find_swings(h1c, width), width,
                             max_break_ago=q.get("max_break_bars_ago", 3))
    if not pats:
        return None
    p = pats[0]
    bear = p.direction == "BEARISH"
    sgn = -1 if bear else 1
    price = quote.last if quote and quote.last else h1.close
    stop = p.head - sgn * a["sl_buffer_atr"] * atr
    risk = abs(p.qml - stop)
    if not a["min_sl_atr"] * atr <= risk <= a["max_sl_atr"] * atr or sgn * (price - p.qml) < 0.2 * atr:
        return None  # stop too tight/wide, or price already at the QM level
    tr = q.get("targets_r", [1.0, 2.0, 3.0])
    tps = [p.qml + sgn * r * risk for r in tr]
    h4 = tfa.get("H4")
    hour = datetime.fromtimestamp(h1c.t[-1] + 3600, timezone.utc).hour  # when the last H1 bar closed
    score, points = qmr_score(not bear, h1.ema20, h1.ema50, h4.adx if h4 else None, risk / atr, hour,
                              ind.efficiency(h1c.c, len(h1c) - 1))
    status = "PENDING" if market_open else "MARKET_CLOSED"
    side = "BULLISH" if not bear else "BEARISH"
    alignment = ",".join(tf for tf in ("M15", "H1", "H4", "D1") if tf in tfa and tfa[tf].bias == side)
    return {
        "symbol": pair, "economic_risk": fund.economic_risk, "headlines": fund.headlines, "catalyst": catalyst_text(fund, pair),
        "style": "QMR", "entry_minutes": q.get("entry_minutes", 720),
        "direction": "SELL" if bear else "BUY", "signal_type": "SELL_LIMIT" if bear else "BUY_LIMIT",
        "entry_price": inst.round_price(p.qml), "stop_loss": inst.round_price(stop),
        "take_profit_1": inst.round_price(tps[0]), "take_profit_2": inst.round_price(tps[1]), "take_profit_3": inst.round_price(tps[2]),
        "risk_reward": f"1:{tr[1]:.1f}", "confidence_score": score, "trend": side,
        "momentum": "MODERATE" if (h1.adx or 0) >= 20 else "WEAK",
        "rsi": _r(h1.rsi, 1), "ema_20": inst.round_price(h1.ema20), "ema_50": inst.round_price(h1.ema50),
        "atr": _r(atr, inst.price_decimals + 1), "timeframe_alignment": alignment,
        "market_structure": f"{side}_QUASIMODO",
        "reason": (f"QMR {'SELL' if bear else 'BUY'}: H1 made a {'higher high' if bear else 'lower low'} (head "
                   f"{inst.round_price(p.head)}) then broke {'below' if bear else 'above'} {inst.round_price(p.broken)}; "
                   f"limit at the left shoulder (QM level), stop beyond the head. Being tested: lost money in the lab."),
        "status": status, "actionable": False,
        "details": {
            "d1_trend": _d1_trend(tfa.get("D1"), not bear), "entry_basis": "level",
            "risk_reward_by_target": [f"1:{r:.2f}" for r in tr],
            "stop_distance_pips": round(inst.to_pips(risk), 1),
            "atr_h1_pips": round(inst.to_pips(atr), 1),
            "qmr_score": {"points": points, "of": len(QMR_POINTS),
                          "evidence": "weak: on unseen history the top half of scores lost −0.01R a trade vs −0.11R for the rest"},
            "quasimodo": {"qml": inst.round_price(p.qml), "head": inst.round_price(p.head),
                          "broken": inst.round_price(p.broken), "bars_since_break": p.bars_since_break},
            "surprises": fund.surprises, "upcoming_high_impact": [e.to_dict() for e in fund.upcoming_high],
            "technicals": {tf: tfa[tf].summary(inst) for tf in ("H1", "H4") if tf in tfa},
            "warnings": ["🧪 QMR is being tested: it lost money after costs in the strategy lab (Jan–Nov 2026)."],
        },
    }


def decide_scalp(pair: str, tfa: dict[str, TFAnalysis], quote: Optional[Quote], fund: PairFundamentals,
                 cfg: dict, market_open: bool) -> Optional[dict[str, Any]]:
    """Short trade for scalpers, separate from the main signal: H1 + M15 agree on direction, a fresh M5
    break/retest/sweep confirms it, a tight stop beyond M15 structure, targets 1R/1.5R/2R, and the trade is
    closed after ``scalp.max_hold_minutes`` whatever happens. Returns None when there is no scalp."""
    s = cfg.get("scalp") or {}
    if not s.get("enabled"):
        return None
    inst = instrument(pair)
    m5, m15, h1, h4 = tfa.get("M5"), tfa.get("M15"), tfa.get("H1"), tfa.get("H4")
    if None in (m5, m15, h1) or None in (m15.atr, m15.ema20, m15.rsi, m15.macd_hist, m5.atr):
        return None
    if fund.blackout_events or m15.bias != h1.bias or m15.bias not in ("BULLISH", "BEARISH"):
        return None
    bull = m15.bias == "BULLISH"
    sgn, side = (1 if bull else -1), m15.bias
    # M15 momentum must point the same way (not overbought/oversold)
    if not ((50 < m15.rsi < 70) if bull else (30 < m15.rsi < 50)) or sgn * m15.macd_hist <= 0:
        return None
    trig = None
    for e in m5.structure.events:
        if e.direction == side and e.bars_ago <= s["trigger_max_bars_ago"] and e.kind in TRIGGER_POINTS:
            if e.kind == "REJECTION" and not _near_level(e.price, [m15, h1], m15.atr, bull):
                continue
            if trig is None or TRIGGER_POINTS[e.kind] > TRIGGER_POINTS[trig.kind]:
                trig = e
    if trig is None:
        return None
    price = quote.last if quote and quote.last else m5.close
    entry = (quote.ask if bull else quote.bid) if quote and (quote.ask if bull else quote.bid) else price
    if sgn * (entry - m15.ema20) / m15.atr > s["max_extension_atr"]:
        return None  # chasing: price already far from the M15 mean
    sa = {**cfg["analysis"], "sl_buffer_atr": s["sl_buffer_atr"], "min_sl_atr": s["min_sl_atr"], "max_sl_atr": s["max_sl_atr"]}
    try:
        stop = _structural_stop(entry, m15, m5, bull, m15.atr, sa)
    except Wait:
        return None
    risk = abs(entry - stop)
    spread = (quote.ask - quote.bid) if quote and quote.ask and quote.bid else None
    if spread is not None and risk < s["min_stop_spreads"] * spread:
        return None  # spread would eat the scalp
    tps = [entry + sgn * r * risk for r in s["targets_r"]]
    # a nearby M15/H1 level before TP1 would block the move
    obstacles = [lv.price for tf in (m15, h1) for lv in tf.structure.levels if 0 < sgn * (lv.price - entry) < sgn * (tps[0] - entry)]
    if obstacles:
        return None
    score = 55 + TRIGGER_POINTS[trig.kind] + (10 if h4 is not None and h4.bias == side else 0) \
        + (8 if (m15.adx or 0) >= cfg["analysis"]["adx_trend_min"] else 0) + (5 if m5.bias == side else 0) \
        + (5 if spread is not None and spread <= 0.05 * risk else 0)
    score = min(score, 90)
    if score < s["min_confidence"]:
        return None
    alignment = ",".join(tf for tf in ("M5", "M15", "H1", "H4") if tf in tfa and tfa[tf].bias == side)
    status = "ACTIVE" if market_open else "MARKET_CLOSED"
    d1t = _d1_trend(tfa.get("D1"), bull)
    hold = s["max_hold_minutes"]
    return {
        "symbol": pair, "economic_risk": fund.economic_risk, "headlines": fund.headlines, "catalyst": catalyst_text(fund, pair),
        "style": "SCALP", "max_hold_minutes": hold, "entry_minutes": s["entry_minutes"],
        "direction": "BUY" if bull else "SELL", "signal_type": "MARKET_ENTRY",
        "entry_price": inst.round_price(entry), "stop_loss": inst.round_price(stop),
        "take_profit_1": inst.round_price(tps[0]), "take_profit_2": inst.round_price(tps[1]), "take_profit_3": inst.round_price(tps[2]),
        "risk_reward": f"1:{s['targets_r'][1]:.1f}", "confidence_score": score, "trend": side,
        "momentum": "STRONG" if (m15.adx or 0) >= 25 else "MODERATE" if (m15.adx or 0) >= 20 else "WEAK",
        "rsi": _r(m15.rsi, 1), "ema_20": inst.round_price(m15.ema20), "ema_50": inst.round_price(m15.ema50),
        "atr": _r(m15.atr, inst.price_decimals + 1), "timeframe_alignment": alignment,
        "market_structure": f"SCALP_{side}_{trig.kind}",
        "reason": (f"SCALP {'BUY' if bull else 'SELL'}: H1 and M15 {side.lower()}, M5 {trig.kind.replace('_', ' ').lower()} "
                   f"{trig.bars_ago * 5} min ago; stop beyond M15 structure; close within {hold} min."),
        "status": status, "actionable": status == "ACTIVE",
        "details": {
            "d1_trend": d1t, "entry_basis": "ask" if bull else "bid",
            "risk_reward_by_target": [f"1:{r:.2f}" for r in s["targets_r"]],
            "stop_distance_pips": round(inst.to_pips(risk), 1),
            "spread_pips": round(inst.to_pips(spread), 1) if spread is not None else None,
            "atr_m15_pips": round(inst.to_pips(m15.atr), 1),
            "trigger": {"kind": trig.kind, "price": inst.round_price(trig.price), "bars_ago": trig.bars_ago,
                        "timeframe": "M5", "detail": trig.detail},
            "surprises": fund.surprises, "upcoming_high_impact": [e.to_dict() for e in fund.upcoming_high],
            "technicals": {tf: tfa[tf].summary(inst) for tf in ("M5", "M15", "H1") if tf in tfa},
            "warnings": [],
        },
    }


def decide(pair: str, tfa: dict[str, TFAnalysis], quote: Optional[Quote], fund: PairFundamentals,
           cfg: dict, market_open: bool) -> dict[str, Any]:
    """Decision layer on already-computed timeframe analyses (separated so the rules are unit-testable)."""
    inst = instrument(pair)
    a = cfg["analysis"]
    base = {"symbol": pair, "economic_risk": fund.economic_risk, "headlines": fund.headlines,
            "catalyst": catalyst_text(fund, pair)}
    h1, h4 = tfa["H1"], tfa["H4"]
    d1, m15 = tfa.get("D1"), tfa.get("M15")
    price = quote.last if quote and quote.last else h1.close
    if quote and quote.last and abs(quote.last - h1.close) / h1.close > cfg["data"]["price_sanity_tolerance_pct"] / 100:
        return _wait(base, "DATA_MISMATCH", f"Live price {quote.last} disagrees with H1 candles ({h1.close}); data not trusted", tfa, inst, None, None)

    direction = _direction(h1, h4)
    if direction is None:
        if "BULLISH" in (h1.bias, h4.bias) and "BEARISH" in (h1.bias, h4.bias):
            return _wait(base, "CONFLICT", f"H1 {h1.bias} conflicts with H4 {h4.bias}", tfa, inst, None, None)
        return _wait(base, "NO_TREND", f"No higher-timeframe trend (H1 {h1.bias}, H4 {h4.bias})", tfa, inst, None, None)
    bull = direction == "BUY"
    lean = "BULLISH" if bull else "BEARISH"

    try:
        if None in (h1.ema20, h1.ema50, h1.rsi, h1.macd, h1.atr):
            raise Wait("H1 indicators not computable", "INSUFFICIENT_DATA")
        if fund.blackout_events:
            e = fund.blackout_events[0]
            raise Wait(f"High-impact {e.currency} '{e.title}' at {e.date:%H:%M} UTC — no new trades inside the news window", "NEWS_BLACKOUT")
        d1t = _d1_trend(d1, bull)
        _d1_trend_check(d1t, bull, a.get("d1_trend_filter", "strict"))
        plan = _build_plan(inst, price, quote, h1, h4, d1, m15, bull, a, cfg["signals"])
        # Pullback entries (order block / limit at structure) are judged on H4 momentum: during a healthy
        # pullback the H1 oscillators point the other way by definition. Breakout entries keep strict H1 rules.
        plan.pullback = a.get("momentum_mode", "adaptive") == "adaptive" and (
            plan.ob is not None or plan.signal_type.endswith("LIMIT"))
        if plan.pullback:
            _pullback_momentum_checks(h1, h4, bull, a)
        else:
            _mandatory_checks(h1, bull, a)
        spread_pips = _spread_check(inst, quote, plan, cfg["spreads"], market_open)
    except Wait as w:
        return _wait(base, w.code, w.reason, tfa, inst, lean, None)

    score, breakdown = score_setup(tfa, bull, plan, fund, a)
    alignment = ",".join(tf for tf in ("M15", "H1", "H4", "D1") if tf in tfa and tfa[tf].bias == lean)
    if score < cfg["signals"]["min_confidence"]:
        return _wait(base, "LOW_CONFIDENCE", f"{direction} setup scored {score} (< {cfg['signals']['min_confidence']})",
                     tfa, inst, lean, {"confidence_score": score, "score_breakdown": breakdown,
                                       "proposed": _plan_dict(plan, inst)})

    momentum = "STRONG" if (h1.adx or 0) >= 25 and _hist_rising(h1, bull) else "MODERATE" if (h1.adx or 0) >= 20 else "WEAK"
    if plan.signal_type == "MARKET_ENTRY":
        status = "ACTIVE" if market_open else "MARKET_CLOSED"
    else:
        status = "PENDING" if market_open else "MARKET_CLOSED"
    rr_main = plan.rr[1]
    reason = _reason(direction, plan, alignment, breakdown)
    return {
        **base,
        "direction": direction,
        "signal_type": plan.signal_type,
        "entry_price": inst.round_price(plan.entry),
        "stop_loss": inst.round_price(plan.stop),
        "take_profit_1": inst.round_price(plan.tps[0]),
        "take_profit_2": inst.round_price(plan.tps[1]),
        "take_profit_3": inst.round_price(plan.tps[2]),
        "risk_reward": f"1:{rr_main:.1f}",
        "confidence_score": score,
        "trend": lean,
        "momentum": momentum,
        "rsi": _r(h1.rsi, 1),
        "ema_20": inst.round_price(h1.ema20),
        "ema_50": inst.round_price(h1.ema50),
        "atr": _r(h1.atr, inst.price_decimals + 1),
        "timeframe_alignment": alignment,
        "market_structure": plan.structure_label,
        "reason": reason,
        "status": status,
        "actionable": status == "ACTIVE",
        "details": {
            "d1_trend": d1t,
            "entry_basis": "ask" if bull and quote and quote.ask and plan.signal_type == "MARKET_ENTRY" else
                           "bid" if (not bull) and quote and quote.bid and plan.signal_type == "MARKET_ENTRY" else "level",
            "risk_reward_by_target": [f"1:{x:.2f}" for x in plan.rr],
            "stop_distance_pips": round(inst.to_pips(plan.entry - plan.stop), 1),
            "spread_pips": spread_pips,
            "volatility": _volatility(h1),
            "atr_h1_pips": round(inst.to_pips(h1.atr), 1) if h1.atr else None,
            "score_breakdown": breakdown,
            "trigger": None if plan.trigger is None else {
                "kind": plan.trigger.kind, "price": inst.round_price(plan.trigger.price),
                "bars_ago": plan.trigger.bars_ago, "detail": plan.trigger.detail},
            "order_block": None if plan.ob is None else {**ob_dict(plan.ob, inst), "timeframe": plan.ob_tf},
            "momentum_rules": "PULLBACK (H4 momentum)" if plan.pullback else "BREAKOUT (H1 momentum)",
            "surprises": fund.surprises,
            "upcoming_high_impact": [e.to_dict() for e in fund.upcoming_high],
            "technicals": {tf: t.summary(inst) for tf, t in tfa.items()},
            "key_levels": key_levels(tfa, inst),
            "tv_ratings": (quote.ratings if quote else {}) or None,
            "warnings": _warnings(market_open, d1, lean, quote),
        },
    }


def _d1_trend(d1: Optional[TFAnalysis], bull: bool) -> Optional[dict]:
    """How the trade sits against the slow trend (time-series momentum, the best-documented FX effect):
    the pair's change over ~1 and ~3 months and how many of the two point the trade's way."""
    if d1 is None or d1.ret_1m is None or d1.ret_3m is None:
        return None
    sgn = 1 if bull else -1
    agree = int(sgn * d1.ret_1m > 0) + int(sgn * d1.ret_3m > 0)
    return {"return_1m_pct": round(d1.ret_1m, 2), "return_3m_pct": round(d1.ret_3m, 2), "agree": agree,
            "label": {2: "with the daily trend", 1: "mixed daily trend", 0: "against the daily trend"}[agree]}


def _d1_trend_check(d1t: Optional[dict], bull: bool, mode: str) -> None:
    """strict: BUY only if up over ~1 AND ~3 months (SELL: down over both); loose: at least one;
    shadow/off: never blocks (shadow still records the comparison in details.d1_trend)."""
    if d1t is None or mode not in ("strict", "loose") or d1t["agree"] == 2 or (mode == "loose" and d1t["agree"] == 1):
        return
    raise Wait(f"{'BUY' if bull else 'SELL'} {d1t['label']}: the pair is {d1t['return_1m_pct']:+.2f}% over 1 month and "
               f"{d1t['return_3m_pct']:+.2f}% over 3 months (needs both {'up' if bull else 'down'})", "D1_TREND_AGAINST")


def _direction(h1: TFAnalysis, h4: TFAnalysis) -> Optional[str]:
    biases = {h1.bias, h4.bias}
    if biases == {"BULLISH"} or biases == {"BULLISH", "NEUTRAL"}:
        return "BUY"
    if biases == {"BEARISH"} or biases == {"BEARISH", "NEUTRAL"}:
        return "SELL"
    return None


def _pullback_momentum_checks(h1: TFAnalysis, h4: TFAnalysis, bull: bool, a: dict) -> None:
    """Momentum rules for pullback entries: the H4 trend must still be strong; H1 may be mid-pullback
    but must not be collapsing (RSI beyond the floor means a reversal, not a pullback)."""
    p = a.get("pullback", {})
    rsi_max, floor = p.get("htf_rsi_max", 75), p.get("h1_rsi_floor", 30)
    if None in (h4.ema20, h4.ema50, h4.rsi, h4.macd, h4.macd_signal):
        raise Wait("H4 indicators not computable", "INSUFFICIENT_DATA")
    s = 1 if bull else -1
    if s * (h4.ema20 - h4.ema50) <= 0:
        raise Wait(f"H4 EMA20 is not {'above' if bull else 'below'} EMA50 — higher-timeframe trend too weak for a pullback entry", "EMA_FILTER")
    r4 = h4.rsi if bull else 100 - h4.rsi
    if r4 <= 50:
        raise Wait(f"H4 RSI {h4.rsi:.1f} lacks {'bullish' if bull else 'bearish'} momentum", "RSI_MOMENTUM")
    if r4 >= rsi_max:
        raise Wait(f"H4 RSI {h4.rsi:.1f} is stretched", "RSI_EXTREME")
    if s * (h4.macd - h4.macd_signal) <= 0 and s * (h4.macd_hist or 0) <= 0:
        raise Wait(f"H4 MACD does not support {'bullish' if bull else 'bearish'} momentum", "MACD_FILTER")
    r1 = h1.rsi if bull else 100 - h1.rsi
    if r1 < floor:
        raise Wait(f"H1 RSI {h1.rsi:.1f} shows a reversal, not a pullback", "RSI_MOMENTUM")
    if r1 >= a["rsi_overbought"]:
        raise Wait(f"H1 RSI {h1.rsi:.1f} is {'overbought' if bull else 'oversold'}", "RSI_EXTREME")


def _mandatory_checks(h1: TFAnalysis, bull: bool, a: dict) -> None:
    if h1.ema20 is None or h1.ema50 is None or h1.rsi is None or h1.macd is None or h1.atr is None:
        raise Wait("H1 indicators not computable", "INSUFFICIENT_DATA")
    if bull:
        if h1.ema20 <= h1.ema50:
            raise Wait("H1 EMA20 is not above EMA50", "EMA_FILTER")
        if h1.rsi >= a["rsi_overbought"]:
            raise Wait(f"H1 RSI {h1.rsi:.1f} is overbought", "RSI_EXTREME")
        if h1.rsi <= 50:
            raise Wait(f"H1 RSI {h1.rsi:.1f} lacks bullish momentum", "RSI_MOMENTUM")
        if h1.macd <= h1.macd_signal and (h1.macd_hist or 0) <= 0:
            raise Wait("H1 MACD does not support bullish momentum", "MACD_FILTER")
    else:
        if h1.ema20 >= h1.ema50:
            raise Wait("H1 EMA20 is not below EMA50", "EMA_FILTER")
        if h1.rsi <= a["rsi_oversold"]:
            raise Wait(f"H1 RSI {h1.rsi:.1f} is oversold", "RSI_EXTREME")
        if h1.rsi >= 50:
            raise Wait(f"H1 RSI {h1.rsi:.1f} lacks bearish momentum", "RSI_MOMENTUM")
        if h1.macd >= h1.macd_signal and (h1.macd_hist or 0) >= 0:
            raise Wait("H1 MACD does not support bearish momentum", "MACD_FILTER")


def _near_level(price: float, tfs: list[TFAnalysis], atr: float, bull: bool, tol_atr: float = 0.5) -> bool:
    for t in tfs:
        for lv in t.structure.levels:
            if abs(lv.price - price) <= tol_atr * atr:
                return True
        for z in t.structure.zones:
            if (z.kind == "DEMAND") == bull and z.low - tol_atr * atr <= price <= z.high + tol_atr * atr:
                return True
    return False


def _find_trigger(m15: Optional[TFAnalysis], h1: TFAnalysis, bull: bool, atr: float, a: dict) -> Optional[st.Event]:
    want = "BULLISH" if bull else "BEARISH"
    best: Optional[st.Event] = None
    for t, max_ago in ((m15, a["trigger_max_bars_ago"]["M15"]), (h1, a["trigger_max_bars_ago"]["H1"])):
        if t is None:
            continue
        for e in t.structure.events:
            if e.direction != want or e.bars_ago > max_ago:
                continue
            if e.kind == "REJECTION" and not _near_level(e.price, [h1, t], atr, bull):
                continue  # a pin bar in mid-air is not a trigger
            if best is None or TRIGGER_POINTS[e.kind] > TRIGGER_POINTS[best.kind]:
                best = e
    return best


def _build_plan(inst: Instrument, price: float, quote: Optional[Quote], h1: TFAnalysis, h4: TFAnalysis,
                d1: Optional[TFAnalysis], m15: Optional[TFAnalysis], bull: bool, a: dict, sig: dict) -> Plan:
    atr = h1.atr
    sgn = 1 if bull else -1
    trigger = _find_trigger(m15, h1, bull, atr, a)
    extension = sgn * (price - h1.ema20) / atr
    signal_type: Optional[str] = None
    entry = None
    confluence = 0
    label = ""
    side = "BULLISH" if bull else "BEARISH"
    ob, ob_tf = None, None
    # enter-now only close to H1 EMA20 (no chasing) and in a real trend (ADX); otherwise fall back to a pending order
    if trigger and extension <= a["max_extension_atr"] and (h1.adx or 0) >= a.get("market_entry_min_adx", 0):
        signal_type = "MARKET_ENTRY"
        entry = (quote.ask if bull else quote.bid) if quote and (quote.ask if bull else quote.bid) else price
        ob, ob_tf = _tapped_order_block([h1, h4], bull, a)
        if ob is not None:
            # SMC sequence: HTF order block tapped, then a lower-timeframe BOS/CHOCH in the trend direction
            confluence, label = 5, f"{side}_{trigger.kind}_AT_ORDER_BLOCK"
        else:
            confluence = 5 if _near_level(entry, [h1, h4], atr, bull) or trigger.kind == "BREAKOUT_RETEST" else 0
            label = f"{side}_{trigger.kind}"
    elif h1.structure.consolidating and h1.structure.range_high and h1.structure.range_low:
        edge = h1.structure.range_high if bull else h1.structure.range_low
        if 0 < sgn * (edge - price) <= 1.0 * atr:
            signal_type = "BUY_STOP" if bull else "SELL_STOP"
            entry = edge + sgn * a["stop_entry_buffer_atr"] * atr
            confluence = 3
            label = f"{'BULLISH' if bull else 'BEARISH'}_RANGE_BREAKOUT_PENDING"
    lo_d, hi_d = a["limit_min_distance_atr"] * atr, a["limit_max_distance_atr"] * atr
    if signal_type is None:
        # Preferred pending entry: the proximal edge of a fresh order block in the trend direction.
        obs = [(o, tf.tf) for tf in (h1, h4) for o in tf.structure.order_blocks
               if o.kind == side and o.fresh and lo_d <= sgn * (price - (o.high if bull else o.low)) <= hi_d]
        if obs:
            ob, ob_tf = max(obs, key=lambda x: sgn * (x[0].high if bull else x[0].low))
            signal_type = "BUY_LIMIT" if bull else "SELL_LIMIT"
            entry, confluence = (ob.high if bull else ob.low), 5
            label = f"{side}_PULLBACK_TO_ORDER_BLOCK"
            trigger = None
    if signal_type is None:
        cands: list[tuple[float, str]] = []
        for t in (h1, h4):
            for lv in t.structure.levels:
                cands.append((lv.price, "level"))
            for z in t.structure.zones:
                if (z.kind == "DEMAND") == bull:
                    cands.append((z.high if bull else z.low, "zone"))
        valid = [(p, k) for p, k in cands if lo_d <= sgn * (price - p) <= hi_d]
        if valid:
            p, k = max(valid, key=lambda x: sgn * x[0])  # closest to price on the pullback side
            signal_type = "BUY_LIMIT" if bull else "SELL_LIMIT"
            entry, confluence = p, 5
            label = f"{'BULLISH' if bull else 'BEARISH'}_PULLBACK_TO_{'DEMAND' if k == 'zone' and bull else 'SUPPLY' if k == 'zone' else 'LEVEL'}"
            trigger = None
    if signal_type is None:
        why = ("trend too weak for an enter-now (H1 ADX below minimum)" if trigger and extension <= a["max_extension_atr"]
               else "price extended from H1 EMA20" if trigger else "no confirmed entry trigger")
        raise Wait(f"Trend valid but {why}, and no structural pullback/breakout level for a pending order", "NO_TRIGGER")

    stop = _structural_stop(entry, h1, m15, bull, atr, a, ob)
    risk = abs(entry - stop)
    targets = _targets(entry, risk, [t for t in (h1, h4, d1) if t], bull, atr, sig, a)
    rr = [abs(tp - entry) / risk for tp in targets]
    if rr[0] < sig["min_risk_reward"] - 1e-9:
        raise Wait(f"Risk/reward 1:{rr[0]:.2f} below minimum 1:{sig['min_risk_reward']}", "POOR_RR")
    return Plan(signal_type, entry, stop, targets, rr, trigger, label, confluence, ob, ob_tf)


def _tapped_order_block(tfs: list[TFAnalysis], bull: bool, a: dict) -> tuple[Optional[st.OrderBlock], Optional[str]]:
    """A same-direction H1/H4 order block that price is inside now or first returned to recently."""
    window = a.get("order_block", {}).get("tap_window_hours", 6) * 3600
    side = "BULLISH" if bull else "BEARISH"
    best = None
    for t in tfs:
        for o in t.structure.order_blocks:
            if o.kind != side:
                continue
            recent = o.mitigated_t is not None and t.last_t - o.mitigated_t <= window
            if o.price_inside or recent:
                if best is None or o.t > best[0].t:
                    best = (o, t.tf)
    return best if best else (None, None)


def _structural_stop(entry: float, h1: TFAnalysis, m15: Optional[TFAnalysis], bull: bool, atr: float, a: dict,
                     ob: Optional[st.OrderBlock] = None) -> float:
    buf = a["sl_buffer_atr"] * atr
    swings = [s for s in getattr(h1.structure, "_swings", []) if s.kind == ("L" if bull else "H")]
    zone_edges = [(z.low if bull else z.high) for z in h1.structure.zones if (z.kind == "DEMAND") == bull]
    side = "BULLISH" if bull else "BEARISH"
    ob_edges = [(o.low if bull else o.high) for o in h1.structure.order_blocks if o.kind == side]
    if ob is not None:
        # the order block's far side is the invalidation: use it when its distance is acceptable
        far = (ob.low - buf) if bull else (ob.high + buf)
        if a["min_sl_atr"] * atr <= abs(entry - far) <= a["max_sl_atr"] * atr and (far < entry if bull else far > entry):
            return far
    cands = [s.price for s in swings] + zone_edges + ob_edges
    if bull:
        cands = sorted((p - buf for p in cands if p < entry), reverse=True)
    else:
        cands = sorted(p + buf for p in cands if p > entry)
    for stop in cands:
        dist = abs(entry - stop)
        if dist < a["min_sl_atr"] * atr:
            continue
        if dist > a["max_sl_atr"] * atr:
            raise Wait(f"Nearest structural stop is {dist / atr:.1f} ATR away (max {a['max_sl_atr']})", "STOP_TOO_WIDE")
        return stop
    raise Wait("No structural invalidation level (swing/zone) found for the stop-loss", "NO_STRUCTURE_STOP")


def _targets(entry: float, risk: float, tfs: list[TFAnalysis], bull: bool, atr: float, sig: dict, a: dict) -> list[float]:
    sgn = 1 if bull else -1
    opposing = "BEARISH" if bull else "BULLISH"
    raw = [lv.price for t in tfs for lv in t.structure.levels]
    # opposing order blocks are where the move is likely to stall: use their near edge as targets
    raw += [(o.low if bull else o.high) for t in tfs for o in t.structure.order_blocks if o.kind == opposing]
    levels = sorted({round(p, 10) for p in raw if sgn * (p - entry) > 0}, key=lambda p: sgn * (p - entry))
    strong = [lv for t in tfs for lv in t.structure.levels
              if lv.touches >= 2 and 0.3 * risk < sgn * (lv.price - entry) < sig["min_risk_reward"] * risk]
    if strong:
        lv = min(strong, key=lambda lv: sgn * (lv.price - entry))
        raise Wait(f"{'Resistance' if bull else 'Support'} at {lv.price:.6g} ({lv.touches} touches) caps reward at "
                   f"1:{sgn * (lv.price - entry) / risk:.2f}", "POOR_RR")
    front = a["tp_front_run_atr"] * atr
    tp1 = entry + sgn * sig["min_risk_reward"] * risk
    lv2 = [p for p in levels if sig["preferred_risk_reward"] * risk <= sgn * (p - entry) <= 5 * risk]
    tp2 = (lv2[0] - sgn * front) if lv2 else entry + sgn * sig["preferred_risk_reward"] * risk
    if sgn * (tp2 - entry) < sig["preferred_risk_reward"] * risk * 0.95:
        tp2 = entry + sgn * sig["preferred_risk_reward"] * risk
    lv3 = [p for p in levels if sgn * (p - tp2) >= 0.5 * risk and sgn * (p - entry) <= 8 * risk]
    tp3 = (lv3[0] - sgn * front) if lv3 else tp2 + sgn * risk
    return [tp1, tp2, tp3]


def _spread_check(inst: Instrument, quote: Optional[Quote], plan: Plan, sp: dict, market_open: bool) -> Optional[float]:
    if not quote or quote.bid is None or quote.ask is None:
        return None
    spread_pips = round(inst.to_pips(quote.ask - quote.bid), 2)
    if not market_open:
        return spread_pips  # weekend quotes are not tradable; spread filter re-applied when the market opens
    max_pips = sp["max_spread_pips"].get(inst.name, sp["max_spread_pips"]["JPY_CROSS" if inst.is_jpy else "DEFAULT"])
    if spread_pips > max_pips:
        raise Wait(f"Spread {spread_pips} pips exceeds limit {max_pips}", "HIGH_SPREAD")
    if (quote.ask - quote.bid) > sp["max_spread_to_stop_ratio"] * abs(plan.entry - plan.stop):
        raise Wait(f"Spread {spread_pips} pips is too large relative to the stop", "HIGH_SPREAD")
    return spread_pips


def _hist_rising(t: TFAnalysis, bull: bool) -> bool:
    if t.macd_hist is None or t.macd_hist_prev is None:
        return False
    return t.macd_hist > t.macd_hist_prev if bull else t.macd_hist < t.macd_hist_prev


def score_setup(tfa: dict[str, TFAnalysis], bull: bool, plan: Plan, fund: PairFundamentals, a: dict) -> tuple[int, dict]:
    lean = "BULLISH" if bull else "BEARISH"
    opp = "BEARISH" if bull else "BULLISH"
    sgn = 1 if bull else -1
    h1, h4 = tfa["H1"], tfa["H4"]
    d1, m15 = tfa.get("D1"), tfa.get("M15")

    def align(t: Optional[TFAnalysis], full: float, neutral: float) -> float:
        if t is None:
            return 0
        return full if t.bias == lean else neutral if t.bias == "NEUTRAL" else 0

    htf = align(h4, 10, 4) + align(h1, 8, 3) + align(d1, 5, 2) + align(m15, 2, 1)

    techn = 0.0
    techn += 5 if sgn * (h1.ema20 - h1.ema50) > 0 else 0
    techn += 3 if h1.ema200 is not None and sgn * (h1.ema50 - h1.ema200) > 0 else 0
    techn += 4 if h4.ema200 is not None and sgn * (h4.close - h4.ema200) > 0 else 0
    techn += 4 if (h1.adx or 0) >= 25 else 2 if (h1.adx or 0) >= a["adx_trend_min"] else 0
    band = h1.bb_upper if bull else h1.bb_lower
    techn += 4 if band is not None and sgn * (band - h1.close) > 0 else 0

    struct = 0.0
    struct += 6 if h1.structure.trend == lean else 2 if h1.structure.trend in ("RANGING", "UNCLEAR") else 0
    struct += 4 if h4.structure.trend == lean else 1 if h4.structure.trend in ("RANGING", "UNCLEAR") else 0
    if plan.ob is not None:
        struct += 10 if plan.trigger else 6  # OB + LTF break is the strongest confirmation; a bare OB limit waits for price
    else:
        struct += TRIGGER_POINTS.get(plan.trigger.kind, 0) if plan.trigger else 4  # pending orders wait for price to come to structure
    struct += plan.confluence

    mom = 0.0
    mtf = h4 if plan.pullback else h1  # pullback entries: momentum is judged on H4 (see _pullback_momentum_checks)
    if mtf.rsi is not None:
        r = mtf.rsi if bull else 100 - mtf.rsi
        mom += 5 if 50 < r <= 65 else 3 if 65 < r < a["rsi_overbought"] + (5 if plan.pullback else 0) else 0
    if mtf.macd_hist is not None and sgn * mtf.macd_hist > 0:
        mom += 5 if _hist_rising(mtf, bull) else 3
    if m15 and m15.rsi is not None and m15.macd_hist is not None:
        ok = (sgn * (m15.rsi - 50) > 0) + (sgn * m15.macd_hist > 0)
        mom += 5 if ok == 2 else 2 if ok == 1 else 0

    fundamental = 0.0
    if fund.available:
        fundamental += 6 if not fund.blackout_events else 0
        fundamental += 4 if not fund.upcoming_high else 1
        b = fund.bias or 0
        fundamental += 5 if sgn * b > 0 else 2 if b == 0 else 0
    else:
        fundamental = 3

    raw = htf + techn + struct + mom + fundamental
    score = int(round(min(100, raw)))
    notes = []
    # Correlated indicators agreeing is not enough: strong scores need real structure confirmation.
    if struct < a["strong_signal_min_structure"] and score > 74:
        score = 74
        notes.append("capped at 74: structure/price-action confirmation too weak for a strong rating")
    if d1 is not None and d1.bias == opp and score > 74:
        score = 74
        notes.append("capped at 74: daily trend opposes the trade")
    return score, {
        "higher_timeframe_alignment": round(htf, 1), "technical_indicators": round(techn, 1),
        "market_structure": round(struct, 1), "momentum": round(mom, 1), "fundamental": round(fundamental, 1),
        "notes": notes,
    }


def _volatility(t: TFAnalysis) -> Optional[str]:
    if t.atr_pct_rank is None:
        return None
    return "LOW" if t.atr_pct_rank < 30 else "HIGH" if t.atr_pct_rank > 80 else "NORMAL"


def _warnings(market_open: bool, d1: Optional[TFAnalysis], lean: str, quote: Optional[Quote]) -> list[str]:
    w = []
    if not market_open:
        w.append("Market closed: levels are for planning only; re-validate after the open (weekend gap risk).")
    if d1 is not None and d1.bias not in (lean, "NEUTRAL"):
        w.append(f"Daily trend is {d1.bias}, against the trade.")
    if quote is None or quote.bid is None:
        w.append("No live bid/ask: spread not verified.")
    if quote and quote.update_mode and quote.update_mode != "streaming":
        w.append(f"Quote feed mode '{quote.update_mode}' (not real-time).")
    return w


def _reason(direction: str, plan: Plan, alignment: str, breakdown: dict) -> str:
    trig = f"{plan.trigger.kind.replace('_', ' ').lower()} ({plan.trigger.detail})" if plan.trigger else "pending order at structure"
    return (f"{direction}: trend aligned on {alignment or 'H1/H4'}; entry via {trig}; "
            f"stop beyond structure, R:R to TP2 1:{plan.rr[1]:.1f}.")


def _plan_dict(plan: Plan, inst: Instrument) -> dict:
    return {"signal_type": plan.signal_type, "entry": inst.round_price(plan.entry), "stop": inst.round_price(plan.stop),
            "targets": [inst.round_price(x) for x in plan.tps], "rr": [round(x, 2) for x in plan.rr]}


def _wait(base: dict, code: str, reason: str, tfa: dict[str, TFAnalysis], inst: Instrument,
          lean: Optional[str], extra: Optional[dict]) -> dict:
    h1 = tfa.get("H1")
    return {
        **base,
        "direction": "WAIT",
        "wait_code": code,
        "reason": reason,
        "bias": lean or (h1.bias if h1 else None),
        "confidence_score": (extra or {}).get("confidence_score"),
        "rsi": _r(h1.rsi, 1) if h1 else None,
        "ema_20": inst.round_price(h1.ema20) if h1 else None,
        "ema_50": inst.round_price(h1.ema50) if h1 else None,
        "atr": _r(h1.atr, inst.price_decimals + 1) if h1 and h1.atr else None,
        "timeframe_bias": {tf: t.bias for tf, t in tfa.items()},
        "details": {**(extra or {}), "technicals": {tf: t.summary(inst) for tf, t in tfa.items()},
                    "key_levels": key_levels(tfa, inst)},
    }


def key_levels(tfa: dict[str, TFAnalysis], inst: Instrument, n: int = 3) -> dict:
    """Nearest structural support/resistance (H1 + H4 swing clusters and S/D zones) around the last close."""
    h1 = tfa.get("H1")
    if h1 is None:
        return {"support": [], "resistance": []}
    price = h1.close
    sup, res = [], []
    for tf in ("H1", "H4"):
        t = tfa.get(tf)
        if t is None:
            continue
        for lv in t.structure.levels:
            item = {"price": inst.round_price(lv.price), "tf": tf, "touches": lv.touches, "type": "level"}
            (sup if lv.price < price else res).append(item)
        for z in t.structure.zones:
            edge = z.high if z.kind == "DEMAND" else z.low
            item = {"price": inst.round_price(edge), "tf": tf, "touches": 1, "type": z.kind.lower() + "_zone"}
            (sup if edge < price else res).append(item)
    sup.sort(key=lambda x: -x["price"])
    res.sort(key=lambda x: x["price"])
    obs = [{**ob_dict(o, inst), "timeframe": tf} for tf in ("H1", "H4") if tfa.get(tf) for o in tfa[tf].structure.order_blocks]
    obs.sort(key=lambda o: min(abs(o["low"] - price), abs(o["high"] - price)))
    return {"support": sup[:n], "resistance": res[:n], "order_blocks": obs[:4]}
