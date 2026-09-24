import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from pawl import strategy as st
from pawl import floor as fl
from pawl import risk as R
from pawl import backtest as bt

CFG = {
    "meta": {"name": "PAWL", "mode": "paper"},
    "universe": {"core": ["BTC/USD", "ETH/USD"], "rotation": ["SOL/USD"],
                 "min_median_dollar_vol_30d": 1_000},
    "regime": {"anchor": "BTC/USD", "sma_days": 50, "asset_sma_days": 20},
    "momentum": {"lookback_days": 14, "entry_threshold": 0.05, "exit_threshold": -0.02,
                 "max_positions": 2, "rank_by": "risk_adjusted"},
    "sizing": {"target_vol_annual": 0.35, "vol_lookback_days": 14,
               "max_weight_per_position": 0.35, "max_total_invested": 0.85,
               "min_notional_usd": 250, "drift_band_pct_of_equity": 0.05,
               "drift_band_pct_of_target": 0.25},
    "floor": {"atr_days": 14, "atr_multiple": 3.0, "limit_offset_pct": 0.01, "ratchet_hours": 4},
    "risk": {"daily_loss_halt_pct": 0.06, "drawdown_kill_pct": 0.25, "max_bar_age_minutes": 120,
             "max_consecutive_order_failures": 2, "monthly_roundtrip_budget": 12},
    "costs": {"taker_fee": 0.0025, "maker_fee": 0.0015, "assumed_slippage": 0.0010},
    "gate": {"min_history_days": 100, "max_drawdown_limit": 0.35,
             "must_beat_buy_hold_sharpe": True, "must_beat_buy_hold_drawdown": True,
             "rotation_sharpe_edge_required": 0.15, "gate_max_age_days": 45},
}


def series(n=400, drift=0.001, vol=0.03, seed=0, crash_at=None):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    if crash_at:
        r[crash_at:crash_at + 30] = -0.045
    close = 100 * np.exp(np.cumsum(r))
    end = pd.Timestamp.now(tz="UTC").normalize()
    idx = pd.date_range(end=end, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.012, "low": close * 0.988,
        "close": close, "volume": np.full(n, 5_000.0),
    }, index=idx)


# ---------------------------------------------------------------- the floor
def test_floor_never_descends():
    """The single invariant the whole exit design rests on."""
    s = fl.open_floor("BTC/USD", 100.0, 100.0, 5.0, CFG)
    start = s.floor_price
    for px in [110, 120, 115, 90, 80, 130, 60]:
        s, _ = fl.ratchet(s, float(px), 5.0, CFG)
        assert s.floor_price >= start
        start = s.floor_price


def test_floor_rises_with_price():
    s = fl.open_floor("BTC/USD", 100.0, 100.0, 5.0, CFG)
    first = s.floor_price
    s, moved = fl.ratchet(s, 140.0, 5.0, CFG)
    assert moved and s.floor_price > first
    assert s.floor_price == pytest.approx(140.0 - 3.0 * 5.0)


def test_floor_not_churned_when_price_hugs_it():
    """Cancel/replace opens an exposure window. Don't do it near the floor."""
    s = fl.open_floor("BTC/USD", 100.0, 100.0, 5.0, CFG)
    s.floor_price = 99.0
    _, moved = fl.ratchet(s, 100.5, 5.0, CFG)   # only 1.5 above a floor with ATR 5
    assert moved is False


def test_limit_sits_below_stop():
    assert fl.limit_for(100.0, CFG) == pytest.approx(99.0)


# ---------------------------------------------------------------- signals
def test_regime_gate_blocks_everything():
    down = series(400, drift=-0.004, seed=1)
    bars = {"BTC/USD": down, "ETH/USD": series(400, 0.004, seed=2), "SOL/USD": series(400, 0.004, seed=3)}
    d = st.decide(bars, CFG, held={"ETH/USD"})
    assert d.regime_on is False
    assert d.targets == {}
    assert d.exits == ["ETH/USD"]


def test_buffer_band_holds_what_it_would_not_buy():
    """A position between the exit and entry thresholds is held, not churned.
    This asymmetry is the anti-whipsaw device."""
    up = series(400, 0.004, seed=4)
    flat = up.copy()
    flat["close"] = flat["close"].iloc[-1]
    v = st.AssetView("ETH/USD", 100, 90, 0.01, 0.5, 2.0, 10_000)   # mom +1%: below entry, above exit
    views = {"ETH/USD": v}
    assert st.eligible(views, CFG, held=set()) == []             # would not buy
    assert [x.symbol for x in st.eligible(views, CFG, held={"ETH/USD"})] == ["ETH/USD"]  # but holds


