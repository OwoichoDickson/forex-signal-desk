"""Per-pair market outlook: a recommendation for every instrument, built only from analysed data.

A trade signal is still only issued when every rule passes. For the other pairs the outlook
states the multi-timeframe lean, why there is no trade now, what would change that, and the
price levels worth an alert — so each pair gets a usable view without forcing trades.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Candles

TF_WEIGHTS = {"D1": 0.20, "H4": 0.30, "H1": 0.30, "M15": 0.12, "M5": 0.08}
VOTE = {"BULLISH": 1, "BEARISH": -1}
RATING_VOTE = {"STRONG_BUY": 1, "BUY": 1, "SELL": -1, "STRONG_SELL": -1}


def lean_score(tf_bias: dict[str, Optional[str]]) -> int:
    """-100..100 weighted vote of each timeframe's bias."""
    return int(round(100 * sum(w * VOTE.get(tf_bias.get(tf) or "", 0) for tf, w in TF_WEIGHTS.items())))


def stance_label(score: int) -> str:
    if score >= 60:
        return "STRONG_BULLISH"
    if score >= 25:
        return "BULLISH"
    if score <= -60:
        return "STRONG_BEARISH"
    if score <= -25:
        return "BEARISH"
    return "NEUTRAL"


def _fmt(v) -> str:
    return "—" if v is None else f"{v}"


def watch_for(r: dict, lean: str) -> list[str]:
    """Concrete conditions that would turn a WAIT into a valid setup, from the failed rule and live values."""
    code = r.get("wait_code")
    h1 = ((r.get("details") or {}).get("technicals") or {}).get("H1") or {}
    kl = (r.get("details") or {}).get("key_levels") or {"support": [], "resistance": []}
    s1 = kl["support"][0]["price"] if kl["support"] else None
    r1 = kl["resistance"][0]["price"] if kl["resistance"] else None
    # The direction the engine evaluated (and rejected) decides which way each condition points.
    tried = r.get("bias") if r.get("bias") in ("BULLISH", "BEARISH") else lean
    bull = tried.endswith("BULLISH")
    side = "above" if bull else "below"
    out: list[str] = []
    if code == "EMA_FILTER":
        out.append(f"H1 EMA20 ({_fmt(h1.get('ema_20'))}) to cross {side} EMA50 ({_fmt(h1.get('ema_50'))})")
    elif code == "RSI_MOMENTUM":
        out.append(f"H1 RSI ({_fmt(h1.get('rsi'))}) to move {side} 50")
    elif code == "RSI_EXTREME":
        out.append(f"a pullback toward H1 EMA20 ({_fmt(h1.get('ema_20'))}) — H1 RSI {_fmt(h1.get('rsi'))} is stretched, so do not chase")
    elif code == "MACD_FILTER":
        out.append(f"H1 MACD ({_fmt(h1.get('macd'))}) to cross {side} its signal line ({_fmt(h1.get('macd_signal'))})")
    elif code == "CONFLICT":
        tb = r.get("timeframe_bias") or {}
        out.append(f"H1 ({tb.get('H1')}) and H4 ({tb.get('H4')}) to agree on direction")
    elif code == "NO_TREND":
        if r1 is not None and s1 is not None:
            out.append(f"a decisive H1 close above {r1} (bullish) or below {s1} (bearish) to start a trend")
        else:
            out.append("H1 and H4 to establish a trend")
    elif code == "POOR_RR":
        m = re.search(r" at ([0-9.]+) \(", r.get("reason") or "")  # the obstacle named by the engine
        lvl = float(m.group(1)) if m else (s1 if not bull else r1)
        out.append(f"a clean H1 close through {lvl} (removes the obstacle) or a deeper pullback that improves reward/risk"
                   if lvl is not None else "a pullback that improves reward/risk")
    elif code == "NO_TRIGGER":
        ob = _nearest_ob(kl, "BULLISH" if bull else "BEARISH")
        if ob:
            out.append(f"price to {'pull back' if bull else 'rally'} into the {ob['timeframe']} {ob['kind'].lower()} order block "
                       f"{ob['low']}–{ob['high']}, then an M15 CHoCH/BOS {'up' if bull else 'down'}")
        else:
            out.append("an M15/H1 trigger (break of structure, breakout-retest or sweep) or a pullback into a support/demand level"
                       if bull else "an M15/H1 trigger (break of structure, breakout-retest or sweep) or a rally into a resistance/supply level")
    elif code == "STOP_TOO_WIDE":
        out.append("a tighter swing to form so the stop can sit within 3 ATR")
    elif code == "NEWS_BLACKOUT":
        out.append("the high-impact release to pass, then re-scan")
    elif code == "HIGH_SPREAD":
        out.append("spreads to normalise (typically after the rollover/illiquid hour)")
    elif code == "LOW_CONFIDENCE":
        prop = (r.get("details") or {}).get("proposed")
        if prop:
            out.append(f"more confirmation; provisional {prop['signal_type']} at {prop['entry']} "
                       f"(stop {prop['stop']}) scored {r.get('confidence_score')}, below 65")
    elif code in ("INSUFFICIENT_DATA", "STALE_DATA", "DATA_MISMATCH"):
        out.append("fresh, complete data on the next scan")
    elif code == "OUTSIDE_TOP_N":
        out.append("a slot in the top list (valid setup, lower rank)")
    return out


