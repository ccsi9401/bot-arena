"""Indicators and the six-vote. Copied from the backtest engine so live == tested.

Reference: Documents/Alpaca Trading EJ/Twin-Coin-Trend-Bot/backtest/backtest.py
(indicators_daily, indicators_4h, vote). tests/test_twincoin.py holds a verbatim copy of
the reference and asserts equality on random bars, so a drift here fails the suite.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def indicators_daily(d: pd.DataFrame, slope_bars: int = 5, obv_lookback: int = 20) -> pd.DataFrame:
    """d: DataFrame indexed by UTC bar open time with open/high/low/close/volume, ascending."""
    d = d.copy()
    c = d["close"]
    d["ema20"], d["ema50"], d["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    d["ema50_5"] = d["ema50"].shift(slope_bars)
    delta = c.diff()
    up = delta.clip(lower=0)
    dn = -delta.clip(upper=0)
    rs = wilder(up, 14) / wilder(dn, 14)
    d["rsi"] = 100 - 100 / (1 + rs)
    m = ema(c, 12) - ema(c, 26)
    d["macd"], d["sig"] = m, ema(m, 9)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - c.shift()).abs(), (d["low"] - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = wilder(tr, 14)
    obv = (np.sign(c.diff()).fillna(0) * d["volume"]).cumsum()
    d["obv"], d["obv20"] = obv, obv.shift(obv_lookback)
    return d


def indicators_4h(h: pd.DataFrame) -> pd.DataFrame:
    h = h.copy()
    h["e20"], h["e50"] = ema(h["close"], 20), ema(h["close"], 50)
    return h


def vote(r, h, rsi_bull=(55, 75), rsi_bear=45) -> tuple[int, int, dict]:
    """(bull_count, bear_count, detail). r: a daily row with indicators; h: the 4H row at the daily close."""
    det = {}
    # 1 trend, two timeframes must agree
    if r.close > r.ema20 and h.e20 > h.e50:
        det["trend"] = 1
    elif r.close < r.ema20 and h.e20 < h.e50:
        det["trend"] = -1
    else:
        det["trend"] = 0
    # 2 rsi
    if rsi_bull[0] <= r.rsi <= rsi_bull[1]:
        det["rsi"] = 1
    elif r.rsi < rsi_bear:
        det["rsi"] = -1
    else:
        det["rsi"] = 0
    # 3 macd
    det["macd"] = 1 if r.macd > r.sig else -1
    # 4 ema50 with slope
    if r.close > r.ema50 and r.ema50 > r.ema50_5:
        det["ema50"] = 1
    elif r.close < r.ema50 and r.ema50 < r.ema50_5:
        det["ema50"] = -1
    else:
        det["ema50"] = 0
    # 5 ema200 structure
    if r.close > r.ema200 and r.ema50 > r.ema200:
        det["ema200"] = 1
    elif r.close < r.ema200 and r.ema50 < r.ema200:
        det["ema200"] = -1
    else:
        det["ema200"] = 0
    # 6 volume (OBV 20-bar change)
    det["volume"] = 1 if (r.obv - r.obv20) > 0 else -1
    b = sum(1 for v in det.values() if v == 1)
    s = sum(1 for v in det.values() if v == -1)
    return b, s, det


def sanitize_daily(al: pd.DataFrame, cb: pd.DataFrame | None) -> pd.DataFrame:
    """Clip Alpaca highs/lows to what Coinbase also traded on the same day (phantom-wick fix).

    Alpaca's crypto feed prints occasional lows 10-20% below the candle body on one coin of
    volume. They inflate ATR and would fire stops at prices that never traded. A low only
    counts if Coinbase also traded through it; without Coinbase, fall back to clipping wicks
    to 8% beyond the body, which is wider than any real BTC/ETH daily wick outside a crash.
    """
    out = al.copy()
    body_lo = out[["open", "close"]].min(axis=1)
    body_hi = out[["open", "close"]].max(axis=1)
    if cb is not None and len(cb):
        cb = cb.reindex(out.index)
        out["low"] = np.where(cb["low"].notna(), np.maximum(out["low"], cb["low"]), out["low"])
        out["high"] = np.where(cb["high"].notna(), np.minimum(out["high"], cb["high"]), out["high"])
    else:
        out["low"] = np.maximum(out["low"], body_lo * 0.92)
        out["high"] = np.minimum(out["high"], body_hi * 1.08)
    out["low"] = np.minimum(out["low"], body_lo)
    out["high"] = np.maximum(out["high"], body_hi)
    return out