def test_illiquid_asset_is_never_eligible():
    v = st.AssetView("SOL/USD", 100, 90, 0.50, 0.5, 2.0, 10.0)    # huge momentum, no volume
    assert st.eligible({"SOL/USD": v}, CFG, held=set()) == []


def test_core_asset_skips_the_liquidity_screen():
    # Alpaca bars carry only Alpaca's own volume (~$150k/day for BTC since 2023).
    # A screen on that number must never lock BTC/ETH out.
    v = st.AssetView("BTC/USD", 100, 90, 0.50, 0.5, 2.0, 10.0)
    assert [p.symbol for p in st.eligible({"BTC/USD": v}, CFG, held=set())] == ["BTC/USD"]


def test_blend_benchmark_rebalances_back_to_target():
    btc, eth = series(seed=1), series(seed=2, drift=0.004)
    res = bt.run_blend({"BTC/USD": btc, "ETH/USD": eth}, {"BTC/USD": 0.7, "ETH/USD": 0.3}, CFG)
    assert len(res.equity) == len(btc) and res.trades > 12 and res.fees_paid > 0
    # never more than a month or one drift band away from 70/30, so it must
    # land between the two buy-and-hold legs
    lo, hi = sorted([btc.close.iloc[-1] / btc.close.iloc[0], eth.close.iloc[-1] / eth.close.iloc[0]])
    assert lo * 0.95 <= res.equity.iloc[-1] / 10_000 <= hi


# ---------------------------------------------------------------- sizing
def test_vol_targeting_sizes_down_the_wilder_asset():
    calm = st.AssetView("BTC/USD", 100, 90, 0.3, 0.40, 2.0, 1e6)
    wild = st.AssetView("SOL/USD", 100, 90, 0.3, 1.20, 2.0, 1e6)
    w = st.target_weights([calm, wild], CFG)
    assert w["BTC/USD"] > w["SOL/USD"]


def test_caps_are_respected():
    calm = [st.AssetView(f"X{i}/USD", 100, 90, 0.3, 0.10, 2.0, 1e6) for i in range(3)]
    w = st.target_weights(calm, CFG)
    assert max(w.values()) <= CFG["sizing"]["max_weight_per_position"] + 1e-9
    assert sum(w.values()) <= CFG["sizing"]["max_total_invested"] + 1e-9


def test_drift_band_suppresses_pointless_trades():
    tgt, cur = {"BTC/USD": 0.30}, {"BTC/USD": 0.29}     # 1% of equity apart
    assert st.orders_needed(tgt, cur, 10_000, CFG) == {}


def test_full_exit_always_goes_through():
    out = st.orders_needed({}, {"BTC/USD": 0.02}, 10_000, CFG)   # tiny, but a full exit
    assert "BTC/USD" in out and out["BTC/USD"] < 0


# ---------------------------------------------------------------- risk
def test_stale_bars_stop_everything():
    old = series(200)
    old.index = old.index - pd.Timedelta(days=400)
    v = R.evaluate({}, 10_000, {"BTC/USD": old}, CFG)
    assert v.halted and not v.may_enter


def test_drawdown_kill_switch_flattens():
    state = {"high_water_equity": 10_000}
    v = R.evaluate(state, 7_000, {"BTC/USD": series(200)}, CFG)
    assert v.must_flatten and v.halted


def test_turnover_budget_stops_new_entries_but_not_exits():
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m")
    state = {"roundtrips": {stamp: 12}, "high_water_equity": 10_000}
    v = R.evaluate(state, 10_000, {"BTC/USD": series(200)}, CFG)
    assert v.may_enter is False and v.must_flatten is False


def test_reconcile_flags_untracked_and_clears_phantom():
    state = {"floors": {"ETH/USD": {}}}
    notes = R.reconcile(state, {"BTC/USD": {"qty": "1"}})
    assert any("UNTRACKED" in n for n in notes)
    assert any("PHANTOM" in n for n in notes)
    assert "ETH/USD" not in state["floors"]


# ---------------------------------------------------------------- engine
def test_backtest_runs_and_respects_cash():
    bars = {"BTC/USD": series(500, 0.002, seed=7),
            "ETH/USD": series(500, 0.002, seed=8),
            "SOL/USD": series(500, 0.002, seed=9)}
    r = bt.run_pawl(bars, CFG, rotation_enabled=True, start_equity=10_000)
    m = r.metrics()
    assert "error" not in m
    assert (r.equity > 0).all()
    assert m["trades"] > 0


