from fxsignals import indicators as ind
from fxsignals import structure as st
from fxsignals.models import Candles


def _c(rows, tf="H1"):
    return Candles.from_rows("EURUSD", tf, [{"time": 1_790_000_000 + i * 3600, "open": o, "high": h, "low": l, "close": c, "volume": 1}
                                           for i, (o, h, l, c) in enumerate(rows)])


def test_uptrend_gives_hh_hl(helpers):
    c = helpers["make_candles"](helpers["zigzag"](120))
    swings = st.find_swings(c, 2)
    trend, labels = st.classify_trend(swings)
    assert trend == "BULLISH" and labels == ["HH", "HL"]


def test_downtrend_gives_lh_ll(helpers):
    closes = [2.2 - (x - 1.1) for x in helpers["zigzag"](120)]
    c = helpers["make_candles"](closes)
    trend, labels = st.classify_trend(st.find_swings(c, 2))
    assert trend == "BEARISH" and labels == ["LH", "LL"]


def test_bos_and_choch_detection(helpers):
    up = helpers["zigzag"](60)
    down = [up[-1] - 0.0015 * i for i in range(1, 25)]
    c = helpers["make_candles"](up + down)
    ev = st.bos_choch_events(c, st.find_swings(c, 2), 2)
    kinds = [(e.kind, e.direction) for e in ev]
    assert ("BOS", "BULLISH") in kinds
    assert ("CHOCH", "BEARISH") in kinds  # first bearish break after a bullish state
    choch = next(e for e in ev if e.kind == "CHOCH")
    assert c.c[len(c) - 1 - choch.bars_ago] < choch.price  # claim is backed by the candle close


def test_liquidity_sweep_and_pin_bar():
    rows = [(1.10, 1.101, 1.099, 1.100)] * 5 + [(1.100, 1.105, 1.099, 1.104), (1.104, 1.106, 1.103, 1.104)] + \
           [(1.104, 1.104, 1.101, 1.102)] * 4 + [(1.102, 1.1065, 1.1015, 1.1030)]
    c = _c(rows)
    swings = st.find_swings(c, 2)
    assert any(s.kind == "H" and abs(s.price - 1.106) < 1e-9 for s in swings)
    sweeps = st.liquidity_sweeps(c, swings)
    assert any(e.direction == "BEARISH" and abs(e.price - 1.106) < 1e-9 for e in sweeps)
    pins = st.rejection_candles(c)
    assert any(e.direction == "BEARISH" and "pin" in e.detail for e in pins)


def test_bullish_engulfing():
    c = _c([(1.10, 1.101, 1.099, 1.100), (1.102, 1.1025, 1.0995, 1.100), (1.0998, 1.1035, 1.0996, 1.103)])
    ev = st.rejection_candles(c)
    assert any(e.direction == "BULLISH" and e.detail == "bullish engulfing" for e in ev)


def test_consolidation_flag():
    rows = [(1.1, 1.1004, 1.0996, 1.1 + (0.0002 if i % 2 else -0.0002)) for i in range(60)]
    c = _c(rows)
    a = ind.atr(c.h, c.l, c.c, 14)
    rep = st.analyze(c, a, adx_value=12.0, width=2)
    assert rep.consolidating and rep.range_high >= rep.range_low


def test_demand_zone_from_impulse():
    rows = [(1.1, 1.1005, 1.0995, 1.1)] * 20 + [(1.1, 1.1003, 1.0998, 1.1001), (1.1001, 1.1080, 1.1000, 1.1078)] + \
           [(1.1078, 1.1085, 1.1070, 1.1080)] * 5
    c = _c(rows)
    a = ind.atr(c.h, c.l, c.c, 14)
    zones = st.supply_demand_zones(c, a)
    assert any(z.kind == "DEMAND" and z.low == 1.0998 and z.high == 1.1003 for z in zones)
