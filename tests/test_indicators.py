from fxsignals import indicators as ind


def test_ema_matches_manual_recurrence():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    e = ind.ema(closes, 3)
    assert e[:2] == [None, None]
    assert e[2] == 2.0  # SMA seed
    k = 2 / 4
    assert abs(e[3] - (4 * k + 2.0 * (1 - k))) < 1e-12
    assert abs(e[5] - (6 * k + e[4] * (1 - k))) < 1e-12


def test_rsi_extremes_and_balance():
    up = [float(i) for i in range(30)]
    assert ind.last(ind.rsi(up, 14)) == 100.0
    down = [float(30 - i) for i in range(30)]
    assert ind.last(ind.rsi(down, 14)) == 0.0
    alt = [1.0 + (0.01 if i % 2 else 0.0) for i in range(60)]
    assert 40 < ind.last(ind.rsi(alt, 14)) < 60


def test_rsi_wilder_value():
    # Classic Wilder example series (first RSI(14) ~ 70.53 / 66.32 per textbook data).
    c = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    r = ind.rsi(c, 14)
    assert abs(r[14] - 70.46) < 0.2
    assert abs(r[15] - 66.25) < 0.3


def test_macd_line_is_ema_difference():
    closes = [1 + 0.01 * i + (0.005 if i % 3 == 0 else 0) for i in range(80)]
    m = ind.macd(closes)
    e12, e26 = ind.ema(closes, 12), ind.ema(closes, 26)
    assert abs(m.macd[-1] - (e12[-1] - e26[-1])) < 1e-12
    assert abs(m.hist[-1] - (m.macd[-1] - m.signal[-1])) < 1e-12
    assert m.signal[25 + 7] is None and m.signal[25 + 8] is not None


def test_atr_constant_range():
    h = [1.01] * 40
    l = [1.00] * 40
    c = [1.005] * 40
    assert abs(ind.last(ind.atr(h, l, c, 14)) - 0.01) < 1e-12


def test_adx_strong_trend_and_bounds():
    n = 120
    c = [1 + 0.002 * i for i in range(n)]
    h = [x + 0.001 for x in c]
    l = [x - 0.001 for x in c]
    a = ind.adx(h, l, c, 14)
    assert 0 <= a.adx[-1] <= 100
    assert a.adx[-1] > 40 and a.plus_di[-1] > a.minus_di[-1]


def test_bollinger_symmetry():
    closes = [1.0 + (0.01 if i % 2 else -0.01) for i in range(40)]
    b = ind.bollinger(closes, 20, 2)
    assert abs((b.upper[-1] - b.middle[-1]) - (b.middle[-1] - b.lower[-1])) < 1e-12
    assert abs(b.upper[-1] - b.middle[-1] - 0.02) < 1e-9  # population sd = 0.01


def test_vwap_resets_each_session():
    t = [0, 3600, 86400 + 22 * 3600, 86400 + 23 * 3600]
    v = ind.session_vwap(t, [2, 2, 10, 10], [2, 2, 10, 10], [2, 2, 10, 10], [1, 1, 1, 1], 22)
    assert v[1] == 2 and v[2] == 10
