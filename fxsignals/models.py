"""Shared data containers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


@dataclass
class Candles:
    symbol: str
    tf: str
    t: list[int] = field(default_factory=list)
    o: list[float] = field(default_factory=list)
    h: list[float] = field(default_factory=list)
    l: list[float] = field(default_factory=list)
    c: list[float] = field(default_factory=list)
    v: list[float] = field(default_factory=list)

    @classmethod
    def from_rows(cls, symbol: str, tf: str, rows: list[dict[str, Any]]) -> "Candles":
        rows = sorted(rows, key=lambda r: r["time"])
        return cls(
            symbol, tf,
            [int(r["time"]) for r in rows],
            [float(r["open"]) for r in rows],
            [float(r["high"]) for r in rows],
            [float(r["low"]) for r in rows],
            [float(r["close"]) for r in rows],
            [float(r.get("volume") or 0.0) for r in rows],
        )

    def __len__(self) -> int:
        return len(self.c)

    def head(self, n: int) -> "Candles":
        return Candles(self.symbol, self.tf, self.t[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.v[:n])

    def window(self, end: int, size: int) -> "Candles":
        """Bars [end-size, end) — the same fixed look-back a live scan sees."""
        s = max(0, end - size)
        return Candles(self.symbol, self.tf, self.t[s:end], self.o[s:end], self.h[s:end], self.l[s:end],
                       self.c[s:end], self.v[s:end])

    def closed_only(self, now_ts: float) -> "Candles":
        """Drop the still-forming last bar (its period has not finished yet)."""
        if self.t and self.t[-1] + TF_SECONDS[self.tf] > now_ts:
            return self.head(len(self) - 1)
        return self

    def last_time(self) -> Optional[int]:
        return self.t[-1] if self.t else None
