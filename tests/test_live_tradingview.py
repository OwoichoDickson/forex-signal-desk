"""Live checks against TradingView Desktop through the MCP server. Run with: pytest --live

These read data only; the chart is restored to its original symbol/timeframe afterwards.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from fxsignals import indicators as ind
from fxsignals.market_data import TF_CODES, TradingViewData
from fxsignals.mcp_client import TradingViewMCP

pytestmark = pytest.mark.live
PAIRS = ["EURUSD", "GBPUSD", "USDJPY"]


def run(coro):
    return asyncio.run(coro)


def test_connectivity_quotes_and_candles(cfg):
    async def go():
        async with TradingViewMCP(cfg) as mcp:
            assert {"tv_health_check", "symbol_search", "ui_evaluate", "chart_get_state"} <= set(mcp.tools)
            d = TradingViewData(mcp, cfg)
            await d.health()
            original = await d.chart_state()
            try:
                tickers = await d.resolve_symbols(PAIRS)
                quotes = await d.screener(tickers)
                candles = await d.load_candles(tickers, ["H1", "D1"])
            finally:
                await d.restore_chart(original)
            after = await d.chart_state()
            return tickers, quotes, candles, original, after
    tickers, quotes, candles, original, after = run(go())
    assert set(tickers) == set(PAIRS) and all(":" in t for t in tickers.values())
    assert after["symbol"] == original["symbol"]
    for p in PAIRS:
        q = quotes[p]
        assert q.last and q.bid and q.ask and q.ask >= q.bid
        assert q.update_mode == "streaming"  # real-time feed
        h1 = candles[p]["H1"]
        assert len(h1) >= 200
        assert all(h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9 for o, h, l, c in zip(h1.o, h1.h, h1.l, h1.c))
        assert all(b > a for a, b in zip(h1.t, h1.t[1:]))
        # live quote agrees with the latest candle (same instrument, current data)
        assert abs(q.last - h1.c[-1]) / q.last < 0.005
        assert abs(candles[p]["D1"].c[-1] - h1.c[-1]) / q.last < 0.005


def test_rsi_matches_tradingview_indicator(cfg):
    """Compare our RSI(14) with TradingView's own RSI study on the same chart, when one is present."""
    async def go():
        async with TradingViewMCP(cfg) as mcp:
            d = TradingViewData(mcp, cfg)
            original = await d.chart_state()
            try:
                if not any("relative strength" in s["name"].lower() for s in original.get("studies", [])):
                    return None
                tickers = await d.resolve_symbols(["USDJPY"])
                c = (await d.load_candles(tickers, ["H1"]))["USDJPY"]["H1"]
                ours = ind.last(ind.rsi(c.c, 14))
                tv = None
                for _ in range(10):  # chart studies recalculate a moment after the series switches
                    vals = await mcp.call("data_get_study_values")
                    tv = next(float(v.replace(",", "")) for s in vals["studies"]
                              if "relative strength" in s["name"].lower() for v in s["values"].values())
                    if abs(ours - tv) < 0.05:
                        break
                    await asyncio.sleep(0.5)
            finally:
                await d.restore_chart(original)
            return ours, tv
    out = run(go())
    if out is None:
        pytest.skip("no RSI study on the chart to compare with")
    ours, tv = out
    # Same bars, same Wilder smoothing: values agree to the displayed precision
    assert abs(ours - tv) < 0.05, (ours, tv)


def test_calendar_and_headlines(cfg):
    async def go():
        async with TradingViewMCP(cfg) as mcp:
            d = TradingViewData(mcp, cfg)
            ev = await d.calendar(datetime.now(timezone.utc))
            news = await d.headlines({"EURUSD": "OANDA:EURUSD"})
            return ev, news, d.limitations
    ev, news, lim = run(go())
    assert ev is not None, lim
    assert all({"date", "title", "importance"} <= set(e) for e in ev)
    assert news is None or isinstance(news, list)


def test_timeframe_codes_cover_all():
    assert set(TF_CODES) == {"M5", "M15", "H1", "H4", "D1"}
