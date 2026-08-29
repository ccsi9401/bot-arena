"""Offline tests for the GLIDER learning stack — synthetic data, no network."""
import math, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bots.swing.analyzer import analyze
from backtest import engine_glider as eg
from backtest.glider_learn import set_yaml_key, evaluate, bootstrap_sharpe_std, candidates_from_grid, GRID
from core import indicators as ind

def _cfg(**strat):
    s = dict(regime_filter="spy_above_200sma", sma_fast=50, sma_slow=200, max_pct_below_52wk_high=15,
             pullback_rsi2_max=10, ema_touch_period=20, atr_period=14, stop_atr_mult=2.0,
             exit_mode="target", target_r_mult=2.0, trail_atr_mult=3.0, trail_target_r_mult=8.0,
             breakeven_at_r=1.0, max_hold_days=15, min_price=10.0, min_avg_dollar_vol=5e7)
    s.update(strat)
    return {"starting_equity": 5000, "strategy": s,
            "risk": {"risk_per_trade_pct": 0.9, "max_positions": 5},
            "universe": {"benchmark": "SPY", "etfs": ["SPY"], "stocks": ["AAA","BBB","CCC"]}}

def _synth(n=900, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    out = {}
    for i, sym in enumerate(["SPY","AAA","BBB","CCC"]):
        drift = 0.0006 + 0.0003*i
        close = 100*np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
        high = close*(1+rng.uniform(0,0.02,n)); low = close*(1-rng.uniform(0,0.02,n))
        opn = close*(1+rng.normal(0,0.005,n)); vol = rng.uniform(5e6,2e7,n)
        out[sym] = pd.DataFrame({"open":opn,"high":high,"low":low,"close":close,"volume":vol}, index=idx)
    return out

def test_trail_mode_emits_raise_stop_and_far_target():
    f = dict(close=100, sma50=95, sma200=90, ema20=99, rsi2=5, atr14=2, avg_dollar_vol_20d=1e8,
             pct_below_52wk_high=3, low_today=98)
    scan = {"benchmark":"SPY","symbols":{"SPY":dict(f), "AAA":dict(f)}}
    r = analyze(scan, _cfg(exit_mode="trail"), [{"symbol":"BBB","entry":80,"stop":76,"age_days":3}])
    # BBB not in scan → no management; AAA buy with far target
    buy = [i for i in r["intents"] if i["action"]=="buy"][0]
    assert buy["target"] == 100 + 8.0*4  # 8R × (2×ATR)
    scan["symbols"]["BBB"] = dict(f, close=95, atr14=2)
    r = analyze(scan, _cfg(exit_mode="trail"), [{"symbol":"BBB","entry":80,"stop":76,"age_days":3}])
    rs = [i for i in r["intents"] if i["action"]=="raise_stop"][0]
    assert rs["new_stop"] == 89.0  # 95 - 3×2, above breakeven 80

def test_target_mode_unchanged_behaviour():
    f = dict(close=100, sma50=95, sma200=90, ema20=99, rsi2=5, atr14=2, avg_dollar_vol_20d=1e8,
             pct_below_52wk_high=3, low_today=98)
    scan = {"benchmark":"SPY","symbols":{"SPY":dict(f), "AAA":dict(f)}}
    r = analyze(scan, _cfg(), [])
    buy = r["intents"][0]
    assert buy["stop"] == 96 and buy["target"] == 108

def test_precompute_matches_slice_method():
    data = _synth()
    pre = eg.precompute(data, {"SPY"})
    df = data["AAA"]; day = df.index[400]; hist = df.loc[:day]
    close = hist["close"]
    old = {"sma50": float(ind.sma(close,50).iloc[-1]), "sma200": float(ind.sma(close,200).iloc[-1]),
           "ema20": float(ind.ema(close,20).iloc[-1]), "rsi2": float(ind.rsi(close,2).iloc[-1]),
           "atr14": float(ind.atr(hist,14).iloc[-1]),
           "avg_dollar_vol_20d": float((close*hist["volume"]).tail(20).mean()),
           "pct_below_52wk_high": ind.pct_from_52wk_high(close)}
    new = pre["feats"]["AAA"][day]
    for k, v in old.items():
        assert math.isclose(v, new[k], rel_tol=1e-9), k

def test_engine_runs_and_learner_pieces():
    data = _synth()
    cfg = _cfg()
    curve, trades = eg.run(data, cfg)
    assert len(curve) > 600 and curve.iloc[0] > 0
    hold = curve.index[-252]
    ev = evaluate(curve, trades, hold)
    assert "sel_sharpe" in ev and ev["full"]["n_trades"] == len(trades)
    assert bootstrap_sharpe_std(curve, 50) >= 0
    c = candidates_from_grid(cfg["strategy"], 20, 1)
    assert len(c) == 20 and all("exit_mode" in s for s in c)
    full_grid = candidates_from_grid(cfg["strategy"], 10_000, 1)
    assert {s["regime_filter"] for s in full_grid} == {"spy_above_200sma", "markov2"}
    # trail mode also runs end-to-end
    curve2, trades2 = eg.run(data, _cfg(exit_mode="trail"))
    assert len(curve2) == len(curve)

def test_yaml_editor_keeps_comments():
    txt = "strategy:\n  exit_mode: target                  # learnable\n  stop_atr_mult: 2.0 # x\nrisk:\n  risk_per_trade_pct: 0.9\n"
    out = set_yaml_key(txt, "exit_mode", "trail")
    out = set_yaml_key(out, "stop_atr_mult", 2.5)
    assert "exit_mode: trail                  # learnable" in out
    assert "stop_atr_mult: 2.5 # x" in out
    import yaml
    d = yaml.safe_load(out)
    assert d["strategy"]["stop_atr_mult"] == 2.5 and d["strategy"]["exit_mode"] == "trail"
