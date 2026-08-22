"""Backtest data layer — fetches free historical data (yfinance) and caches to parquet.

Runs on the GitHub Actions runner (full network), not in the Claude sandbox.

NOTE: backtest/cache/ is in .gitignore, so the parquet cache is LOCAL ONLY and is
not committed. Every CI run therefore re-downloads from yfinance and sees whatever
window is current that day — two gate runs a week apart are not comparing like with
like, and small shifts in the reported figures are expected. That is a deliberate
trade (a gate should grade fresh data) but it means these numbers are not
reproducible from the repo alone; commit the parquet if you ever need them to be.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHE = Path(__file__).resolve().parent / "cache"
CACHE.mkdir(exist_ok=True)


def _yf():
    import yfinance as yf
    return yf


def daily_history(symbols: list[str], period: str = "3y") -> dict[str, pd.DataFrame]:
    """Per-symbol daily OHLCV (unadjusted close for price levels, splits auto-handled)."""
    f = CACHE / f"daily_{period}.parquet"
    if f.exists():
        raw = pd.read_parquet(f)
    else:
        raw = _yf().download(symbols, period=period, interval="1d",
                             auto_adjust=True, progress=False, group_by="ticker",
                             threads=True)
        raw.to_parquet(f)
    return _split(raw, symbols)


def hourly_history(symbols: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    f = CACHE / f"hourly_{period}.parquet"
    if f.exists():
        raw = pd.read_parquet(f)
    else:
        raw = _yf().download(symbols, period=period, interval="1h",
                             auto_adjust=True, progress=False, group_by="ticker",
                             prepost=False, threads=True)
        raw.to_parquet(f)
    return _split(raw, symbols)


def _split(raw: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in symbols:
        ysym = sym.replace(".", "-")  # BRK.B -> BRK-B
        try:
            df = raw[ysym] if ysym in raw.columns.get_level_values(0) else raw[sym]
        except Exception:
            continue
        df = df.rename(columns=str.lower).dropna(subset=["close"])
        if len(df):
            out[sym] = df[["open", "high", "low", "close", "volume"]]
    return out
