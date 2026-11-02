from fxsignals import structure as st
from fxsignals.models import Candles
from fxsignals.signals import Quote, decide
from fxsignals.fundamentals import PairFundamentals

BASE = [
    (1.1000, 1.1005, 1.0995, 1.1000),
    (1.1000, 1.1010, 1.0998, 1.1008),
    (1.1008, 1.1030, 1.1005, 1.1025),  # swing high 1.1030
    (1.1025, 1.1026, 1.1010, 1.1012),
    (1.1012, 1.1015, 1.0995, 1.0998),
    (1.0998, 1.1000, 1.0980, 1.0985),  # last bearish candle at the leg low -> bullish order block
    (1.0985, 1.1010, 1.0984, 1.1008),
    (1.1008, 1.1028, 1.1005, 1.1026),
    (1.1026, 1.1050, 1.1024, 1.1048),  # closes above 1.1030 -> BOS
    (1.1048, 1.1060, 1.1045, 1.1058),
    (1.1058, 1.1065, 1.1050, 1.1062),
]
ATR = 0.0020


def _c(rows):
    return Candles("EURUSD", "H1", [1_790_000_000 + i * 3600 for i in range(len(rows))],
                   [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [1] * len(rows))


def _obs(rows):
    c = _c(rows)
    ev = st.bos_choch_events(c, st.find_swings(c, 2), 2)
    return ev, st.order_blocks(c, ev, ATR)


def test_bullish_order_block_is_last_bearish_candle_before_break():
    ev, obs = _obs(BASE)
    bos = next(e for e in ev if e.kind == "BOS" and e.direction == "BULLISH")
    assert bos.idx == 8 and bos.ref_idx == 2
    assert len(obs) == 1
    ob = obs[0]
    assert ob.kind == "BULLISH" and ob.low == 1.0980 and ob.high == 1.1000 and ob.t == _c(BASE).t[5]
    assert ob.fresh and not ob.price_inside and ob.displacement_atr >= 3


def test_order_block_mitigation_then_invalidation():
    tapped = BASE + [(1.1062, 1.1063, 1.0996, 1.1004)]  # returns into the block and holds
    _, obs = _obs(tapped)
    assert obs and not obs[0].fresh and obs[0].price_inside
    broken = tapped + [(1.1004, 1.1005, 1.0960, 1.0970)]  # closes below the block
    _, obs = _obs(broken)
    assert all(o.low != 1.0980 for o in obs)


def test_weak_displacement_is_not_an_order_block():
    c = _c(BASE)
    ev = st.bos_choch_events(c, st.find_swings(c, 2), 2)
    assert st.order_blocks(c, ev, atr_value=0.0100) == []  # move is < 1 ATR when ATR is large


def test_bearish_order_block_mirror():
    mirror = [(2.2 - o, 2.2 - l, 2.2 - h, 2.2 - cl) for o, h, l, cl in BASE]
    _, obs = _obs(mirror)
    assert len(obs) == 1 and obs[0].kind == "BEARISH"
    assert abs(obs[0].low - (2.2 - 1.1000)) < 1e-9 and abs(obs[0].high - (2.2 - 1.0980)) < 1e-9


# ── signal integration ────────────────────────────────────────────────────────
T = 1_790_000_000
Q = Quote(last=1.1000, bid=1.09995, ask=1.10005, update_mode="streaming")
NOFUND = PairFundamentals(available=True)


def _setup(helpers, ob, trigger=True):
    tfa = helpers["tfa"]
    events = [st.Event("CHOCH", "BULLISH", 1.0996, T, 2, "close above swing high")] if trigger else []
    h1 = tfa("H1", 1.1000, "BULLISH", atr=0.0010, swings=[st.Swing(280, T, 1.0960, "L")])
    h1.structure.order_blocks = [ob]
    h1.last_t = T + 3600 * 3
    return {"M15": tfa("M15", 1.1, "BULLISH", atr=0.0004, events=events), "H1": h1,
            "H4": tfa("H4", 1.1, "BULLISH", atr=0.0025), "D1": tfa("D1", 1.1, "BULLISH", atr=0.006)}


def test_choch_at_tapped_order_block_is_top_quality_entry(cfg, helpers):
    ob = st.OrderBlock("BULLISH", 1.0985, 1.0995, T, "BOS", T + 3600, 2.5, mitigated=True, mitigated_t=T + 3600 * 2)
    r = decide("EURUSD", _setup(helpers, ob), Q, NOFUND, cfg, True)
    assert r["direction"] == "BUY" and r["signal_type"] == "MARKET_ENTRY", r.get("reason")
    assert r["market_structure"] == "BULLISH_CHOCH_AT_ORDER_BLOCK"
    assert r["stop_loss"] == round(1.0985 - 0.25 * 0.0010, 5)  # beyond the order block, not the far swing
    assert r["details"]["order_block"]["low"] == 1.0985 and r["details"]["order_block"]["timeframe"] == "H1"
    assert r["details"]["score_breakdown"]["market_structure"] == 25


def test_pending_limit_at_fresh_order_block(cfg, helpers):
    ob = st.OrderBlock("BULLISH", 1.0982, 1.0990, T, "CHOCH", T + 3600, 2.0, mitigated=False)
    r = decide("EURUSD", _setup(helpers, ob, trigger=False), Q, NOFUND, cfg, True)
    assert r["direction"] == "BUY" and r["signal_type"] == "BUY_LIMIT", r.get("reason")
    assert r["entry_price"] == 1.099 and r["market_structure"] == "BULLISH_PULLBACK_TO_ORDER_BLOCK"
    assert r["stop_loss"] == round(1.0982 - 0.25 * 0.0010, 5)
    assert r["status"] == "PENDING"


def test_stale_tap_is_not_used(cfg, helpers):
    ob = st.OrderBlock("BULLISH", 1.0985, 1.0995, T, "BOS", T + 3600, 2.5, mitigated=True, mitigated_t=T - 86400)
    r = decide("EURUSD", _setup(helpers, ob), Q, NOFUND, cfg, True)
    assert not r.get("market_structure", "").endswith("AT_ORDER_BLOCK")
