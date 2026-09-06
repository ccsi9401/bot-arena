"""Offline tests for GLIDER's core sleeve (idle cash in SPY) and the benchmark-relative
gate — synthetic data, no broker, no network."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import core_sleeve as cs
from core import validator
from core.common import State, now_et
from bots.swing.analyzer import analyze
from backtest import engine_glider as eg
from backtest import metrics
from backtest.glider_learn import sharpe, active_returns


def _cfg(core=True, mode="always", **strat):
    s = dict(regime_filter="spy_above_200sma", sma_fast=50, sma_slow=200, max_pct_below_52wk_high=15,
             pullback_rsi2_max=10, ema_touch_period=20, atr_period=14, stop_atr_mult=2.0,
             exit_mode="target", target_r_mult=2.0, trail_atr_mult=3.0, trail_target_r_mult=8.0,
             breakeven_at_r=1.0, max_hold_days=15, min_price=10.0, min_avg_dollar_vol=5e7)
    s.update(strat)
    cfg = {"starting_equity": 5000, "strategy": s,
           "risk": {"risk_per_trade_pct": 0.9, "max_positions": 5, "max_gross_exposure_pct": 100,
                    "kill_switch_drawdown_pct": 15.0, "daily_loss_limit_pct": None,
                    "data_freshness_minutes": 60, "limit_price_tolerance_pct": 1.0},
           "universe": {"benchmark": "SPY", "etfs": ["SPY"], "stocks": ["AAA", "BBB", "CCC"]}}
    if core:
        cfg["core_sleeve"] = {"enabled": True, "symbol": "SPY", "mode": mode, "cash_buffer_pct": 1.0,
                              "min_order_dollars": 25, "slippage_bps": 1}
    return cfg


ACCT = {"equity": 5000.0, "cash": 100.0, "last_equity": 5000.0, "buying_power": 100.0, "status": "ACTIVE"}


# ---------------- planner ----------------
def test_plan_always_targets_idle_cash():
    core = {"symbol": "SPY", "market_value": 3000.0}
    other = [{"symbol": "AAA", "market_value": 1900.0}]
    p = cs.plan(ACCT, core, other, [], [], _cfg())
    assert p["target"] == 3050 and p["action"] == "buy" and p["notional"] == 50  # 5000-1900-50


def test_plan_sells_to_fund_approved_buys():
    core = {"symbol": "SPY", "market_value": 4900.0}
    approved = [{"action": "buy", "symbol": "BBB", "notional": 700.0}]
    p = cs.plan(ACCT, core, [], [], approved, _cfg())
    assert p["action"] == "sell" and p["notional"] == 650  # target 5000-700-50 = 4250


def test_plan_respects_pending_buys_and_min_order():
    core = {"symbol": "SPY", "market_value": 4000.0}
    pending = [{"symbol": "CCC", "side": "OrderSide.BUY", "qty": 5, "limit_price": 190.0}]
    p = cs.plan(ACCT, core, [], pending, [], _cfg())
    assert p["action"] == "hold" and p["notional"] == 0  # target 5000-950-50 = 4000


def test_plan_goes_flat_on_kill_or_closed_gate_or_disabled():
    core = {"symbol": "SPY", "market_value": 4000.0}
    assert cs.plan(ACCT, core, [], [], [], _cfg(), kill=True)["target"] == 0
    assert cs.plan(ACCT, core, [], [], [], _cfg(mode="gated"), regime_ok=False)["action"] == "sell"
    assert cs.plan(ACCT, core, [], [], [], _cfg(mode="gated"), regime_ok=True)["action"] != "sell"
    assert cs.plan(ACCT, core, [], [], [], _cfg(core=False))["target"] == 0


def test_split_positions_strips_core():
    pos = [{"symbol": "SPY", "market_value": 1}, {"symbol": "AAA", "market_value": 2}]
    core, rest = cs.split_positions(pos, _cfg())
    assert core["symbol"] == "SPY" and [p["symbol"] for p in rest] == ["AAA"]
    assert cs.split_positions(pos, _cfg(core=False)) == (None, pos)


# ---------------- analyzer / validator ----------------
def _feat(close=100, **kw):
    f = dict(close=close, sma50=95, sma200=90, ema20=99, rsi2=5, atr14=2, avg_dollar_vol_20d=1e8,
             pct_below_52wk_high=3, low_today=98)
    f.update(kw)
    return f


def test_analyzer_never_proposes_core_symbol():
    scan = {"benchmark": "SPY", "symbols": {"SPY": _feat(), "AAA": _feat()}}
    on = [i["symbol"] for i in analyze(scan, _cfg(), [])["intents"] if i["action"] == "buy"]
    assert on == ["AAA"]
    off = [i["symbol"] for i in analyze(scan, _cfg(core=False), [])["intents"] if i["action"] == "buy"]
    assert set(off) == {"SPY", "AAA"}


def test_validator_counts_core_value_as_spendable(tmp_path):
    st = State("glider")
    st.dir = tmp_path
    intent = {"action": "buy", "symbol": "AAA", "entry_limit": 100.0, "stop": 96.0, "target": 108.0}
    acct = dict(ACCT, cash=0.0)
    v = validator.validate([intent], acct, [], [], _cfg(), st, now_et().isoformat(), True, {"AAA": 100.0})
    assert v["rejected"] and "spendable" in v["rejected"][0]["reject_reason"]
    v2 = validator.validate([intent], dict(acct, core_value=4900.0), [], [], _cfg(), st,
                            now_et().isoformat(), True, {"AAA": 100.0})
    assert len(v2["approved"]) == 1 and v2["approved"][0]["qty"] == 11  # $45 risk / $4 per share


# ---------------- engine ----------------
def _synth(n=900, seed=0, spy_drift=0.0006):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    out = {}
    for i, sym in enumerate(["SPY", "AAA", "BBB", "CCC"]):
        drift = spy_drift if sym == "SPY" else 0.0006 + 0.0003 * i
        close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
        high = close * (1 + rng.uniform(0, 0.02, n))
        low = close * (1 - rng.uniform(0, 0.02, n))
        opn = close * (1 + rng.normal(0, 0.005, n))
        vol = rng.uniform(5e6, 2e7, n)
        out[sym] = pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "volume": vol},
                                index=idx)
    return out


def test_engine_sleeve_off_is_unchanged_and_on_tracks_benchmark():
    data = _synth()
    off, t_off = eg.run(data, _cfg(core=False))
    disabled_cfg = _cfg()
    disabled_cfg["core_sleeve"]["enabled"] = False
    off2, _ = eg.run(data, disabled_cfg)
    pd.testing.assert_series_equal(off, off2)          # block present but off == no block
    on, t_on = eg.run(data, _cfg())
    assert len(on) == len(off) and on.iloc[0] > 0
    spy = data["SPY"]["close"].reindex(on.index).pct_change()
    c_on, c_off = on.pct_change().corr(spy), off.pct_change().corr(spy)
    assert c_on > 0.5 and c_on > c_off + 0.3           # fully invested book moves with SPY
    # overlay still trades; SPY itself is no longer a setup once the sleeve holds it
    t_off_ex = [t for t in t_off if t["symbol"] != "SPY"]
    assert all(t["symbol"] != "SPY" for t in t_on)
    assert abs(len(t_on) - len(t_off_ex)) <= max(3, 0.2 * len(t_off_ex))


def test_engine_gated_mode_leaves_core_when_gate_closed():
    data = _synth(spy_drift=-0.0008)                    # SPY trends down: gate mostly closed
    gated, _ = eg.run(data, _cfg(mode="gated"))
    always, _ = eg.run(data, _cfg(mode="always"))
    spy = data["SPY"]["close"].reindex(gated.index).pct_change()
    assert always.pct_change().corr(spy) > gated.pct_change().corr(spy)


# ---------------- gate / scoring ----------------
def test_gate_is_benchmark_relative_when_bench_given():
    idx = pd.bdate_range("2024-01-01", periods=300)
    bench = pd.Series(np.linspace(100, 80, 150).tolist() + np.linspace(80, 120, 150).tolist(), index=idx)
    trades = [{"pnl": 1.0, "risk": 1.0}] * 30
    s = metrics.summarize(bench * 1.05, trades, "x", bench_curve=bench)
    assert s["gate"]["checks"]["max_dd_within_benchmark"] and s["gate"]["checks"]["return_ge_benchmark"]
    assert "max_dd_under_15pct" not in s["gate"]["checks"] and s["gate"]["passed"]
    worse = metrics.summarize(bench + 20, trades, "y", bench_curve=bench)   # +16.7% < +20%
    assert not worse["gate"]["checks"]["return_ge_benchmark"] and not worse["gate"]["passed"]
    absolute = metrics.summarize(bench * 1.05, trades, "z")                 # DD -20% vs 15% cap
    assert "max_dd_under_15pct" in absolute["gate"]["checks"] and not absolute["gate"]["passed"]


def test_active_sharpe_is_zero_against_itself():
    idx = pd.bdate_range("2024-01-01", periods=300)
    bench = pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0.0005, 0.01, 300))), index=idx)
    s = sharpe(bench, bench)
    assert s is None or abs(s) < 1e-9
    assert abs(active_returns(bench * 2, bench)).max() < 1e-9
    assert sharpe(bench) is not None and sharpe(bench) != 0
