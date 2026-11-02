"""Ranking of valid BUY/SELL signals."""
from __future__ import annotations


def _rr(s: dict) -> float:
    try:
        return float(s["risk_reward"].split(":")[1])
    except (KeyError, IndexError, ValueError, AttributeError):
        return 0.0


def rank_signals(results: list[dict], min_confidence: int, top_n: int) -> list[dict]:
    """Sort by confidence, then HTF alignment, R:R, structure quality and news alignment.

    Only BUY/SELL results at or above ``min_confidence`` qualify; the list is never padded.
    """
    valid = [r for r in results if r.get("direction") in ("BUY", "SELL") and (r.get("confidence_score") or 0) >= min_confidence]

    def key(s: dict):
        b = s.get("details", {}).get("score_breakdown", {})
        return (
            -s["confidence_score"],
            -b.get("higher_timeframe_alignment", 0),
            -_rr(s),
            -b.get("market_structure", 0),
            -b.get("fundamental", 0),
            s["symbol"],
        )

    ranked = sorted(valid, key=key)[:top_n]
    for i, s in enumerate(ranked, 1):
        s["rank"] = i
    return ranked
