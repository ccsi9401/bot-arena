"""Pure indicator math on pandas Series/DataFrames. No I/O, no broker, no state."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """df needs columns high, low, close."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def session_vwap(intraday: pd.DataFrame) -> float:
    """Volume-weighted average price over today's bars (columns: close/high/low/volume)."""
    typical = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    v = intraday["volume"]
    if v.sum() <= 0:
        return float(intraday["close"].iloc[-1])
    return float((typical * v).sum() / v.sum())


def pct_from_52wk_high(close: pd.Series) -> float:
    hi = close.tail(252).max()
    return float((hi - close.iloc[-1]) / hi * 100) if hi > 0 else 100.0