def test_regime_filter_cuts_drawdown_through_a_crash():
    """The one claim PAWL actually rests on: the gate should hurt less in a bust."""
    crash = series(600, drift=0.002, seed=11, crash_at=420)
    bars = {"BTC/USD": crash, "ETH/USD": crash.copy(), "SOL/USD": crash.copy()}
    pawl = bt.run_pawl(bars, CFG, rotation_enabled=False).metrics()
    hold = bt.run_buy_hold(bars, ["BTC/USD"], CFG).metrics()
    assert pawl["max_drawdown"] > hold["max_drawdown"]   # less negative


def test_no_lookahead_shifted_series_gives_shifted_result():
    """If the engine peeked at future bars, adding a bar at the end would change
    the past. It must not."""
    base = series(400, 0.002, seed=13)
    bars_a = {"BTC/USD": base.iloc[:-1], "ETH/USD": base.iloc[:-1].copy()}
    bars_b = {"BTC/USD": base, "ETH/USD": base.copy()}
    a = bt.run_pawl(bars_a, CFG, rotation_enabled=False).equity
    b = bt.run_pawl(bars_b, CFG, rotation_enabled=False).equity
    common = a.index.intersection(b.index)[:-1]
    assert np.allclose(a.loc[common].values, b.loc[common].values, rtol=1e-9)


# ---------------------------------------------------------------- venues
def test_every_venue_declares_a_coherent_floor_mode():
    from pawl.broker import ALL_VENUES, capabilities
    for v in ALL_VENUES:
        c = capabilities(v)
        mode = c.floor_mode()
        assert mode in ("native_trailing", "resting_ratchet", "bot_side")
        # A venue that cannot hold a GTC order must never claim a resting floor.
        if not (c.resting_stop and c.good_till_cancelled):
            assert mode in ("native_trailing", "bot_side")


def test_ibkr_is_rejected_for_the_right_reason():
    """Cheaper than Alpaca on fees, disqualified on order model. If someone
    'fixes' this by looking at the fee table only, this test should stop them."""
    from pawl.broker import capabilities
    ib, al = capabilities("ibkr"), capabilities("alpaca")
    assert ib.roundtrip_taker < al.roundtrip_taker      # genuinely cheaper
    assert ib.floor_mode() == "bot_side"                # and still not usable


def test_perps_venue_is_the_cheapest_and_unlocks_shorting():
    from pawl.broker import ALL_VENUES, capabilities
    perps = capabilities("kraken_us_perps")
    assert perps.shorting and perps.perpetuals
    assert perps.roundtrip_taker == min(capabilities(v).roundtrip_taker for v in ALL_VENUES)


def test_sim_broker_charges_the_target_venues_fees():
    """The whole point of sim is an honest rehearsal, which means the target
    venue's costs -- not the data source's."""
    import pawl.broker.sim as simmod
    from pawl.broker import capabilities

    class FakeData:
        def daily_bars(self, symbols, days):
            return {s: series(120) for s in symbols}

    sim = simmod.SimBroker(target_venue="kraken_us_perps", data_broker=FakeData(),
                           start_equity=10_000, ledger_path="/tmp/pawl_test_ledger.json")
    assert sim.caps.taker_fee == capabilities("kraken_us_perps").taker_fee
    sim.daily_bars(["BTC/USD"], 120)
    sim.market("BTC/USD", "buy", 1.0)
    assert "BTC/USD" in sim.positions()
    assert sim.equity() > 0
    import os
    os.remove("/tmp/pawl_test_ledger.json")


def test_sim_resting_stop_fills_when_the_bar_trades_through_it():
    import pawl.broker.sim as simmod
    df = series(120)
    last = float(df["close"].iloc[-1])

    class FakeData:
        def daily_bars(self, symbols, days):
            d = df.copy()
            d.iloc[-1, d.columns.get_loc("low")] = last * 0.5   # deep wick
            return {s: d for s in symbols}

    sim = simmod.SimBroker(target_venue="alpaca", data_broker=FakeData(),
                           start_equity=10_000, ledger_path="/tmp/pawl_test_ledger2.json")
    sim.daily_bars(["BTC/USD"], 120)
    sim.market("BTC/USD", "buy", 1.0)
    sim.protective_stop("BTC/USD", 1.0, last * 0.9, last * 0.89)
    sim.daily_bars(["BTC/USD"], 120)          # bar trades through the stop
    assert "BTC/USD" not in sim.positions()
    import os
    os.remove("/tmp/pawl_test_ledger2.json")
