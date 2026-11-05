from datetime import datetime, timezone

from fxsignals import qm_shadow
from fxsignals.models import Candles

CFG = {"analysis": {"swing_width": {"H1": 3}, "sl_buffer_atr": 0.25, "min_sl_atr": 0.5, "max_sl_atr": 3.0}}
T0 = int(datetime(2026, 11, 2, 0, tzinfo=timezone.utc).timestamp())


def _h1(points, steps=5, wick=0.004, flat=110):
    cl = [points[0]] * flat
    for a, b in zip(points, points[1:]):
        cl += [a + (b - a) * k / steps for k in range(1, steps + 1)]
    n = len(cl)
    return Candles("EURUSD", "H1", [T0 + i * 3600 for i in range(n)], cl, [x + wick for x in cl], [x - wick for x in cl], cl, [1] * n)


def _h4(h1):
    return Candles("EURUSD", "H4", h1.t[::4], h1.c[::4], h1.h[::4], h1.l[::4], h1.c[::4], [1] * len(h1.t[::4]))


def test_pattern_is_recorded_once_then_settled_with_costs(tmp_path):
    pts = [1.080, 1.100, 1.090, 1.120, 1.085]
    c = _h1(pts)
    now = datetime.fromtimestamp(c.t[-1] + 3600, timezone.utc)
    recs = qm_shadow.update(tmp_path, {"EURUSD": c}, {"EURUSD": _h4(c)}, CFG, now)
    assert len(recs) == 1 and recs[0]["direction"] == "SELL" and recs[0]["results"] == {}
    qm_shadow.update(tmp_path, {"EURUSD": c}, {"EURUSD": _h4(c)}, CFG, now)
    assert len(qm_shadow.load(tmp_path)) == 1  # not recorded twice
    # price comes back to the QM level, turns, and falls far: both target sets win
    c2 = _h1(pts + [1.1005, 1.040], steps=5)
    recs = qm_shadow.update(tmp_path, {"EURUSD": c2}, {"EURUSD": _h4(c2)}, CFG, now)
    res = recs[0]["results"]
    assert res["2R/3R"]["outcome"] == "TARGET" and res["1R/2R"]["outcome"] == "TARGET"
    assert 1.5 < res["1R/2R"]["r"] < 2.0  # 2R less spread and slippage
    assert recs[0]["done"]
    s = qm_shadow.summary(recs, hours=None)
    assert s["2R/3R"]["trades"] == 1 and s["patterns_recorded"] == 1
