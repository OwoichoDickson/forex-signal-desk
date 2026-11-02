"""Market-data retrieval through the TradingView MCP server.

Tool usage (all real tools of the connected server):
  tv_health_check, symbol_search, chart_get_state, chart_set_symbol, chart_set_timeframe,
  ui_evaluate.

``ui_evaluate`` runs read-only JavaScript inside TradingView Desktop. It is used to
(1) read the loaded candles together with the symbol/resolution they belong to, so a
half-loaded chart can never be mistaken for the requested instrument, and (2) query
TradingView's own screener (bid/ask, update mode, technical ratings), economic-calendar
and headline endpoints — the MCP server has no dedicated tools for those. These
endpoints are undocumented; every consumer degrades gracefully when they fail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .history import atomic_write_json
from .logging_setup import log
from .mcp_client import ToolError, TradingViewMCP
from .models import TF_SECONDS, Candles
from .signals import Quote

logger = logging.getLogger("fxsignals.data")

TF_CODES = {"M5": "5", "M15": "15", "H1": "60", "H4": "240", "D1": "D"}
CAL_COUNTRIES = "US,EU,DE,GB,JP,CH,AU,NZ,CA"
RATING_COLS = {"M5": "Recommend.All|5", "M15": "Recommend.All|15", "H1": "Recommend.All|60",
               "H4": "Recommend.All|240", "D1": "Recommend.All"}

READ_BARS_JS = """(() => {
  const w = window.TradingViewApi._activeChartWidgetWV.value();
  const s = w._chartWidget.model().mainSeries();
  const b = s.bars();
  const out = [];
  if (b && b.lastIndex() !== null) {
    for (let i = Math.max(b.firstIndex(), b.lastIndex() - %d + 1); i <= b.lastIndex(); i++) {
      const v = b.valueAt(i);
      if (v) out.push([v[0], v[1], v[2], v[3], v[4], v[5] || 0]);
    }
  }
  const si = s.symbolInfo() || {};
  return {symbol: w.symbol(), series_symbol: si.pro_name || si.full_name || null, resolution: w.resolution(), bars: out};
})()"""


# chart_set_symbol's readiness wait compares the legend text (a description such as
# "Euro / U.S. Dollar") with the ticker, so for broker tickers it always runs to its 10 s
# timeout. Switching via the same chart API and verifying the loaded series ourselves is
# both faster and stricter.
SET_SYMBOL_JS = "(() => { window.TradingViewApi._activeChartWidgetWV.value().setSymbol(%s, {}); return true; })()"


MORE_BARS_JS = """(() => {
  const s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();
  const b = s.bars();
  const n = b.lastIndex() - b.firstIndex() + 1;
  const more = s.requestMoreDataAvailable ? s.requestMoreDataAvailable() : false;
  if (n < %d && more && !(s.isLoading && s.isLoading())) s.requestMoreData(1000);
  return {n: n, more: more};
})()"""


def _norm_res(r: str) -> str:
    r = str(r).upper()
    return {"1D": "D", "D": "D"}.get(r, r)


class TradingViewData:
    def __init__(self, mcp: TradingViewMCP, cfg: dict):
        self.mcp = mcp
        self.cfg = cfg
        self.state_dir = Path(cfg["paths"]["state_dir"])
        self.limitations: list[str] = []
        self._last_sig: Optional[tuple] = None

    # ── connectivity ──────────────────────────────────────────────────────────
    async def health(self) -> dict:
        h = await self.mcp.call("tv_health_check", retries=1)
        if not (h.get("cdp_connected") and h.get("api_available")):
            raise ToolError(f"TradingView not ready: {h}")
        return h

    async def chart_state(self) -> dict:
        return await self.mcp.call("chart_get_state")

    async def begin_chart_session(self) -> dict:
        """Remember the user's chart before switching it. The state is written to disk so that a scan
        killed half-way (restart, crash) still restores the *user's* chart next time, not a scan pair."""
        path = self.state_dir / "chart_restore.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                log(logger, logging.WARNING, "previous scan was interrupted; restoring the chart it saved", symbol=saved.get("symbol"))
                return saved
            except json.JSONDecodeError:
                pass
        state = await self.chart_state()
        atomic_write_json(path, {"symbol": state.get("symbol"), "resolution": state.get("resolution")})
        return state

    async def end_chart_session(self, state: dict) -> None:
        await self.restore_chart(state)
        (self.state_dir / "chart_restore.json").unlink(missing_ok=True)

    async def restore_chart(self, state: dict) -> None:
        try:
            if state.get("symbol"):
                await self.mcp.call("ui_evaluate", {"expression": SET_SYMBOL_JS % json.dumps(state["symbol"])})
            if state.get("resolution"):
                await self.mcp.call("chart_set_timeframe", {"timeframe": str(state["resolution"]).replace("1D", "D")})
        except ToolError as e:
            log(logger, logging.WARNING, "could not restore chart", error=str(e))

    # ── symbols ───────────────────────────────────────────────────────────────
    async def resolve_symbols(self, pairs: list[str]) -> dict[str, str]:
        """Map each pair to a TradingView ticker chosen from symbol_search results (cached 7 days)."""
        cache_path = self.state_dir / "symbols.json"
        prefs = [p.upper() for p in self.cfg["data"]["preferred_exchanges"]]
        cache: dict = {}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
            except json.JSONDecodeError:
                cache = {}
        fresh = cache.get("resolved_at") and time.time() - cache["resolved_at"] < 7 * 86400 and cache.get("prefs") == prefs
        mapping: dict[str, str] = dict(cache.get("symbols", {})) if fresh else {}
        for pair in pairs:
            if pair in mapping:
                continue
            res = await self.mcp.call("symbol_search", {"query": pair})
            cands = [r for r in res.get("results", []) if r.get("symbol", "").upper() == pair]
            by_ex = {r["exchange"].upper(): r["full_name"] for r in cands}
            chosen = next((by_ex[p] for p in prefs if p in by_ex), None)
            if not chosen:
                # Search results are capped at 15; query the preferred exchanges explicitly.
                for p in prefs:
                    res = await self.mcp.call("symbol_search", {"query": f"{p}:{pair}"})
                    hit = [r for r in res.get("results", []) if r.get("symbol", "").upper() == pair and r["exchange"].upper() == p]
                    if hit:
                        chosen = hit[0]["full_name"]
                        break
            if not chosen and cands:
                chosen = cands[0]["full_name"]
            if chosen:
                mapping[pair] = chosen
            else:
                log(logger, logging.ERROR, "symbol not found", pair=pair)
        atomic_write_json(cache_path, {"resolved_at": cache.get("resolved_at") if fresh else time.time(),
                                       "prefs": prefs, "symbols": mapping})
        return {p: mapping[p] for p in pairs if p in mapping}

    # ── candles ───────────────────────────────────────────────────────────────
    async def _read_bars(self, ticker: str, tf: str, count: int, timeout_s: float = 15.0,
                         reject_sig: Optional[tuple] = None) -> Optional[Candles]:
        """Poll until the chart's series reports the requested symbol + resolution and the bar set is stable.

        ``reject_sig`` is the signature of the previously loaded series: identical data means the
        chart has not swapped series yet, so it is never accepted as the new instrument.
        """
        want_res = _norm_res(TF_CODES[tf])
        deadline = time.monotonic() + timeout_s
        prev_sig = None
        while time.monotonic() < deadline:
            r = (await self.mcp.call("ui_evaluate", {"expression": READ_BARS_JS % count})).get("result") or {}
            bars = r.get("bars") or []
            sym_ok = (str(r.get("symbol", "")).upper() == ticker.upper()
                      and str(r.get("series_symbol") or "").upper() == ticker.upper())
            res_ok = _norm_res(r.get("resolution", "")) == want_res
            if sym_ok and res_ok and bars:
                sig = (len(bars), bars[0][0], bars[-1][0], bars[-1][4])
                if sig == prev_sig and sig != reject_sig:  # two identical reads of new data: loading finished
                    self._last_sig = sig
                    rows = [{"time": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in bars]
                    return Candles.from_rows(ticker.split(":")[-1], tf, rows)
                prev_sig = sig
            await asyncio.sleep(0.3)
        log(logger, logging.WARNING, "bars not loaded for requested symbol/timeframe", ticker=ticker, tf=tf)
        return None

    async def load_candles(self, tickers: dict[str, str], timeframes: list[str]) -> dict[str, dict[str, Optional[Candles]]]:
        """Load every pair on every timeframe. Loops timeframe-outer to minimise resolution switches."""
        out: dict[str, dict[str, Optional[Candles]]] = {p: {} for p in tickers}
        bars = self.cfg["data"]["bars"]
        for tf in timeframes:
            await self.mcp.call("chart_set_timeframe", {"timeframe": TF_CODES[tf]})
            for pair, ticker in tickers.items():
                try:
                    await self.mcp.call("ui_evaluate", {"expression": SET_SYMBOL_JS % json.dumps(ticker)})
                    out[pair][tf] = await self._read_bars(ticker, tf, bars[tf], reject_sig=self._last_sig)
                except ToolError as e:
                    log(logger, logging.WARNING, "candle load failed", pair=pair, tf=tf, error=str(e))
                    out[pair][tf] = None
            log(logger, logging.INFO, "timeframe loaded", tf=tf,
                ok=sum(1 for p in out if out[p].get(tf) is not None), total=len(tickers))
        return out

    async def load_history(self, tickers: dict[str, str], targets: dict[str, int],
                           per_load_timeout_s: float = 40.0) -> dict[str, dict[str, Optional[Candles]]]:
        """Deep history for backtests: after the normal load, ask the chart for older bars
        (series.requestMoreData) until ``targets[tf]`` bars are loaded or the feed has no more."""
        out: dict[str, dict[str, Optional[Candles]]] = {p: {} for p in tickers}
        for tf, target in targets.items():
            await self.mcp.call("chart_set_timeframe", {"timeframe": TF_CODES[tf]})
            for pair, ticker in tickers.items():
                await self.mcp.call("ui_evaluate", {"expression": SET_SYMBOL_JS % json.dumps(ticker)})
                first = await self._read_bars(ticker, tf, 50, reject_sig=self._last_sig)
                if first is None:
                    out[pair][tf] = None
                    continue
                deadline = time.monotonic() + per_load_timeout_s
                last_n, stalls = -1, 0
                while time.monotonic() < deadline:
                    r = (await self.mcp.call("ui_evaluate", {"expression": MORE_BARS_JS % target})).get("result") or {}
                    n = r.get("n", 0)
                    if n >= target or not r.get("more"):
                        break
                    stalls = stalls + 1 if n == last_n else 0
                    if stalls >= 6:
                        break
                    last_n = n
                    await asyncio.sleep(0.7)
                out[pair][tf] = await self._read_bars(ticker, tf, target, timeout_s=20)
                log(logger, logging.INFO, "history loaded", pair=pair, tf=tf,
                    bars=0 if out[pair][tf] is None else len(out[pair][tf]))
        return out

    # ── async page fetches (screener / calendar / news) ───────────────────────
    async def _page_fetch(self, start_js: str, timeout_s: float = 20.0) -> Any:
        key = "k" + uuid.uuid4().hex[:10]
        js = ("(() => { window.__fxsig = window.__fxsig || {}; const K = %s; window.__fxsig[K] = {done:false};"
              " const done = (d) => { window.__fxsig[K] = {done:true, data:d}; };"
              " const fail = (e) => { window.__fxsig[K] = {done:true, error:String(e && e.message || e)}; };"
              " try { %s } catch (e) { fail(e); } return 'started'; })()") % (json.dumps(key), start_js)
        await self.mcp.call("ui_evaluate", {"expression": js})
        poll = ("(() => { const s = window.__fxsig || {}; const r = s[%s];"
                " if (r && r.done) { delete s[%s]; return r; } return {done:false}; })()") % (json.dumps(key), json.dumps(key))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.8)
            r = (await self.mcp.call("ui_evaluate", {"expression": poll})).get("result") or {}
            if r.get("done"):
                if r.get("error"):
                    raise ToolError(r["error"])
                return r.get("data")
        await self.mcp.call("ui_evaluate", {"expression": "(() => { delete (window.__fxsig||{})[%s]; return 1; })()" % json.dumps(key)})
        raise ToolError("page fetch timed out")

    async def screener(self, tickers: dict[str, str]) -> dict[str, Quote]:
        cols = ["close", "bid", "ask", "change", "update_mode", *RATING_COLS.values()]
        body = json.dumps({"symbols": {"tickers": list(tickers.values())}, "columns": cols})
        js = ("Promise.all(['forex','cfd'].map(m => fetch('https://scanner.tradingview.com/' + m + '/scan',"
              " {method:'POST', body:%s}).then(r => r.ok ? r.json() : {data:[]}).catch(() => ({data:[]}))))"
              ".then(a => done(a.flatMap(x => x.data || []))).catch(fail);") % json.dumps(body)
        try:
            rows = await self._page_fetch(js)
        except ToolError as e:
            self.limitations.append(f"Live quotes/spreads unavailable (screener fetch failed: {e}); using candle closes.")
            return {}
        by_ticker = {r["s"].upper(): dict(zip(cols, r["d"])) for r in rows or []}
        quotes: dict[str, Quote] = {}
        for pair, t in tickers.items():
            d = by_ticker.get(t.upper())
            if not d:
                continue
            ratings = {tf: _rating_label(d.get(col)) for tf, col in RATING_COLS.items()}
            quotes[pair] = Quote(last=d.get("close"), bid=d.get("bid"), ask=d.get("ask"),
                                 change_pct=d.get("change"), update_mode=d.get("update_mode"), ratings=ratings)
        return quotes

    async def calendar(self, now: datetime) -> Optional[list[dict]]:
        cache = self.state_dir / "calendar_cache.json"
        ttl = self.cfg["data"]["calendar_cache_minutes"] * 60
        if cache.exists():
            c = json.loads(cache.read_text())
            if time.time() - c.get("fetched_at", 0) < ttl:
                return c["events"]
        frm = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        url = (f"https://economic-calendar.tradingview.com/events?from={frm}&to={to}"
               f"&countries={CAL_COUNTRIES}&minImportance=0")
        js = ("fetch(%s).then(r => r.json()).then(j => done((j.result || []).map(e => ({date:e.date, country:e.country,"
              " currency:e.currency, title:e.title, importance:e.importance, actual:e.actual, forecast:e.forecast,"
              " previous:e.previous})))).catch(fail);") % json.dumps(url)
        try:
            events = await self._page_fetch(js)
        except ToolError as e:
            self.limitations.append(f"Economic calendar unavailable ({e}); news-risk filter cannot be applied.")
            return None
        atomic_write_json(cache, {"fetched_at": time.time(), "events": events})
        return events

    async def headlines(self, tickers: dict[str, str]) -> Optional[list[dict]]:
        if not self.cfg["data"].get("news_enabled", True):
            return None
        cache = self.state_dir / "news_cache.json"
        ttl = self.cfg["data"]["news_cache_minutes"] * 60
        if cache.exists():
            c = json.loads(cache.read_text())
            if time.time() - c.get("fetched_at", 0) < ttl:
                return c["items"]
        syms = json.dumps(list(tickers.values()))
        js = ("(async () => { const seen = {}; const out = [];"
              " for (const s of %s) {"
              "  try { const r = await fetch('https://news-headlines.tradingview.com/v2/headlines?client=web&lang=en&symbol=' + encodeURIComponent(s));"
              "   const j = await r.json();"
              "   for (const i of (j.items || []).slice(0, 40)) { if (seen[i.id]) continue; seen[i.id] = 1;"
              "     out.push({id:i.id, title:i.title, source:i.source || i.provider, published:i.published,"
              "       related_symbols:(i.relatedSymbols || []).map(x => x.symbol || x),"
              "       url: i.storyPath ? 'https://www.tradingview.com' + i.storyPath : null}); }"
              "  } catch (e) {}"
              "  await new Promise(r => setTimeout(r, 250)); }"
              " done(out); })().catch(fail);") % syms
        try:
            items = await self._page_fetch(js, timeout_s=60)
        except ToolError as e:
            self.limitations.append(f"Headlines unavailable ({e}).")
            return None
        atomic_write_json(cache, {"fetched_at": time.time(), "items": items})
        return items


def _rating_label(v: Optional[float]) -> Optional[str]:
    """TradingView's Recommend.All thresholds."""
    if v is None:
        return None
    if v >= 0.5:
        return "STRONG_BUY"
    if v >= 0.1:
        return "BUY"
    if v > -0.1:
        return "NEUTRAL"
    if v > -0.5:
        return "SELL"
    return "STRONG_SELL"


def data_freshness(candles: dict[str, Optional[Candles]], now: datetime, market_open: bool, max_age_min: int) -> Optional[str]:
    """Return a problem description if live data looks stale while the market is open."""
    m5 = candles.get("M5")
    if not market_open or m5 is None or not m5.t:
        return None
    age = now.timestamp() - (m5.t[-1] + TF_SECONDS["M5"])
    if age > max_age_min * 60:
        return f"Latest M5 candle is {age / 60:.0f} min old"
    return None
