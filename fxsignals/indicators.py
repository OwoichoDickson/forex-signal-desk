"""Technical indicators computed from raw OHLCV candles.

All functions take plain Python lists and return lists aligned to the input,
with ``None`` where the indicator is not yet defined (warm-up period).
Wilder smoothing is used for RSI, ATR and ADX, matching TradingView's built-ins.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Optional, Sequence

Num = Optional[float]


def ema(values: Sequence[float], length: int) -> list[Num]:
    """Exponential moving average seeded with the SMA of the first ``length`` values."""
    out: list[Num] = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    k = 2.0 / (length + 1)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: Sequence[float], length: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    window = sum(values[:length])
    out[length - 1] = window / length
    for i in range(length, len(values)):
        window += values[i] - values[i - length]
        out[i] = window / length
    return out


def _rma(values: Sequence[float], length: int, start: int = 0) -> list[Num]:
    """Wilder's moving average (RMA), seeded with an SMA starting at ``start``."""
    out: list[Num] = [None] * len(values)
    if len(values) - start < length:
        return out
    prev = sum(values[start:start + length]) / length
    out[start + length - 1] = prev
    for i in range(start + length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def rsi(closes: Sequence[float], length: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(closes)
    if len(closes) <= length:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = _rma(gains, length, start=1)
    avg_l = _rma(losses, length, start=1)
    for i in range(len(closes)):
        g, l = avg_g[i], avg_l[i]
        if g is None or l is None:
            continue
        if l == 0:
            out[i] = 100.0 if g > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + g / l)
    return out


@dataclass
class MACD:
    macd: list[Num]
    signal: list[Num]
    hist: list[Num]


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal_len: int = 9) -> MACD:
    ef, es = ema(closes, fast), ema(closes, slow)
    line: list[Num] = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[Num] = [None] * len(closes)
    if first is not None:
        tail = ema([v for v in line[first:]], signal_len)  # type: ignore[misc]
        sig[first:] = tail
    hist: list[Num] = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return MACD(line, sig, hist)


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = [highs[0] - lows[0]] if highs else []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return tr


def atr(highs, lows, closes, length: int = 14) -> list[Num]:
    return _rma(true_range(highs, lows, closes), length)


@dataclass
class ADX:
    adx: list[Num]
    plus_di: list[Num]
    minus_di: list[Num]


def adx(highs, lows, closes, length: int = 14) -> ADX:
    n = len(closes)
    plus_dm, minus_dm = [0.0] * n, [0.0] * n
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    tr = true_range(highs, lows, closes)
    tr_s, p_s, m_s = _rma(tr, length, 1), _rma(plus_dm, length, 1), _rma(minus_dm, length, 1)
    pdi: list[Num] = [None] * n
    mdi: list[Num] = [None] * n
    dx: list[float] = []
    dx_idx: list[int] = []
    for i in range(n):
        if tr_s[i] is None or tr_s[i] == 0:
            continue
        pdi[i] = 100.0 * p_s[i] / tr_s[i]
        mdi[i] = 100.0 * m_s[i] / tr_s[i]
        s = pdi[i] + mdi[i]
        dx.append(0.0 if s == 0 else 100.0 * abs(pdi[i] - mdi[i]) / s)
        dx_idx.append(i)
    adx_out: list[Num] = [None] * n
    smoothed = _rma(dx, length)
    for j, v in enumerate(smoothed):
        if v is not None:
            adx_out[dx_idx[j]] = v
    return ADX(adx_out, pdi, mdi)


@dataclass
class Bands:
    upper: list[Num]
    middle: list[Num]
    lower: list[Num]


def bollinger(closes: Sequence[float], length: int = 20, mult: float = 2.0) -> Bands:
    mid = sma(closes, length)
    up: list[Num] = [None] * len(closes)
    lo: list[Num] = [None] * len(closes)
    for i in range(length - 1, len(closes)):
        window = closes[i - length + 1:i + 1]
        m = mid[i]
        sd = sqrt(sum((x - m) ** 2 for x in window) / length)  # population SD, as TradingView
        up[i], lo[i] = m + mult * sd, m - mult * sd
    return Bands(up, mid, lo)


def session_vwap(times: Sequence[int], highs, lows, closes, volumes, session_start_utc_hour: int = 22) -> list[Num]:
    """VWAP anchored to the FX trading-day roll (17:00 New York ~ 21:00/22:00 UTC).

    Spot FX has no centralised volume; TradingView supplies broker *tick* volume,
    so this VWAP is informational only and never used for scoring.
    """
    out: list[Num] = [None] * len(closes)
    pv = vol = 0.0
    current_day = None
    for i, t in enumerate(times):
        day = (t - session_start_utc_hour * 3600) // 86400
        if day != current_day:
            current_day, pv, vol = day, 0.0, 0.0
        v = volumes[i] or 0.0
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        pv += typical * v
        vol += v
        out[i] = pv / vol if vol > 0 else None
    return out


def last(series: Sequence[Num], offset: int = 0) -> Num:
    idx = len(series) - 1 - offset
    return series[idx] if 0 <= idx < len(series) else None


def percentile_rank(series: Sequence[Num], lookback: int = 100) -> Num:
    vals = [v for v in series[-lookback:] if v is not None]
    if len(vals) < 10:
        return None
    cur = vals[-1]
    return 100.0 * sum(1 for v in vals if v <= cur) / len(vals)


def efficiency(c: list[float], i: int, n: int = 48) -> Optional[float]:
    """Kaufman efficiency ratio over n bars: net move / path length. Near 1 = clean trend, near 0 = chop."""
    if i < n:
        return None
    path = sum(abs(c[k] - c[k - 1]) for k in range(i - n + 1, i + 1))
    return abs(c[i] - c[i - n]) / path if path else None
