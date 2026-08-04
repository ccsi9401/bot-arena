"""Smoke test for both backtest engines using synthetic data (no network).
Run: python -m tests.test_backtest_smoke
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.common import load_config
from backtest.engine_glider import run as run_glider
from backtest.engine_scalpel import run as run_scalpel
from backtest.metrics import summarize

rng = np.random.default_rng(7)


def synth_daily(n=400, start=100.0, drift=0.0006, vol=0.015):
    dates = pd.bdate_range("2025-01-02", periods=n)
    rets = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(rets))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    volu = rng.integers(2_000_000, 9_000_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volu}, index=dates)


def synth_hourly(days=60, start=100.0):
    frames = []
    dates = pd.bdate_range("2026-05-01", periods=days)
    px = start
    for d in dates:
        idx = pd.date_range(f"{d.date()} 09:30", f"{d.date()} 15:30", freq="60min",
                            tz="America/New_York")
        rets = rng.normal(0.0002, 0.004, len(idx))
        closes = px * np.exp(np.cumsum(rets))
        opens = np.r_[px, closes[:-1]]
        highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.002, len(idx))))
        lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.002, len(idx))))
        vol = rng.integers(300_000, 1_500_000, len(idx)).astype(float)
        frames.append(pd.DataFrame({"open": opens, "high": highs, "low": lows,
                                    "close": closes, "volume": vol}, index=idx))
        px = closes[-1]
    return pd.concat(frames)


def main() -> int:
    gcfg = load_config("glider")
    scfg = load_config("scalpel")
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD", "EEE"]
    # shrink universe/thresholds so synthetic data can trigger signals
    for cfg in (gcfg, scfg):
        cfg["universe"] = {"stocks": syms[1:], "etfs": ["SPY"], "benchmark": "SPY"}
        cfg["strategy"]["min_avg_dollar_vol"] = 0

    ddata = {s: synth_daily(drift=0.0008 if s != "SPY" else 0.0005) for s in syms}
    g_curve, g_trades = run_glider(ddata, gcfg)
    g = summarize(g_curve, g_trades, "glider-synth")
    print("GLIDER synth:", {k: g[k] for k in ("n_trades", "total_return_pct",
                                              "max_drawdown_pct")})
    assert len(g_curve) > 100, "glider curve too short"

    hdata = {s: synth_hourly() for s in syms}
    s_curve, s_trades = run_scalpel(hdata, scfg)
    s = summarize(s_curve, s_trades, "scalpel-synth") if len(s_curve) else {"n_trades": 0}
    print("SCALPEL synth:", {k: s.get(k) for k in ("n_trades", "total_return_pct",
                                                   "max_drawdown_pct")})
    assert len(s_curve) > 10, "scalpel curve too short"
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