def _nearest_ob(kl: dict, kind: str) -> Optional[dict]:
    """Closest still-valid order block of the given direction (fresh ones first)."""
    obs = [o for o in kl.get("order_blocks") or [] if o["kind"] == kind]
    obs.sort(key=lambda o: (not o["fresh"],))
    return obs[0] if obs else None


def alert_levels(lean: str, kl: dict) -> list[dict]:
    s = kl["support"][0] if kl.get("support") else None
    r = kl["resistance"][0] if kl.get("resistance") else None
    out = []
    if lean.endswith("BULLISH") or lean.endswith("BEARISH"):
        kind = "BULLISH" if lean.endswith("BULLISH") else "BEARISH"
        ob = _nearest_ob(kl, kind)
        if ob:
            out.append({"price": ob["high"] if kind == "BULLISH" else ob["low"],
                        "why": f"{ob['timeframe']} {kind.lower()} order block {ob['low']}–{ob['high']} "
                               f"({'fresh' if ob['fresh'] else 'tapped'}) — {'buy' if kind == 'BULLISH' else 'sell'} zone"})
    if lean.endswith("BULLISH"):
        if r:
            out.append({"price": r["price"], "why": "breakout trigger (close above resistance)"})
        if s:
            out.append({"price": s["price"], "why": "pullback-buy area / bullish view weakens below"})
    elif lean.endswith("BEARISH"):
        if s:
            out.append({"price": s["price"], "why": "breakdown trigger (close below support)"})
        if r:
            out.append({"price": r["price"], "why": "pullback-sell area / bearish view weakens above"})
    else:
        if r:
            out.append({"price": r["price"], "why": "range top — bullish if broken"})
        if s:
            out.append({"price": s["price"], "why": "range bottom — bearish if broken"})
    return out


