"""Offline tests for the Markov 2.0 regime gate — synthetic data, no network."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import markov2 as mk
from bots.swing.analyzer import analyze
from backtest import engine_glider as eg


def _series(n=900, seed=3):
    """Quiet drift, then a 20-bar crash (~-25%), a 40-bar rally (~+35%), quiet again."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0002, 0.004, n)
    steps[400:420] -= 0.0145   # crash
    steps[430:470] += 0.0080   # recovery rally
    close = 100 * np.exp(np.cumsum(steps))
    return pd.Series(close, index=pd.bdate_range("2022-01-03", periods=n))


def test_fix2_label_self_checks():
    close = _series()
    states = mk.label_states(close)
    ret20 = (close / close.shift(mk.WINDOW) - 1.0)[states.index]
    assert states[ret20.idxmin()] == mk.BEAR      # worst 20d return labels BEAR
    assert states[ret20.idxmax()] == mk.BULL      # best 20d return labels BULL
    assert states[ret20.abs().idxmin()] == mk.SIDEWAYS
    crash = states.loc["2023-07-15":"2023-08-20"]  # bars 400-420 fall in here
    assert (crash == mk.BEAR).mean() > 0.5


def test_stride_matrix_less_sticky_than_overlapping():
    # regime alternates every WINDOW bars -> overlapping windows fake persistence
    arr = np.array(([mk.BULL] * mk.WINDOW + [mk.BEAR] * mk.WINDOW) * 10)
    M_legacy, C_legacy = mk.transition_matrix(arr)
    M_stride, C_stride = mk.transition_matrix(arr[::mk.WINDOW])
    assert np.allclose(M_legacy.sum(axis=1)[[mk.BULL, mk.BEAR]], 1)
    assert np.allclose(M_stride.sum(axis=1)[[mk.BULL, mk.BEAR]], 1)
    assert M_legacy[mk.BULL, mk.BULL] > 0.9       # overlapping: looks sticky
    assert M_stride[mk.BULL, mk.BULL] == 0.0      # stride: pure alternation
    assert C_stride.sum() < C_legacy.sum()


def test_signal_series_no_lookahead():
    close = _series()
    full = mk.signal_series(close)
    cut = len(close) - 100
    mutated = close.copy()
    mutated.iloc[cut:] = close.iloc[cut:] * np.linspace(1.0, 0.4, 100)  # fake future crash
    part = mk.signal_series(mutated)
    common = full.index[full.index < close.index[cut]]
    pd.testing.assert_frame_equal(full.loc[common], part.loc[common])
    mature = full["signal"].dropna()
    assert len(mature) > 0 and (full["n_transitions"].values[:-1] <= full["n_transitions"].values[1:]).all()


def _cfg(**strat):
    s = dict(regime_filter="markov2", markov2_min_signal=0.0, sma_fast=50, sma_slow=200,
             max_pct_below_52wk_high=15, pullback_rsi2_max=10, ema_touch_period=20,
             atr_period=14, stop_atr_mult=2.0, exit_mode="target", target_r_mult=2.0,
             trail_atr_mult=3.0, trail_target_r_mult=8.0, breakeven_at_r=1.0,
             max_hold_days=15, min_price=10.0, min_avg_dollar_vol=5e7)
    s.update(strat)
    return {"starting_equity": 5000, "strategy": s,
            "risk": {"risk_per_trade_pct": 0.9, "max_positions": 5},
            "universe": {"benchmark": "SPY", "etfs": ["SPY"], "stocks": ["AAA"]}}


def _scan(bench_extra=None):
    f = dict(close=100, sma50=95, sma200=90, ema20=99, rsi2=5, atr14=2, avg_dollar_vol_20d=1e8,
             pct_below_52wk_high=3, low_today=98)
    bench = dict(f)
    if bench_extra:
        bench.update(bench_extra)
    return {"benchmark": "SPY", "symbols": {"SPY": bench, "AAA": dict(f)}}


def test_analyzer_markov_filter_gates_entries():
    open_gate = analyze(_scan({"markov2_signal": 0.42, "markov2_state": "BULL"}), _cfg(), [])
    assert any(i["action"] == "buy" for i in open_gate["intents"]) and open_gate["regime_ok"]
    shut = analyze(_scan({"markov2_signal": -0.30, "markov2_state": "BEAR"}), _cfg(), [])
    assert shut["regime_ok"] is False and not any(i["action"] == "buy" for i in shut["intents"])
    assert any("markov2" in n for n in shut["notes"])
    # min_signal raises the bar
    strict = analyze(_scan({"markov2_signal": 0.10, "markov2_state": "SIDEWAYS"}),
                     _cfg(markov2_min_signal=0.25), [])
    assert strict["regime_ok"] is False


def test_analyzer_falls_back_to_200sma_when_immature():
    r = analyze(_scan(), _cfg(), [])            # markov2 requested, signal absent, close>sma200
    assert r["regime_ok"] and any("fell back" in n for n in r["notes"])
    bear = analyze(_scan({"sma200": 105}), _cfg(), [])
    assert bear["regime_ok"] is False           # fallback still enforces the 200SMA gate


def test_engine_runs_with_markov_filter():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2022-01-03", periods=900)
    data = {}
    for i, sym in enumerate(["SPY", "AAA"]):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0006 + 0.0003 * i, 0.015, 900)))
        data[sym] = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                                  "close": close, "volume": np.full(900, 1e7)}, index=idx)
    curve, trades = eg.run(data, _cfg())
    base, _ = eg.run(data, _cfg(regime_filter="spy_above_200sma"))
    assert len(curve) == len(base) > 600 and curve.iloc[0] > 0
    pre = eg.precompute(data, {"SPY"})
    sig_days = [d for d, r in pre["feats"]["SPY"].items() if "markov2_signal" in r]
    assert len(sig_days) > 0                    # matrix matures inside the window
