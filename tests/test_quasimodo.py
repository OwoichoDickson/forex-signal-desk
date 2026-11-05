from fxsignals.models import Candles
from fxsignals.structure import find_quasimodo, find_swings


def _path(points, steps=5):
    """Straight-line closes through the given turning points; highs/lows 5 pips either side."""
    cl = [points[0]]
    for a, b in zip(points, points[1:]):
        cl += [a + (b - a) * k / steps for k in range(1, steps + 1)]
    n = len(cl)
    return Candles("EURUSD", "H1", [i * 3600 for i in range(n)], cl, [x + 0.0005 for x in cl], [x - 0.0005 for x in cl], cl, [1] * n)


def test_bearish_quasimodo_found_after_the_break_and_gone_once_price_returns():
    # rise to the left shoulder 1.100, dip to 1.090, higher head 1.120, then close below 1.090
    c = _path([1.080, 1.100, 1.090, 1.120, 1.085, 1.087, 1.086])
    qm = find_quasimodo(c, find_swings(c, 3), 3)
    assert len(qm) == 1 and qm[0].direction == "BEARISH"
    assert abs(qm[0].qml - 1.1005) < 1e-9 and abs(qm[0].head - 1.1205) < 1e-9
    back = _path([1.080, 1.100, 1.090, 1.120, 1.085, 1.087, 1.101])  # returned to the QML: pattern used up
    assert not [q for q in find_quasimodo(back, find_swings(back, 3), 3) if q.direction == "BEARISH"]


def test_bullish_quasimodo_is_the_mirror():
    c = _path([1.120, 1.100, 1.110, 1.080, 1.115, 1.113, 1.114])
    qm = find_quasimodo(c, find_swings(c, 3), 3)
    assert [q.direction for q in qm] == ["BULLISH"] and abs(qm[0].qml - 1.0995) < 1e-9