def build_outlook(r: dict) -> dict:
    tech = (r.get("details") or {}).get("technicals") or {}
    tf_bias = {tf: t.get("bias") for tf, t in tech.items()}
    score = lean_score(tf_bias)
    lean = stance_label(score)
    ratings = ((r.get("live") or {}).get("tv_ratings")) or {}
    rating_votes = [RATING_VOTE.get(v, 0) for v in ratings.values() if v]
    tv_net = sum(rating_votes)
    agree = None
    if rating_votes and lean != "NEUTRAL":
        agree = (tv_net > 0) == lean.endswith("BULLISH") and tv_net != 0
    kl = (r.get("details") or {}).get("key_levels") or {"support": [], "resistance": []}
    aligned = [tf for tf in ("M5", "M15", "H1", "H4", "D1") if tf_bias.get(tf) in ("BULLISH", "BEARISH")
               and (VOTE[tf_bias[tf]] > 0) == (score > 0) and score != 0]
    if r.get("direction") in ("BUY", "SELL"):
        action = f"{r['direction']} — {r.get('signal_type')} (see signal)"
        conditions: list[str] = []
    else:
        action = "WAIT"
        conditions = watch_for(r, lean)
    words = lean.replace("_", " ").lower()
    summary = f"{words.capitalize()} lean ({', '.join(aligned) or 'no aligned timeframes'})."
    if r.get("direction") in ("BUY", "SELL"):
        summary += f" Trade signal: {r['direction']} {r.get('signal_type')} at {r.get('entry_price')}."
    else:
        summary += f" No trade now: {r.get('reason')}."
        if conditions:
            summary += f" Watch for {conditions[0]}."
    if agree is False:
        summary += " TradingView's own ratings disagree — treat the lean with extra caution."
    return {
        "symbol": r["symbol"],
        "lean": lean,
        "lean_score": score,
        "action": action,
        "timeframes": {tf: {"bias": tf_bias.get(tf), "tv_rating": ratings.get(tf), "rsi": (tech.get(tf) or {}).get("rsi"),
                            "structure": (tech.get(tf) or {}).get("swing_labels"),
                            "last_break": (tech.get(tf) or {}).get("last_bos_choch")}
                       for tf in ("M5", "M15", "H1", "H4", "D1")},
        "order_blocks": kl.get("order_blocks", []),
        "tv_ratings_agree": agree,
        "watch_for": conditions,
        "alert_levels": alert_levels(lean, kl),
        "key_levels": kl,
        "economic_risk": r.get("economic_risk"),
        "summary": summary,
    }


# ── currency strength ─────────────────────────────────────────────────────────
def pct_change(c: Optional[Candles], bars_back: int) -> Optional[float]:
    if c is None or len(c) <= bars_back:
        return None
    a, b = c.c[-1 - bars_back], c.c[-1]
    return (b / a - 1) * 100 if a else None


def currency_strength(candles: dict[str, dict[str, Optional[Candles]]]) -> list[dict]:
    """Average % move of each currency against the others (base +, quote -) over ~1 day and ~1 week."""
    acc: dict[str, dict[str, list[float]]] = {}
    for pair, tfs in candles.items():
        base, quote = pair[:3], pair[3:6]
        if {"XAU", "BTC"} & {base, quote}:
            continue
        for key, val in (("day", pct_change(tfs.get("H1"), 24)), ("week", pct_change(tfs.get("D1"), 5))):
            if val is None:
                continue
            acc.setdefault(base, {}).setdefault(key, []).append(val)
            acc.setdefault(quote, {}).setdefault(key, []).append(-val)
    rows = []
    for ccy, d in acc.items():
        day = sum(d.get("day", [])) / len(d["day"]) if d.get("day") else None
        week = sum(d.get("week", [])) / len(d["week"]) if d.get("week") else None
        rows.append({"currency": ccy, "day_pct": None if day is None else round(day, 3),
                     "week_pct": None if week is None else round(week, 3), "pairs": len(d.get("day", []))})
    rows.sort(key=lambda x: -(x["day_pct"] if x["day_pct"] is not None else -1e9))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def strength_pairings(strength: list[dict], monitored: list[str], outlooks: dict[str, dict]) -> list[dict]:
    """Strong-vs-weak combinations among monitored pairs, flagged only where the pair's own outlook agrees."""
    if len(strength) < 2:
        return []
    out = []
    strong = [r["currency"] for r in strength[:2]]
    weak = [r["currency"] for r in strength[-2:]]
    for s in strong:
        for w in weak:
            if s == w:
                continue
            if s + w in monitored:
                pair, favoured = s + w, "BULLISH"
            elif w + s in monitored:
                pair, favoured = w + s, "BEARISH"
            else:
                continue
            ol = outlooks.get(pair, {})
            agrees = ol.get("lean", "").endswith(favoured)
            out.append({"pair": pair, "strong": s, "weak": w, "favoured": favoured,
                        "outlook_agrees": agrees, "pair_lean": ol.get("lean"),
                        "note": f"{s} strong vs {w} weak favours {pair} {'higher' if favoured == 'BULLISH' else 'lower'}"
                                + ("; the pair's own multi-timeframe lean agrees" if agrees else
                                   "; the pair's own lean does not confirm — no edge yet")})
    return out
