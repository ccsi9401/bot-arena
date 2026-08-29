"""Markov 2.0 regime signal — stride-sampled transition matrix on the benchmark.

Used by GLIDER's `regime_filter: markov2` (FILTER mode: the signal gates entries,
the strategy stays the trend-pullback in bots/swing/analyzer.py).

Method (Markov 2.0 "hedge fund method", corrected):
  state  = 20-day return beyond a vol-scaled band -> BULL/BEAR, inside -> SIDEWAYS.
           Band = 1.1 x trailing 252d std of 20d returns, shifted 1 bar (past data
           only, no lookahead).
  matrix = state->state transition counts, rows normalised. Sampled at stride = 20
           bars (NON-overlapping windows): consecutive overlapping windows share
           19/20 days, which fakes persistence on the diagonal.
  signal = P(bull next) - P(bear next) from the current state's row, in [-1, +1].

Walk-forward: the matrix at day t is built from labels at or before t only. The
signal is None until the label warmup (252+20 bars) plus MIN_TRANSITIONS stride
samples exist — callers must fall back (analyzer falls back to the 200SMA gate)
rather than trade an immature matrix.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 20          # state window (bars) and matrix stride
VOL_MULT = 1.1       # band = VOL_MULT x trailing std of WINDOW-day returns
VOL_LOOKBACK = 252   # trailing std lookback (shifted 1 bar: past data only)
MIN_TRANSITIONS = 24 # stride samples needed before the signal is trusted (~2y)
HISTORY_DAYS = 1400  # trading days of benchmark history the live scanner fetches

SIDEWAYS, BULL, BEAR = 0, 1, 2
NAMES = {SIDEWAYS: "SIDEWAYS", BULL: "BULL", BEAR: "BEAR"}


def label_states(close: pd.Series) -> pd.Series:
    """Int state per day (0/1/2), only where the 20d return AND band are defined."""
    close = close.dropna().astype(float)
    ret20 = close / close.shift(WINDOW) - 1.0
    band = VOL_MULT * ret20.rolling(VOL_LOOKBACK).std().shift(1)
    valid = ret20.notna() & band.notna()
    codes = np.where(ret20 >= band, BULL, np.where(ret20 <= -band, BEAR, SIDEWAYS))
    return pd.Series(codes, index=close.index)[valid].astype(int)


def transition_matrix(seq: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(row-normalised probabilities, raw counts). Zero rows stay zero rows."""
    C = np.zeros((3, 3))
    for a, b in zip(seq[:-1], seq[1:]):
        C[a, b] += 1
    rows = C.sum(axis=1, keepdims=True)
    rows[rows == 0] = 1
    return C / rows, C


def signal_series(close: pd.Series) -> pd.DataFrame:
    """Walk-forward signal for every labelled day: columns signal/state/n_transitions.

    signal is NaN until MIN_TRANSITIONS stride samples exist. At day t the matrix
    uses labels[0 : t+1 : WINDOW] — only data at or before t.
    """
    labels = label_states(close)
    arr = labels.to_numpy()
    sig = np.full(len(arr), np.nan)
    ntr = np.zeros(len(arr), dtype=int)
    for t in range(len(arr)):
        sampled = arr[: t + 1 : WINDOW]
        ntr[t] = len(sampled) - 1
        if ntr[t] < MIN_TRANSITIONS:
            continue
        M, _ = transition_matrix(sampled)
        sig[t] = M[arr[t], BULL] - M[arr[t], BEAR]
    return pd.DataFrame({"signal": sig, "state": arr, "n_transitions": ntr},
                        index=labels.index)


def latest_signal(close: pd.Series) -> dict | None:
    """Today's signal for the live scanner, or None while the matrix is immature."""
    labels = label_states(close)
    if labels.empty:
        return None
    arr = labels.to_numpy()
    sampled = arr[::WINDOW]
    if len(sampled) - 1 < MIN_TRANSITIONS:
        return None
    M, C = transition_matrix(sampled)
    state = int(arr[-1])
    return {
        "signal": float(M[state, BULL] - M[state, BEAR]),
        "state": NAMES[state],
        "n_transitions": int(C.sum()),
        "asof": str(labels.index[-1].date()),
    }
