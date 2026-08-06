"""STEWARD stage 1 — portfolio scanner.

Self-contained (does not touch the traders' scanner): fetches daily bars for the
steward universe and computes the features the portfolio analyzer needs. Output
is journaled as scan.json — the ONLY market data the analyzer sees.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.common import now_et


def _sym_df(bars: pd.DataFrame, symbol: str):
    try:
        df = bars.xs(symbol, level="symbol")
        return df if len(df) else None
    except KeyError:
        return None


def scan(data, cfg: dict) -> dict:
    uni = cfg["universe"]
    symbols = sorted(set(uni["stocks"] + uni["index_etfs"] + uni["defensive_etfs"]))
    s = cfg["strategy"]
    need = s["momentum_lookback_days"] + s["momentum_skip_days"] + 10

    daily = data.daily_bars(symbols, days=need)
    snapshot: dict[str, dict] = {}
    for sym in symbols:
        df = _sym_df(daily, sym)
        if df is None or len(df) < 60:
            continue
        close = df["close"]
        rets = close.pct_change().dropna()
        lb, skip = s["momentum_lookback_days"], s["momentum_skip_days"]
        mom_12_1 = (float(close.iloc[-skip] / close.iloc[-lb] - 1)
                    if len(close) >= lb else None)
        mom_6m = (float(close.iloc[-1] / close.iloc[-s["index_trend_lookback_days"]] - 1)
                  if len(close) >= s["index_trend_lookback_days"] else None)
        sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        snapshot[sym] = {
            "close": float(close.iloc[-1]),
            "last_bar_date": str(df.index[-1].date()),
            "mom_12_1": mom_12_1,
            "mom_6m": mom_6m,
            "sma200": sma200,
            "above_200sma": bool(sma200 and close.iloc[-1] > sma200),
            "vol_63d_ann": float(rets.tail(63).std() * np.sqrt(252)) if len(rets) >= 30 else None,
            "avg_dollar_vol_20d": float((close * df["volume"]).tail(20).mean()),
            "sleeve": ("stock" if sym in uni["stocks"] else
                       "index" if sym in uni["index_etfs"] else "defensive"),
        }

    return {"mode": "portfolio", "asof_et": now_et().isoformat(),
            "benchmark": cfg["benchmark"], "universe_size": len(symbols),
            "scanned": len(snapshot), "symbols": snapshot}
