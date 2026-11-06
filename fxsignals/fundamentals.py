"""Economic-calendar and headline analysis for both currencies of each pair.

Only measurable facts are used: scheduled event times/importance and released
actual-vs-forecast surprises. Headlines are attached as context but are never
sentiment-scored, so no narrative is invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

COUNTRY_TO_CCY = {
    "US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "GB": "GBP", "UK": "GBP", "JP": "JPY", "CH": "CHF", "AU": "AUD", "NZ": "NZD", "CA": "CAD",
}
CCY_KEYWORDS = {
    "USD": ["dollar", "greenback", "fed", "federal reserve", "fomc", "powell", "usd"],
    "EUR": ["euro", "ecb", "lagarde", "eurozone", "euro zone", "eur"],
    "GBP": ["sterling", "pound", "boe", "bank of england", "bailey", "gbp"],
    "JPY": ["yen", "boj", "bank of japan", "ueda", "japan", "japanese", "jpy"],
    "CHF": ["franc", "snb", "swiss", "chf"],
    "AUD": ["aussie", "rba", "australia", "australian", "aud"],
    "NZD": ["kiwi", "rbnz", "new zealand", "nzd"],
    "CAD": ["loonie", "boc", "bank of canada", "canada", "canadian", "cad"],
    "XAU": ["gold", "bullion", "xau"],
    "BTC": ["bitcoin", "btc", "crypto", "cryptocurrency"],
}
CCY_PATTERNS = {c: re.compile(r"\b(" + "|".join(re.escape(k) for k in kws) + r")\b", re.I) for c, kws in CCY_KEYWORDS.items()}
# Higher-than-forecast prints that are negative for the currency.
INVERSE_TITLES = re.compile(r"unemployment|jobless|claims", re.I)
IMPORTANCE_WEIGHT = {1: 2, 0: 1, -1: 0}


@dataclass
class CalendarEvent:
    date: datetime
    currency: str
    title: str
    importance: int
    actual: Optional[float]
    forecast: Optional[float]
    previous: Optional[float]

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(), "currency": self.currency, "title": self.title,
            "importance": {1: "HIGH", 0: "MEDIUM", -1: "LOW"}.get(self.importance, str(self.importance)),
            "actual": self.actual, "forecast": self.forecast, "previous": self.previous,
        }


@dataclass
class PairFundamentals:
    available: bool
    blackout_events: list[CalendarEvent] = field(default_factory=list)   # high impact inside the blackout window
    upcoming_high: list[CalendarEvent] = field(default_factory=list)     # high impact within caution window
    surprises: list[dict] = field(default_factory=list)
    bias: Optional[int] = None    # >0 favours base currency, <0 favours quote currency, 0 neutral
    headlines: list[dict] = field(default_factory=list)

    @property
    def economic_risk(self) -> Optional[str]:
        if not self.available:
            return None
        if self.blackout_events:
            return "HIGH"
        if self.upcoming_high:
            return "MEDIUM"
        return "LOW"


def parse_events(raw: list[dict[str, Any]]) -> list[CalendarEvent]:
    out = []
    for e in raw or []:
        ccy = e.get("currency") or COUNTRY_TO_CCY.get(str(e.get("country", "")).upper())
        if not ccy or not e.get("date"):
            continue
        try:
            dt = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append(CalendarEvent(dt, ccy.upper(), str(e.get("title", "")), int(e.get("importance", -1) or 0),
                                 _num(e.get("actual")), _num(e.get("forecast")), _num(e.get("previous"))))
    return sorted(out, key=lambda ev: ev.date)


def _num(v) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def currency_surprise_bias(events: list[CalendarEvent], ccy: str, now: datetime, lookback_h: int = 48) -> tuple[int, list[dict]]:
    score, used = 0, []
    for ev in events:
        if ev.currency != ccy or ev.actual is None or ev.forecast is None:
            continue
        if not (now - timedelta(hours=lookback_h) <= ev.date <= now):
            continue
        diff = ev.actual - ev.forecast
        if diff == 0:
            continue
        sign = 1 if diff > 0 else -1
        if INVERSE_TITLES.search(ev.title):
            sign = -sign
        w = IMPORTANCE_WEIGHT.get(ev.importance, 0)
        if w == 0:
            continue
        score += sign * w
        used.append({"currency": ccy, "title": ev.title, "date": ev.date.isoformat(), "actual": ev.actual,
                     "forecast": ev.forecast, "effect": f"{ccy}-{'positive' if sign > 0 else 'negative'} surprise"})
    return score, used


def pair_currencies(pair: str) -> tuple[str, str]:
    return pair[:3].upper(), pair[3:6].upper()


def analyse_pair(pair: str, events: Optional[list[CalendarEvent]], headlines: Optional[list[dict]], now: datetime,
                 blackout_before_min: int = 60, blackout_after_min: int = 15, caution_hours: int = 4) -> PairFundamentals:
    base, quote = pair_currencies(pair)
    ccys = {base, quote} - {"XAU", "BTC"}
    if events is None:
        return PairFundamentals(available=False, headlines=filter_headlines(headlines, pair, now))
    window_start = now - timedelta(minutes=blackout_after_min)
    blackout_end = now + timedelta(minutes=blackout_before_min)
    caution_end = now + timedelta(hours=caution_hours)
    rel = [e for e in events if e.currency in ccys and e.importance >= 1]
    pf = PairFundamentals(
        available=True,
        blackout_events=[e for e in rel if window_start <= e.date <= blackout_end],
        upcoming_high=[e for e in rel if blackout_end < e.date <= caution_end],
        headlines=filter_headlines(headlines, pair, now),
    )
    b_score, b_used = currency_surprise_bias(events, base, now) if base not in ("XAU", "BTC") else (0, [])
    q_score, q_used = currency_surprise_bias(events, quote, now)
    pf.surprises = b_used + q_used
    pf.bias = b_score - q_score
    return pf


def filter_headlines(items: Optional[list[dict]], pair: str, now: datetime, max_age_h: int = 24, limit: int = 5) -> list[dict]:
    if not items:
        return []
    base, quote = pair_currencies(pair)
    pats = [CCY_PATTERNS[c] for c in (base, quote) if c in CCY_PATTERNS]
    out, seen = [], set()
    cutoff = (now - timedelta(hours=max_age_h)).timestamp()
    for it in items:
        title = str(it.get("title", ""))
        if not title or title in seen or (it.get("published") or 0) < cutoff:
            continue
        # relatedSymbols on TradingView headlines are broad, so only the headline text decides relevance
        if any(p.search(title) for p in pats):
            seen.add(title)
            out.append({"title": title, "source": it.get("source"), "published": datetime.fromtimestamp(it["published"], timezone.utc).isoformat(), "url": it.get("url")})
        if len(out) >= limit:
            break
    return out


def catalyst_text(pf: PairFundamentals, pair: str) -> Optional[str]:
    """Plain statement of measurable catalysts; None when nothing measurable exists."""
    if not pf.available:
        return "Economic calendar unavailable — fundamental risk not assessed."
    parts = []
    if pf.blackout_events:
        e = pf.blackout_events[0]
        parts.append(f"High-impact {e.currency} '{e.title}' at {e.date:%Y-%m-%d %H:%M} UTC is inside the news blackout window.")
    elif pf.upcoming_high:
        e = pf.upcoming_high[0]
        parts.append(f"Next high-impact event: {e.currency} '{e.title}' at {e.date:%Y-%m-%d %H:%M} UTC.")
    for s in pf.surprises[:2]:
        parts.append(f"{s['title']} ({s['currency']}) printed {s['actual']} vs {s['forecast']} forecast — {s['effect']}.")
    if not parts:
        parts.append("No high-impact scheduled events for either currency in the caution window; no recent data surprises.")
    return " ".join(parts)
