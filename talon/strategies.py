"""Indicators and signal functions.

Contract: EVERY strategy takes ``(df, cfg)`` — ``df`` a bar frame with columns
open/high/low/close/volume, ``cfg`` that strategy's own block from
``config/talon.yaml`` — and returns a pandas Series of 0/1 aligned to ``df.index``.
The live cycle takes ``.iloc[-1]``; the backtest takes the whole series. Same function.
Nothing here returns a scalar.

All indicators are causal (rolling / ewm / shift only). Where a window is not yet
full the indicator is NaN and the signal is 0 — a strictly rising series produces
no signal before the slowest window is defined (see ``warmup_bars``).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Unit constants, not tunables: RSI is scaled 0..100 by definition.
RSI_SCALE = 100.0


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """EMA with min_periods=n so it is NaN (not a seed) until the window is full."""
    n = int(n)
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing: EMA with alpha = 1/n, NaN until n observations."""
    n = int(n)
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder ATR."""
    return _wilder(true_range(df), n)


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Wilder RSI, 0..100. NaN until n bars; RSI_SCALE when there are no losses."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder(gain, n)
    avg_loss = _wilder(loss, n)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = RSI_SCALE - RSI_SCALE / (1.0 + rs)
    # no losses at all in the window -> RSI is 100 by definition (not NaN)
    out = out.where(~((avg_loss == 0.0) & avg_gain.notna()), RSI_SCALE)
    return out


def zscore(s: pd.Series, n: int) -> pd.Series:
    n = int(n)
    mu = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def realised_vol(close: pd.Series, window: int, periods_per_year: int) -> pd.Series:
    """Annualised stdev of log returns over ``window`` bars."""
    lr = np.log(close / close.shift(1))
    return lr.rolling(int(window), min_periods=int(window)).std(ddof=0) * np.sqrt(float(periods_per_year))


def pct_change(close: pd.Series, n: int) -> pd.Series:
    """close / close[n bars ago] - 1, without pandas' fill-forward behaviour."""
    return close / close.shift(int(n)) - 1.0


# ---------------------------------------------------------------------------
# Strategies: (df, cfg) -> 0/1 Series aligned to df.index
# ---------------------------------------------------------------------------

def _as_signal(cond: pd.Series, index: pd.Index) -> pd.Series:
    """NaN-safe bool -> 0/1 int Series on the frame's index."""
    return cond.fillna(False).astype(bool).astype(int).reindex(index).fillna(0).astype(int)


def donchian_breakout(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Close above the max of the PREVIOUS ``entry_lookback`` highs (shift(1) keeps the
    current bar out of its own breakout level) AND above the ``trend_sma``."""
    n = int(cfg["entry_lookback"])
    level = df["high"].rolling(n, min_periods=n).max().shift(1)
    trend = sma(df["close"], cfg["trend_sma"])
    cond = (df["close"] > level) & (df["close"] > trend)
    return _as_signal(cond, df.index)


def ema_trend(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Fast EMA above slow EMA AND the slow EMA higher than ``slope_bars`` ago."""
    fast = ema(df["close"], cfg["fast"])
    slow = ema(df["close"], cfg["slow"])
    rising = slow > slow.shift(int(cfg["slope_bars"]))
    cond = (fast > slow) & rising
    return _as_signal(cond, df.index)


def dip_reversion(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """In an uptrend (close above ``trend_sma`` — mandatory), buy a dip:
    RSI below ``rsi_max`` OR z-score below ``z_max``."""
    trend = sma(df["close"], cfg["trend_sma"])
    r = rsi(df["close"], cfg["rsi_period"])
    z = zscore(df["close"], cfg["z_window"])
    dip = (r < float(cfg["rsi_max"])) | (z < float(cfg["z_max"]))
    cond = (df["close"] > trend) & dip
    return _as_signal(cond, df.index)


def momentum_score(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Ranking key, NOT a 0/1 signal: short_weight*pct_change(short) + long_weight*pct_change(long).
    Cross-sectional momentum needs the whole panel, so the planner applies it."""
    return (
        float(cfg["short_weight"]) * pct_change(df["close"], cfg["short"])
        + float(cfg["long_weight"]) * pct_change(df["close"], cfg["long"])
    )


def regime_on(benchmark_df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Boolean Series: is the benchmark above its ``regime.sma_days`` SMA?
    NaN (window not full) counts as OFF. Disabled -> all True."""
    rcfg = cfg["regime"]
    if not rcfg.get("enabled", True):
        return pd.Series(True, index=benchmark_df.index)
    trend = sma(benchmark_df["close"], rcfg["sma_days"])
    return (benchmark_df["close"] > trend).fillna(False).astype(bool)


# The 0/1 voters. momentum_score is deliberately absent — it is a ranking key.
STRATEGIES = {
    "donchian_breakout": donchian_breakout,
    "ema_trend": ema_trend,
    "dip_reversion": dip_reversion,
}


def _lookback(name: str, scfg: dict) -> int:
    """Longest window a strategy needs before it can have an opinion."""
    if name == "donchian_breakout":
        return max(int(scfg["entry_lookback"]) + 1, int(scfg["trend_sma"]))
    if name == "ema_trend":
        return int(scfg["slow"]) + int(scfg["slope_bars"])
    if name == "dip_reversion":
        return max(int(scfg["trend_sma"]), int(scfg["rsi_period"]) + 1, int(scfg["z_window"]))
    if name == "momentum_score":
        return max(int(scfg["short"]), int(scfg["long"]))
    return 0


def active_strategies(strat_cfg: dict) -> list[str]:
    """Voter names with weight > 0, in config order."""
    return [n for n in STRATEGIES if n in strat_cfg and float(strat_cfg[n].get("weight", 0)) > 0]


def warmup_bars(strat_cfg: dict) -> int:
    """No voter votes before EVERY active voter's window is full. Otherwise the early
    ensemble score is a vote among whichever strategies happen to be defined."""
    names = active_strategies(strat_cfg)
    return max([_lookback(n, strat_cfg[n]) for n in names] + [0])


def consecutive_valid(close: pd.Series) -> pd.Series:
    """Run length of real (non-NaN) closes ending at each bar. Resets to 0 at every gap."""
    ok = close.notna()
    groups = (~ok).cumsum()
    return ok.groupby(groups).cumsum().astype(int)


def signal_frame(df: pd.DataFrame, strat_cfg: dict) -> pd.DataFrame:
    """Run every weighted voter; one 0/1 column per strategy, aligned to df.index.

    A vote is forced to 0 unless the bar sits at the end of at least ``warmup_bars``
    CONSECUTIVE real bars. That covers the start of the series and every gap after a
    delisting: rolling windows go NaN through a hole on their own, but EWM-based
    indicators carry state across NaN and would otherwise vote on stale history."""
    out = pd.DataFrame(index=df.index)
    warm = warmup_bars(strat_cfg)
    ready = consecutive_valid(df["close"]) >= warm if warm > 0 else pd.Series(True, index=df.index)
    for name in active_strategies(strat_cfg):
        sig = STRATEGIES[name](df, strat_cfg[name])
        out[name] = sig.where(ready, 0).astype(int)
    return out


def ensemble_score(signals: pd.DataFrame, strat_cfg: dict) -> pd.Series:
    """Weighted vote share in 0..1: sum(weight_i * signal_i) / sum(weight_i)."""
    total = 0.0
    acc = pd.Series(0.0, index=signals.index)
    for name in signals.columns:
        w = float(strat_cfg[name]["weight"])
        acc = acc + w * signals[name].astype(float)
        total += w
    if total <= 0:
        return acc * 0.0
    return acc / total
