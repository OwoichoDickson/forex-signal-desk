import copy
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsignals.config import load_config  # noqa: E402
from fxsignals.models import Candles  # noqa: E402
from fxsignals import structure as st  # noqa: E402
from fxsignals.signals import TFAnalysis  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run tests against the live TradingView MCP connection")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live (TradingView Desktop + MCP server)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"].update(output_dir=str(tmp_path), state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    return c


def make_candles(closes, tf="H1", start=1_790_000_000, spread=None, symbol="EURUSD"):
    step = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}[tf]
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        rng = spread if spread is not None else max(abs(c - prev), abs(c) * 0.0004)
        o = prev
        rows.append({"time": start + i * step, "open": o, "high": max(o, c) + rng * 0.3,
                     "low": min(o, c) - rng * 0.3, "close": c, "volume": 100})
        prev = c
    return Candles.from_rows(symbol, tf, rows)


def zigzag(n, start=1.1000, up=0.0012, down=0.0007, leg=6):
    """Trending zigzag: ``leg`` bars up by ``up`` per bar, then ``leg//2`` bars down by ``down``."""
    out, p, i = [], start, 0
    while len(out) < n:
        for _ in range(leg):
            p += up
            out.append(p)
        for _ in range(leg // 2):
            p -= down
            out.append(p)
        i += 1
    return out[:n]


def tfa(tf="H1", close=1.1000, bias="BULLISH", ema20=None, ema50=None, ema200=None, rsi=58.0,
        macd=0.0004, macd_signal=0.0002, hist=0.0002, hist_prev=0.0001, atr=0.0010, adx=27.0,
        bb_upper=None, bb_lower=None, trend="BULLISH", events=None, swings=None, levels=None,
        zones=None, consolidating=False, range_high=None, range_low=None):
    """Hand-built timeframe analysis for exercising the decision rules on exact inputs."""
    bull = bias == "BULLISH"
    sgn = 1 if bias != "BEARISH" else -1
    ema20 = ema20 if ema20 is not None else close - sgn * 0.5 * atr
    ema50 = ema50 if ema50 is not None else close - sgn * 1.5 * atr
    ema200 = ema200 if ema200 is not None else close - sgn * 4 * atr
    rep = st.StructureReport(trend=trend, swing_labels=["HH", "HL"] if bull else ["LH", "LL"],
                             last_swing_high=None, last_swing_low=None, events=events or [],
                             levels=levels or [], zones=zones or [], consolidating=consolidating,
                             range_high=range_high, range_low=range_low)
    rep._swings = swings or []
    return TFAnalysis(tf=tf, bars=300, close=close, ema9=close, ema20=ema20, ema50=ema50, ema200=ema200, rsi=rsi,
                      macd=macd, macd_signal=macd_signal, macd_hist=hist, macd_hist_prev=hist_prev, atr=atr,
                      atr_pct_rank=50.0, adx=adx, plus_di=25.0, minus_di=15.0,
                      bb_upper=bb_upper if bb_upper is not None else close + 3 * atr,
                      bb_middle=close, bb_lower=bb_lower if bb_lower is not None else close - 3 * atr,
                      vwap=None, structure=rep, bias=bias, votes=4 if bull else -4)


@pytest.fixture
def helpers():
    return {"make_candles": make_candles, "zigzag": zigzag, "tfa": tfa, "math": math}
