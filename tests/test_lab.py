"""Strategy lab replay: filters and trade management are applied exactly as the rule set says."""
from fxsignals.backtest import DEFAULT_COSTS
from fxsignals.lab import replay
from fxsignals.models import Candles

NOCOST = {**DEFAULT_COSTS, "spread_pips": 0.0, "slippage_pips": 0.0}
RULES = dict(hours=None, cooldown_h=0, breakeven=False, d1=None)
T0 = 1_790_000_000 - 1_790_000_000 % 86400  # midnight UTC


def h1(rows):
    return Candles("EURUSD", "H1", [T0 + i * 3600 for i in range(len(rows))],
                   [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [0] * len(rows))


def sig(i, **kw):
    return {"i": i, "t": T0 + i * 3600 + 3600, "hour": (i + 1) % 24, "direction": "BUY", "type": "MARKET_ENTRY",
            "setup": "X", "score": 80, "entry": 1.1, "sl": 1.098, "tp1": 1.104, "tp2": 1.106, "adx": 25, "ext_atr": 0.5,
            "d1_agree": 2, **kw}


FLAT = (1.1, 1.1005, 1.0995, 1.1)


def test_tp1_then_back_is_breakeven_only_with_the_rule():
    bars = h1([FLAT, FLAT, (1.1, 1.1045, 1.0999, 1.104), (1.104, 1.1041, 1.0975, 1.098)] + [FLAT] * 3)
    assert replay("EURUSD", bars, [sig(0)], {**RULES, "breakeven": True}, NOCOST)[0]["outcome"] == "BREAKEVEN"
    assert replay("EURUSD", bars, [sig(0)], RULES, NOCOST)[0]["outcome"] == "STOP"


def test_one_trade_at_a_time_hours_and_cooldown():
    stop = (1.1, 1.1, 1.097, 1.098)
    bars = h1([FLAT, FLAT, stop, FLAT, FLAT, FLAT, FLAT, FLAT, FLAT, FLAT])
    sigs = [sig(0), sig(1), sig(4), sig(8)]
    tr = replay("EURUSD", bars, sigs, RULES, NOCOST)
    assert [t["t"] for t in tr][:2] == [sigs[0]["t"], sigs[2]["t"]]  # sig(1) skipped: trade still running
    tr = replay("EURUSD", bars, sigs, {**RULES, "cooldown_h": 4}, NOCOST)
    assert sigs[2]["t"] not in [t["t"] for t in tr]  # within 4h of the stop-out
    tr = replay("EURUSD", bars, [sig(0, hour=3)], {**RULES, "hours": (7, 16)}, NOCOST)
    assert tr == []


def test_efficiency_ratio_is_one_for_a_straight_line_and_low_for_chop():
    from fxsignals.lab import efficiency
    assert efficiency([1 + k * 0.001 for k in range(60)], 59) == 1.0
    assert efficiency([1 + (k % 2) * 0.001 for k in range(60)], 59) == 0.0


def test_london_sweep_of_the_asian_high_gives_a_sell():
    from datetime import datetime, timezone
    from fxsignals.lab import _range_targets, asian_sweep_signals
    day = int(datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp())
    n = 220
    t = [day - (210 - k) * 3600 for k in range(n)]           # bar 210 opens at 00:00 UTC
    c = [1.1000] * n
    h, l = [x + 0.0010 for x in c], [x - 0.0010 for x in c]
    h[217] = 1.1030                                          # 07:00 bar pokes above the Asian high, closes back inside
    sigs = asian_sweep_signals("EURUSD", Candles("EURUSD", "H1", t, c, h, l, c, [1] * n), {"analysis": {"sl_buffer_atr": 0.25}})
    assert [(s["i"], s["direction"]) for s in sigs] == [(217, "SELL")]
    s = _range_targets(sigs)[0]
    assert s["sl"] > 1.1030 and s["tp2"] < s["entry"]


def test_half_off_at_tp1_banks_profit_before_break_even():
    bars = h1([FLAT, FLAT, (1.1, 1.1045, 1.0999, 1.104), (1.104, 1.1041, 1.0975, 1.098)] + [FLAT] * 3)
    t = replay("EURUSD", bars, [sig(0)], {**RULES, "breakeven": True, "partial": 0.5}, NOCOST)[0]
    assert t["outcome"] == "BREAKEVEN" and t["r"] == 1.0  # half at TP1 (2R), half at entry


def test_trailing_stop_follows_the_best_price_after_tp1():
    bars = h1([FLAT, FLAT, (1.1, 1.105, 1.0999, 1.105), (1.105, 1.1052, 1.1025, 1.103)] + [FLAT] * 3)
    t = replay("EURUSD", bars, [sig(0)], {**RULES, "trail_r": 1.0}, NOCOST)[0]
    assert t["outcome"] == "TRAIL" and t["r"] == 1.5  # best 1.105 - 1R = 1.103
    t = replay("EURUSD", bars, [sig(0)], {**RULES, "max_hold": 2}, NOCOST)[0]
    assert t["outcome"] == "TIME_EXIT"
