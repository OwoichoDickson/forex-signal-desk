"""Market-structure analysis derived strictly from retrieved candles.

Every pattern reported here carries the bar time and price that justify it, so
nothing is claimed without support in the data.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .models import Candles


@dataclass
class Swing:
    idx: int
    t: int
    price: float
    kind: str  # "H" or "L"


@dataclass
class Level:
    price: float
    kind: str        # "SUPPORT" / "RESISTANCE" relative to current price
    touches: int
    last_t: int


@dataclass
class Zone:
    kind: str        # "DEMAND" / "SUPPLY"
    low: float
    high: float
    t: int


@dataclass
class Event:
    kind: str        # BOS / CHOCH / BREAKOUT_RETEST / LIQUIDITY_SWEEP / REJECTION
    direction: str   # BULLISH / BEARISH
    price: float
    t: int
    bars_ago: int
    detail: str = ""
    idx: int = -1        # bar index of the event (break candle for BOS/CHOCH)
    ref_idx: int = -1    # bar index of the swing that was broken


@dataclass
class OrderBlock:
    """Last opposite-colour candle before the impulse that broke structure (SMC order block)."""
    kind: str            # BULLISH (demand) / BEARISH (supply)
    low: float
    high: float
    t: int               # time of the order-block candle
    formed_by: str       # BOS / CHOCH that the impulse produced
    break_t: int         # time of the break candle
    displacement_atr: float
    mitigated: bool      # price has returned into the block since it formed
    mitigated_t: Optional[int] = None
    price_inside: bool = False  # the latest candle trades inside the block

    @property
    def fresh(self) -> bool:
        return not self.mitigated


@dataclass
class StructureReport:
    trend: str                       # BULLISH / BEARISH / RANGING / UNCLEAR
    swing_labels: list[str]          # e.g. ["HH", "HL"]
    last_swing_high: Optional[Swing]
    last_swing_low: Optional[Swing]
    events: list[Event] = field(default_factory=list)
    levels: list[Level] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    consolidating: bool = False
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    order_blocks: list[OrderBlock] = field(default_factory=list)

    def recent(self, kind: str, direction: str, max_bars_ago: int) -> Optional[Event]:
        hits = [e for e in self.events if e.kind == kind and e.direction == direction and e.bars_ago <= max_bars_ago]
        return min(hits, key=lambda e: e.bars_ago) if hits else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["levels"] = d["levels"][:8]
        return d


def find_swings(c: Candles, width: int = 3) -> list[Swing]:
    """Confirmed fractal pivots: a high/low with ``width`` lower highs/higher lows on each side."""
    out: list[Swing] = []
    for i in range(width, len(c) - width):
        hi, lo = c.h[i], c.l[i]
        if all(hi > c.h[j] for j in range(i - width, i)) and all(hi >= c.h[j] for j in range(i + 1, i + width + 1)):
            out.append(Swing(i, c.t[i], hi, "H"))
        if all(lo < c.l[j] for j in range(i - width, i)) and all(lo <= c.l[j] for j in range(i + 1, i + width + 1)):
            out.append(Swing(i, c.t[i], lo, "L"))
    return out


def classify_trend(swings: list[Swing]) -> tuple[str, list[str]]:
    highs = [s for s in swings if s.kind == "H"][-2:]
    lows = [s for s in swings if s.kind == "L"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "UNCLEAR", []
    hl = "HH" if highs[1].price > highs[0].price else "LH"
    ll = "HL" if lows[1].price > lows[0].price else "LL"
    if hl == "HH" and ll == "HL":
        return "BULLISH", [hl, ll]
    if hl == "LH" and ll == "LL":
        return "BEARISH", [hl, ll]
    return "RANGING", [hl, ll]


def bos_choch_events(c: Candles, swings: list[Swing], width: int = 3) -> list[Event]:
    """Walk forward; a close through the latest confirmed swing is a BOS (with trend) or CHOCH (against)."""
    events: list[Event] = []
    swing_by_confirm: dict[int, list[Swing]] = {}
    for s in swings:
        # a pivot only becomes known once ``width`` bars to its right have closed
        swing_by_confirm.setdefault(s.idx + width, []).append(s)
    last_h: Optional[Swing] = None
    last_l: Optional[Swing] = None
    state = "UNCLEAR"
    n = len(c)
    for i in range(n):
        for s in swing_by_confirm.get(i, []):
            if s.kind == "H":
                last_h = s
            else:
                last_l = s
        if last_h and c.c[i] > last_h.price and (i == 0 or c.c[i - 1] <= last_h.price):
            kind = "CHOCH" if state == "BEARISH" else "BOS"
            events.append(Event(kind, "BULLISH", last_h.price, c.t[i], n - 1 - i,
                                f"close {c.c[i]} above swing high {last_h.price}", i, last_h.idx))
            state, last_h = "BULLISH", None
        elif last_l and c.c[i] < last_l.price and (i == 0 or c.c[i - 1] >= last_l.price):
            kind = "CHOCH" if state == "BULLISH" else "BOS"
            events.append(Event(kind, "BEARISH", last_l.price, c.t[i], n - 1 - i,
                                f"close {c.c[i]} below swing low {last_l.price}", i, last_l.idx))
            state, last_l = "BEARISH", None
    return events


def order_blocks(c: Candles, events: list[Event], atr_value: Optional[float], min_displacement_atr: float = 1.0,
                 search_back: int = 10, max_per_side: int = 3) -> list[OrderBlock]:
    """Order blocks from BOS/CHOCH events.

    Bullish OB: the last bearish candle at/before the lowest point of the leg that broke a swing high.
    Bearish OB: the last bullish candle at/before the highest point of the leg that broke a swing low.
    The leg (OB low -> break close) must cover ``min_displacement_atr`` x ATR. A block is *mitigated* once
    price trades back into it and *invalidated* (dropped) once a candle closes through its far side.
    """
    if not atr_value:
        return []
    n = len(c)
    out: list[OrderBlock] = []
    seen: set[int] = set()
    for e in events:
        if e.kind not in ("BOS", "CHOCH") or e.idx < 1:
            continue
        bull = e.direction == "BULLISH"
        start = max(0, e.ref_idx if e.ref_idx >= 0 else e.idx - search_back)
        leg = range(start, e.idx + 1)
        extreme = min(leg, key=lambda j: c.l[j]) if bull else max(leg, key=lambda j: c.h[j])
        ob_i = None
        for j in range(extreme, max(-1, extreme - search_back), -1):
            if (c.c[j] < c.o[j]) if bull else (c.c[j] > c.o[j]):
                ob_i = j
                break
        if ob_i is None or ob_i in seen:
            continue
        lo, hi = c.l[ob_i], c.h[ob_i]
        disp = ((c.c[e.idx] - lo) if bull else (hi - c.c[e.idx])) / atr_value
        if disp < min_displacement_atr:
            continue
        later = range(e.idx + 1, n)
        if any((c.c[j] < lo) if bull else (c.c[j] > hi) for j in later):
            continue  # closed through the block: invalidated
        touch = next((j for j in later if ((c.l[j] <= hi) if bull else (c.h[j] >= lo))), None)
        seen.add(ob_i)
        out.append(OrderBlock(
            kind="BULLISH" if bull else "BEARISH", low=lo, high=hi, t=c.t[ob_i], formed_by=e.kind,
            break_t=e.t, displacement_atr=round(disp, 2), mitigated=touch is not None,
            mitigated_t=None if touch is None else c.t[touch],
            price_inside=c.l[-1] <= hi and c.h[-1] >= lo,
        ))
    bulls = sorted([o for o in out if o.kind == "BULLISH"], key=lambda o: o.t)[-max_per_side:]
    bears = sorted([o for o in out if o.kind == "BEARISH"], key=lambda o: o.t)[-max_per_side:]
    return bulls + bears


def sr_levels(swings: list[Swing], price: float, atr_value: float, tolerance_atr: float = 0.3) -> list[Level]:
    """Cluster swing prices that sit within ``tolerance_atr`` x ATR into support/resistance levels."""
    if not swings or not atr_value:
        return []
    tol = tolerance_atr * atr_value
    clusters: list[list[Swing]] = []
    for s in sorted(swings, key=lambda s: s.price):
        if clusters and abs(s.price - clusters[-1][-1].price) <= tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    levels = []
    for cl in clusters:
        p = sum(s.price for s in cl) / len(cl)
        levels.append(Level(p, "RESISTANCE" if p > price else "SUPPORT", len(cl), max(s.t for s in cl)))
    levels.sort(key=lambda lv: abs(lv.price - price))
    return levels


def supply_demand_zones(c: Candles, atr_series: list, impulse_atr: float = 1.5, max_zones: int = 3) -> list[Zone]:
    """Base candle immediately before an impulsive candle (body >= impulse_atr x ATR), kept while unmitigated."""
    zones: list[Zone] = []
    for i in range(1, len(c)):
        a = atr_series[i - 1]
        if a is None:
            continue
        body = c.c[i] - c.o[i]
        if abs(body) < impulse_atr * a:
            continue
        base_lo, base_hi = c.l[i - 1], c.h[i - 1]
        kind = "DEMAND" if body > 0 else "SUPPLY"
        # invalidated if any later close goes through the far side of the zone
        later = c.c[i + 1:]
        if kind == "DEMAND" and any(x < base_lo for x in later):
            continue
        if kind == "SUPPLY" and any(x > base_hi for x in later):
            continue
        zones.append(Zone(kind, base_lo, base_hi, c.t[i - 1]))
    demand = [z for z in zones if z.kind == "DEMAND"][-max_zones:]
    supply = [z for z in zones if z.kind == "SUPPLY"][-max_zones:]
    return demand + supply


def breakout_retest(c: Candles, levels_source: list[Swing], atr_value: float, lookback: int = 20, tol_atr: float = 0.25) -> list[Event]:
    """Close through a prior swing level, followed by a pull back to the level that holds."""
    out: list[Event] = []
    n = len(c)
    if n < lookback + 5 or not atr_value:
        return out
    tol = tol_atr * atr_value
    start = n - lookback
    for s in levels_source:
        if s.idx >= start:
            continue
        lvl = s.price
        for i in range(start, n):
            if s.kind == "H" and c.c[i] > lvl and c.c[i - 1] <= lvl:
                after = range(i + 1, n)
                touch = [j for j in after if c.l[j] <= lvl + tol]
                if touch and all(c.c[j] > lvl - tol for j in range(i + 1, n)):
                    j = touch[-1]
                    out.append(Event("BREAKOUT_RETEST", "BULLISH", lvl, c.t[j], n - 1 - j, f"broke {lvl:.5f}, retested and held"))
                break
            if s.kind == "L" and c.c[i] < lvl and c.c[i - 1] >= lvl:
                touch = [j for j in range(i + 1, n) if c.h[j] >= lvl - tol]
                if touch and all(c.c[j] < lvl + tol for j in range(i + 1, n)):
                    j = touch[-1]
                    out.append(Event("BREAKOUT_RETEST", "BEARISH", lvl, c.t[j], n - 1 - j, f"broke {lvl:.5f}, retested and held"))
                break
    return out


def liquidity_sweeps(c: Candles, swings: list[Swing], lookback: int = 3) -> list[Event]:
    """Wick beyond a prior swing that closes back inside: stops taken, move rejected."""
    out: list[Event] = []
    n = len(c)
    for i in range(max(0, n - lookback), n):
        prior_h = [s for s in swings if s.kind == "H" and s.idx < i - 1]
        prior_l = [s for s in swings if s.kind == "L" and s.idx < i - 1]
        if prior_h:
            s = prior_h[-1]
            if c.h[i] > s.price and c.c[i] < s.price:
                out.append(Event("LIQUIDITY_SWEEP", "BEARISH", s.price, c.t[i], n - 1 - i, f"swept high {s.price:.5f}, closed back below"))
        if prior_l:
            s = prior_l[-1]
            if c.l[i] < s.price and c.c[i] > s.price:
                out.append(Event("LIQUIDITY_SWEEP", "BULLISH", s.price, c.t[i], n - 1 - i, f"swept low {s.price:.5f}, closed back above"))
    return out


def rejection_candles(c: Candles, lookback: int = 2) -> list[Event]:
    """Pin bars (wick >= 2x body and >= 60% of range) and engulfing candles on the last closed bars."""
    out: list[Event] = []
    n = len(c)
    for i in range(max(1, n - lookback), n):
        o, h, l, cl = c.o[i], c.h[i], c.l[i], c.c[i]
        rng = h - l
        if rng <= 0:
            continue
        body = abs(cl - o)
        upper, lower = h - max(o, cl), min(o, cl) - l
        if lower >= 2 * body and lower >= 0.6 * rng:
            out.append(Event("REJECTION", "BULLISH", l, c.t[i], n - 1 - i, "bullish pin bar"))
        elif upper >= 2 * body and upper >= 0.6 * rng:
            out.append(Event("REJECTION", "BEARISH", h, c.t[i], n - 1 - i, "bearish pin bar"))
        po, pc = c.o[i - 1], c.c[i - 1]
        if pc < po and cl > o and cl >= po and o <= pc:
            out.append(Event("REJECTION", "BULLISH", l, c.t[i], n - 1 - i, "bullish engulfing"))
        elif pc > po and cl < o and cl <= po and o >= pc:
            out.append(Event("REJECTION", "BEARISH", h, c.t[i], n - 1 - i, "bearish engulfing"))
    return out


def analyze(c: Candles, atr_series: list, adx_value: Optional[float], width: int = 3,
            range_bars: int = 20, range_atr: float = 2.5, adx_range_max: float = 20.0,
            ob_cfg: Optional[dict] = None) -> StructureReport:
    swings = find_swings(c, width)
    trend, labels = classify_trend(swings)
    a = next((v for v in reversed(atr_series) if v is not None), None)
    price = c.c[-1]
    events = bos_choch_events(c, swings, width)
    if a:
        events += breakout_retest(c, swings, a)
    events += liquidity_sweeps(c, swings)
    events += rejection_candles(c)
    rep = StructureReport(
        trend=trend,
        swing_labels=labels,
        last_swing_high=next((s for s in reversed(swings) if s.kind == "H"), None),
        last_swing_low=next((s for s in reversed(swings) if s.kind == "L"), None),
        events=events,
        levels=sr_levels(swings, price, a) if a else [],
        zones=supply_demand_zones(c, atr_series),
        order_blocks=order_blocks(c, events, a, **{k: v for k, v in (ob_cfg or {}).items()
                                                   if k in ("min_displacement_atr", "search_back", "max_per_side")}),
    )
    if a and len(c) >= range_bars:
        hi, lo = max(c.h[-range_bars:]), min(c.l[-range_bars:])
        if (hi - lo) <= range_atr * a and (adx_value is not None and adx_value < adx_range_max):
            rep.consolidating, rep.range_high, rep.range_low = True, hi, lo
            if rep.trend in ("UNCLEAR",):
                rep.trend = "RANGING"
    rep._swings = swings  # type: ignore[attr-defined]  # kept for level selection, not serialised
    return rep
