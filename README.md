# Forex Signal Generator (TradingView MCP)

A disciplined multi-timeframe forex/gold signal generator. It reads market data through the
**TradingView MCP server** (`~/tradingview-mcp`) connected to your own TradingView Desktop, analyses 20 instruments on
M5/M15/H1/H4/D1, and produces ranked **BUY / SELL / WAIT** decisions with entry, stop, three targets,
position size and a transparent confidence score.

**It never executes trades.** It is an analysis tool, not financial advice. When nothing qualifies it
returns zero signals rather than forcing trades.

## How data is obtained

| Need | Source (all via the TradingView MCP server) |
|---|---|
| Connectivity | `tv_health_check` |
| Symbol identifiers | `symbol_search` → first match from `preferred_exchanges` (default OANDA, which covers all 20 incl. XAUUSD) |
| Candles (5 timeframes) | `chart_set_timeframe` + `ui_evaluate` (switches the chart and reads the loaded series together with its symbol/resolution, so a half-loaded chart is never mistaken for another instrument) |
| Live price, bid/ask, feed mode, TV technical ratings | TradingView screener endpoint, called from inside TradingView via `ui_evaluate` |
| Economic calendar | TradingView economic-calendar endpoint via `ui_evaluate` |
| Headlines | TradingView news-headlines endpoint via `ui_evaluate` |
| Indicators (EMA 9/20/50/200, RSI 14, MACD 12/26/9, ATR 14, ADX 14, BB 20/2, VWAP) | Computed locally from the candles (Wilder smoothing as in TradingView — verified to match TradingView's RSI/SMA to the displayed precision) |

The MCP server has **no dedicated tools** for technical ratings, the economic calendar, news or bid/ask; the
endpoints above are undocumented and may change. Every consumer degrades gracefully: missing calendar →
`economic_risk: null` and a reduced fundamental score; missing quotes → candle close is used and the spread
filter reports "not verified"; failed candle loads → `INSUFFICIENT_DATA` (WAIT).

**Feed status:** the screener reports `update_mode: "streaming"` for all 20 OANDA instruments, i.e. real-time.
The report's `feed.mode` field shows this per scan; `DELAYED` quotes are flagged in each signal's warnings.

> The scan drives the TradingView Desktop chart (about 100 symbol/timeframe switches, ~1.5–2 min). Your
> chart's symbol and timeframe are restored afterwards. For uninterrupted use, run scheduled scans in a
> separate TradingView window/layout or while you are not using the chart.

## Set it up on your own computer

You need about 20 minutes. Steps 1–6 are required; 7–9 are optional extras.

**What you need**

* A computer that stays on while you want scans: **Mac**, **Windows PC**, or a **Linux server/VPS**
  (steps 1–7 below are written for Mac; see "Windows" and "Linux server" further down for the differences)
* **TradingView Desktop** (free account is enough) — https://www.tradingview.com/desktop/
* **Python 3.11 or newer** — https://www.python.org/downloads/ (the python.org installer is the safest choice)
* **Node.js 18 or newer** — https://nodejs.org/
* **git** (macOS: run `xcode-select --install` once)

No paid API, broker account or API key is needed. Market data comes from your own TradingView Desktop.

### 1. Download this project

```bash
cd ~
git clone https://github.com/OwoichoDickson/forex-signal-desk.git
cd forex-signal-desk
```

### 2. Install the TradingView MCP server

This small server lets the app read charts from TradingView Desktop. It must sit in `~/tradingview-mcp`
(or change `mcp.server_path` in `config.yaml`).

```bash
cd ~
git clone https://github.com/tradesdontlie/tradingview-mcp.git
cd tradingview-mcp
npm install
cd ~/forex-signal-desk
```

### 3. Install the Python part

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

If `python3 -m venv` fails with a `pyexpat` error (some Homebrew Pythons), use the python.org one:
`/usr/local/bin/python3.13 -m venv .venv`.

### 4. Open TradingView with remote debugging

The app talks to TradingView through port 9222, which is only open when TradingView is started like this.
**Quit TradingView first** (⌘Q), then:

```bash
open -a TradingView --args --remote-debugging-port=9222
```

Opening TradingView from the Dock does *not* open the port — always use this command (or make a small
launcher app with Script Editor containing `do shell script "open -a TradingView --args --remote-debugging-port=9222"`).
Log in to TradingView and leave it open. Check it worked:

```bash
curl -s http://127.0.0.1:9222/json/version
```

(it should print a few lines of JSON).

### 5. Check everything connects

```bash
.venv/bin/python -m fxsignals health
```

It reports the MCP connection, the 20 symbols and live prices. Fix anything it flags before going on.

### 6. Make your own settings

Personal settings go in `.env` (never uploaded). Only if you set up online access (step 9):

```bash
FXSIG_ADMIN_EMAILS=you@example.com      # who may run scans / change settings online
FXSIG_PUBLIC_URL=https://forex.your-domain.com
```

Optional: in `.env` set `FXSIG_ACCOUNT_CURRENCY` and `FXSIG_RISK_PER_TRADE_PCT`. The amount you trade with is
typed into the dashboard later; nothing is read from a broker.

Now try one scan and the dashboard:

```bash
.venv/bin/python -m fxsignals scan --force     # takes ~2 minutes; it drives the TradingView chart
.venv/bin/python -m fxsignals dashboard        # then open http://127.0.0.1:8765
```

During a scan the app switches the TradingView chart through every pair and timeframe and puts it back
afterwards — don't use the chart while it runs.

### 7. Telegram alerts (optional, recommended)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts → copy the **token** it gives you.
2. Put it in `.env`: `TELEGRAM_BOT_TOKEN=<your token>` (never share or commit this file — it is in `.gitignore`).
3. Open your new bot in Telegram and press **Start**.
4. Link it: `.venv/bin/python -m fxsignals telegram-setup` — you get a test message.

You (the chat that ran `telegram-setup`) are the **owner**. Anyone else who presses Start gets a request
that you approve or deny in Telegram; approved followers receive signals without lot sizes. Bot commands:
`/open`, `/check`, `/hold`, `/archive`, `/mytrades`, and for the owner `/users`, `/remove`.

### 8. Run it in the background (Mac)

```bash
scripts/service.sh install     # scanner every 10 min + dashboard + Telegram bot; start at login, restart on crash
scripts/service.sh status
scripts/service.sh logs
scripts/service.sh uninstall
```

Things to keep in mind:

* TradingView must stay open **with port 9222** (step 4). After a restart, open it with that command again.
* A sleeping Mac does not scan. The scanner blocks idle sleep while it runs (`scan: keep_awake`, on by default),
  but closing the lid on battery still puts the Mac to sleep: keep it open during trading hours, or plug in
  with an external display.
* Scans run Mon–Fri while the market is open (`scan:` section of `config.yaml`).
* After changing code or `config.yaml`, restart a service, e.g.
  `launchctl kickstart -k gui/$(id -u)/com.fxsignals.scanner`.

### 9. See the dashboard from your phone / share it (optional)

The dashboard only listens on your own computer. To reach it from anywhere, use a free **Cloudflare Tunnel**
(needs a domain on Cloudflare):

1. `brew install cloudflared`, then `cloudflared tunnel login`
2. `cloudflared tunnel create fxsignals` and `cloudflared tunnel route dns fxsignals forex.<your-domain>`
3. Create `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: fxsignals
   credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json
   protocol: http2
   ingress:
     - hostname: forex.<your-domain>
       service: http://127.0.0.1:8765
     - service: http_status:404
   ```
4. Run `scripts/service.sh install` again — it adds the tunnel as a background service.
5. **Protect it:** Cloudflare Zero Trust → Access → Applications → add a self-hosted app for
   `forex.<your-domain>` with a policy listing the allowed emails, and enable the **One-time PIN** login method
   (Settings → Authentication). Put your email in `FXSIG_ADMIN_EMAILS` and the link in `FXSIG_PUBLIC_URL` in `.env` (step 6).

Some networks (schools, offices) block the tunnel's port 7844; the dashboard is then unreachable from
outside until the computer is on another network. Telegram keeps working.

### Which computer should run it?

| | Mac | Windows PC | Linux server / VPS |
|---|---|---|---|
| Difficulty | Easiest (this guide) | Easy | Advanced |
| Runs in background at login | `scripts/service.sh` | Task Scheduler (below) | systemd (below) |
| On 24/7 | Only while awake & plugged in | Only while awake | Yes |
| Cost | Free | Free | ~5–20 USD/month for the server |

Whichever you choose, **TradingView Desktop must run on that same machine** (the app reads the chart through
port 9222 on `127.0.0.1`). Each person needs their **own** Telegram bot token — one token can only be used by
one running bot at a time.

### Windows

1. Install Python (tick **"Add python.exe to PATH"**), Node.js, Git for Windows and TradingView Desktop.
2. In **PowerShell**:
   ```powershell
   cd $HOME
   git clone https://github.com/OwoichoDickson/forex-signal-desk.git
   git clone https://github.com/tradesdontlie/tradingview-mcp.git
   cd tradingview-mcp; npm install; cd ..\forex-signal-desk
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   copy .env.example .env
   ```
3. Start TradingView with the debugging port: quit it, then right-click its shortcut → **Properties** →
   **Target**, add ` --remote-debugging-port=9222` after the closing quote, **OK**, and open it from that
   shortcut. Check: open http://127.0.0.1:9222/json/version in a browser.
4. Steps 5–7 are the same, with `.venv\Scripts\python` instead of `.venv/bin/python`.
5. Keep it running: open three PowerShell windows in the project folder and run one command in each:
   ```powershell
   .venv\Scripts\python -m fxsignals run
   .venv\Scripts\python -m fxsignals dashboard
   .venv\Scripts\python -m fxsignals telegram-bot
   ```
   To start them automatically, add each as a **Task Scheduler** task ("At log on", program
   `<project>\.venv\Scripts\python.exe`, arguments `-m fxsignals run` etc., "Start in" = the project folder).
6. Settings → System → Power: set **Sleep** to *Never* while plugged in.
7. Online access (step 9) works the same with the Windows build of `cloudflared`
   (`winget install Cloudflare.cloudflared`, then `cloudflared service install` with your tunnel).

### Linux server / VPS (runs 24/7)

For people comfortable with a terminal. Use an Ubuntu 22.04/24.04 server with **at least 4 GB RAM**
(TradingView Desktop is an Electron app and needs memory). TradingView needs a screen, so the server gets a
virtual one.

```bash
# as a normal user with sudo
sudo apt update && sudo apt install -y git python3-venv nodejs npm xvfb x11vnc fluxbox
sudo snap install tradingview
cd ~ && git clone https://github.com/tradesdontlie/tradingview-mcp.git && (cd tradingview-mcp && npm install)
git clone https://github.com/OwoichoDickson/forex-signal-desk.git && cd forex-signal-desk
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt && cp .env.example .env
```

(Node must be 18+: `node -v`; if the Ubuntu one is older, install from https://nodejs.org/ or NodeSource.)

Start a virtual screen and TradingView, then log in to TradingView **once** through VNC:

```bash
Xvfb :1 -screen 0 1600x900x24 &
DISPLAY=:1 fluxbox &
DISPLAY=:1 tradingview --remote-debugging-port=9222 &
x11vnc -display :1 -localhost -forever -nopw &
```

From your own computer: `ssh -L 5900:localhost:5900 <you>@<server>` and connect a VNC viewer to
`localhost:5900`; log in to TradingView there, then close the viewer (TradingView keeps running). VNC only
listens on localhost, so it is reached through SSH, never openly on the internet.

Then follow steps 5–7. To keep everything running after logout and reboots, create systemd services — one
per command. Example for the scanner, `/etc/systemd/system/fxsignals-scanner.service`:

```ini
[Unit]
Description=fxsignals scanner
After=network-online.target

[Service]
User=<you>
WorkingDirectory=/home/<you>/forex-signal-desk
Environment=DISPLAY=:1
ExecStart=/home/<you>/forex-signal-desk/.venv/bin/python -u -m fxsignals run
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Make the same file for `dashboard` and `telegram-bot` (change the name and the last word of `ExecStart`), and
similar ones for `Xvfb :1 -screen 0 1600x900x24` and `tradingview --remote-debugging-port=9222`
(`Environment=DISPLAY=:1`, `After=` the Xvfb service). Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fxsignals-scanner fxsignals-dashboard fxsignals-telegram
journalctl -u fxsignals-scanner -f      # watch the logs
```

The dashboard stays on `127.0.0.1:8765` on the server. View it with `ssh -L 8765:127.0.0.1:8765 <you>@<server>`
and http://127.0.0.1:8765, or put it online with the Cloudflare Tunnel from step 9 (`cloudflared` has Linux
packages; run `sudo cloudflared service install` instead of `scripts/service.sh`). Never open port 8765 or 9222
in the server's firewall — anyone reaching 9222 could control your TradingView.

### Troubleshooting

| Problem | Fix |
|---|---|
| `health` / dashboard says TradingView port 9222 not reachable | Quit TradingView and reopen it with the command in step 4 |
| `TradingView MCP server not found` | Step 2 not done, or `mcp.server_path` in `config.yaml` points elsewhere |
| Dashboard shows "Scheduler not running" | The Mac slept or the scanner service stopped — `scripts/service.sh status`, then `logs` |
| "Another scan is already running" | Wait ~2 minutes for it to finish |
| No Telegram messages | Check `TELEGRAM_BOT_TOKEN` in `.env`, press Start in the bot, rerun `telegram-setup` |
| Every signal says BLOCKED | Your risk limits in the dashboard planner don't allow it (e.g. risk % above the daily loss limit) — the reason is shown under the signal |
| Online link down, local works | Tunnel blocked by the network, or `cloudflared` not running (`scripts/service.sh status`) |

## Commands

```bash
.venv/bin/python -m fxsignals health          # verify MCP connectivity, symbols and live quotes
.venv/bin/python -m fxsignals scan            # one scan (respects market-closed + min-interval guards)
.venv/bin/python -m fxsignals scan --force    # scan regardless of guards (e.g. weekend planning)
.venv/bin/python -m fxsignals scan --pairs EURUSD USDJPY XAUUSD
.venv/bin/python -m fxsignals show            # print the latest report (statuses re-evaluated now)
.venv/bin/python -m fxsignals run             # scan every scan.interval_minutes (default 10)
.venv/bin/python -m fxsignals dashboard       # http://127.0.0.1:8765
.venv/bin/python -m fxsignals backtest --pairs EURUSD GBPUSD USDJPY
.venv/bin/python -m fxsignals telegram-setup  # link Telegram and send a test message
.venv/bin/python -m fxsignals telegram-bot    # listen for button taps and bot commands
```

Nothing secret is written to logs or reports (log records also pass through a redaction filter).

## Features

### Market outlook (a recommendation for every pair)

Trade signals stay strict, so many scans return none. Every pair still gets an **outlook** (terminal, JSON
`outlook[]`, dashboard): a weighted multi-timeframe lean (D1 20 % · H4 30 % · H1 30 % · M15 12 % · M5 8 %),
TradingView's own rating per timeframe as a second opinion (disagreement is flagged), the exact reason there
is no trade now, the concrete condition that would change that (e.g. "H1 RSI (53.6) to move below 50"), and
alert levels taken from real support/resistance. A **currency-strength** table (average % move of each
currency against the others over ~1 day / ~1 week) highlights strong-vs-weak pairs, and says whether the
pair's own chart agrees. A lean is a view, not a trade.

### Position-size planner (optional)

The dashboard has an optional **Amount** box. Type any amount ("if I trade with 100 USD") and every
signal shows the recommended lot size and money at risk for that amount; leave it empty and signals show
without lot sizes. Nothing is read from TradingView or a broker. The limits (risk % per trade, max open
trades, max total risk, daily loss limit, currency-exposure cap) apply either way, and each signal shows
a **risk status** (ALLOWED / ALLOWED WHEN ACTIVE / BLOCKED with the reason). If an amount is too small for
a signal's stop even at the 0.01-lot minimum, the signal is marked "too small — skip" instead of being
oversized.

### Track record

Every published signal is followed on 5-minute candles: fill (pending orders must actually be touched),
then stop vs TP2 — whichever is touched first (a bar touching both counts as a stop). Results (wins, losses,
average R, TP1 touch rate) appear in the report's `track_record` and the dashboard. Under 30 closed trades
it is labelled "not yet meaningful". This is how you judge whether the generator is any good — not the
confidence score.

### Every change on Telegram, and your own trades

Each open signal is re-checked on every scan, and **every change is sent once** to Telegram, taken or not:
new signal · entry reached · going as planned (+1R) · going against (−0.5R) · back on track · TP1 reached
(move stop to break-even) · structure/news warnings on running trades · latest scan no longer confirms ·
cancelled (H4 trend flip, H1 change of character against a breakout setup, high-impact news) · replaced ·
expired without entry · final win/loss. Running trades are never closed by the app — the stop is the plan.

Signal messages carry **✅ I took it / ❌ Skipped** buttons (asked again when a limit/stop order's entry is
reached). The `com.fxsignals.telegram` service records taps in `state/taken.json`; `/open` and `/mytrades` in
the bot list open signals and your own results. The dashboard shows the same answers ("Did you take it?") and a
"Your own trades" summary; the owner can also set them there.

### Signal results

The dashboard's **Signal results** section lists every signal ever generated with its outcome — wins (TP2 hit)
in green, losses (stop hit) in red, plus running / awaiting fill / not filled — and totals: signals generated,
wins, losses, win rate and total R. Filter by result or pair.

## Output

Each scan writes `./forex_signals_YYYY-MM-DD.json` (latest scan of the day) and archives a copy in
`state/scans/`. The top-level schema follows the specification (`scanned_at`, `data_source`,
`market_status`, `signals[]`, `watchlist[]`, `market_summary`). Each signal carries all specified fields plus:
`signal_id`, `created_at`, `expires_at`, `actionable`, `live` (last/bid/ask/day high/low/previous close/feed),
`position_size`, `portfolio` (allocation under the risk limits) and `details` (score breakdown,
R:R per target, stop distance in pips, spread, per-timeframe technicals, trigger evidence, warnings).
Unavailable values are `null`.

`status` values: `ACTIVE` (market entry, market open), `PENDING` (limit/stop order awaiting price),
`MARKET_CLOSED` (planning only), `PENDING_REVALIDATION`, `EXPIRED`, `INVALIDATED` (stop crossed),
`TARGET_HIT`, `SUPERSEDED`. Status is recomputed whenever a report is shown, so an expired signal is never
presented as active. `actionable` is true only for an `ACTIVE` signal that fits the portfolio limits.

## Decision logic (summary)

1. **Direction** from H1 + H4 bias (EMA20/50/200 stack, price vs EMA50, MACD, swing structure). Opposing
   H1/H4 → `CONFLICT`; no trend → `NO_TREND`.
2. **Mandatory filters (H1):** EMA20 vs EMA50, RSI between 50 and 70 (buy) / 30 and 50 (sell), MACD support.
3. **News:** a high-impact event for *either* currency within 60 min ahead (or 15 min after) → `NEWS_BLACKOUT`.
4. **Order blocks (SMC):** a bullish order block is the last bearish candle before the impulse (≥ 1 ATR) that
   produced a BOS/CHoCH up (mirror for bearish). Blocks are tracked as fresh / tapped and dropped once a candle
   closes through them. An M15/H1 BOS or CHoCH after price taps a same-direction H1/H4 order block is the
   top-scoring entry (`*_AT_ORDER_BLOCK`), with the stop beyond the block; otherwise a fresh block is the
   preferred `BUY/SELL_LIMIT` level. Opposing blocks are used as targets. The dashboard chart draws blocks and
   BOS/CHoCH markers.
5. **Entry:** `MARKET_ENTRY` needs a confirmed trigger on M15/H1 (BOS/CHOCH, breakout-retest, liquidity sweep,
   or a rejection candle *at* a level) and price within 1.5 ATR of H1 EMA20. Otherwise a `BUY/SELL_STOP` above
   a consolidation, or a `BUY/SELL_LIMIT` at a support/demand (resistance/supply) level; else `NO_TRIGGER`.
5. **Stop:** beyond the nearest structural swing/zone + 0.25 ATR buffer; must be 0.5–3 ATR(H1) away, otherwise
   `STOP_TOO_WIDE` (stops are never tightened arbitrarily).
6. **Targets:** TP1 = 2R, TP2 = first structural level ≥ 3R (or 3R), TP3 = next level (or TP2 + 1R). A strong
   level between 0.3R and 2R → `POOR_RR`.
7. **Spread:** above the per-pair limit or > 15 % of the stop distance → `HIGH_SPREAD`.
8. **Confidence (0–100):** HTF alignment 25 · indicators 20 · structure/price action 25 · momentum 15 ·
   fundamentals 15. Scores above 74 require real structure confirmation (≥ 15/25) and a non-opposing daily
   trend, so correlated indicators alone cannot create a "strong" rating. < 65 → WAIT. **This score is a
   heuristic, not a probability of profit.**
9. **Ranking:** confidence → HTF alignment → R:R → structure → fundamentals; top 10, never padded.
10. **Risk:** lots = balance × risk% ÷ (stop pips × pip value), with pip value converted to the account
    currency from live rates (JPY pairs 0.01 pip, XAUUSD 0.1 pip × 100 oz). Portfolio limits: max 3 positions,
    max daily loss (set `daily_loss_used_pct`), and max risk stacked on one currency in one direction.

Fundamentals use only measurable data: scheduled events and actual-vs-forecast surprises (higher-than-forecast
is currency-positive, inverted for unemployment/jobless claims). Headlines are attached for context but are
**not** sentiment-scored, and no narrative is invented.

## Tests

```bash
.venv/bin/python -m pytest              # offline: indicators, structure, BUY/SELL/WAIT rules, R:R, JPY/XAU pips,
                                        # position sizing, missing data, market closure, news blackout, expiry
.venv/bin/python -m pytest --live       # live: MCP connectivity, real-time feed, candle/quote consistency,
                                        # RSI vs TradingView's own indicator, calendar & headlines
```

## Backtesting

`backtest` replays the technical engine walk-forward on the H1/H4/D1 history the chart holds (~300 bars,
about two weeks of H1), with spread + slippage costs and conservative fills (stop assumed first when a bar
touches both). Lower timeframes and the historical calendar are not available through the MCP, and the
sample is far too small for conclusions — the output says so explicitly. **No profitability is claimed.**

## Project layout

```
fxsignals/
  config.py          configuration (config.yaml + .env)
  mcp_client.py      MCP stdio client: rate limiting, retries, reconnect
  market_data.py     TradingView data retrieval (symbols, candles, quotes, calendar, news)
  indicators.py      technical indicators
  structure.py       market structure (swings, BOS/CHOCH, S/R, zones, sweeps, rejections, ranges)
  fundamentals.py    calendar / surprise / headline analysis per currency
  signals.py         multi-timeframe decision, levels, confidence score
  risk.py            position sizing and portfolio limits
  ranking.py         signal ranking
  history.py         duplicate prevention, expiry, invalidation, history
  filelock.py        cross-platform file locks (Mac/Linux/Windows)
  subscribers.py     Telegram followers (owner approves each one)
  report.py          JSON report + terminal output
  scanner.py         scan orchestration, lock, guards
  backtest.py        walk-forward replay
  dashboard/         local web dashboard (stdlib HTTP server + static page)
  cli.py             commands
tests/               pytest suite (offline + --live)
config.yaml          all tunable parameters
```
