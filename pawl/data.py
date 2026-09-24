"""Offline data loading. Live bars come from whichever broker adapter is
configured (every Broker implements daily_bars), so there is exactly one
fetch path per venue and no second copy here."""
from __future__ import annotations

import glob
import os
from typing import Dict

import pandas as pd


def load_csvs(folder: str) -> Dict[str, pd.DataFrame]:
    """Backtest from local CSVs: one file per symbol, columns
    date,open,high,low,close,volume. Filenames use '-' for '/' (BTC-USD.csv)."""
    out: Dict[str, pd.DataFrame] = {}
    for path in glob.glob(os.path.join(folder, "*.csv")):
        sym = os.path.basename(path)[:-4].replace("-", "/")
        df = pd.read_csv(path)
        date_col = next(c for c in df.columns if c.lower() in ("date", "time", "timestamp", "t"))
        df[date_col] = pd.to_datetime(df[date_col], utc=True)
        df = df.set_index(date_col).sort_index()
        df.columns = [c.lower() for c in df.columns]
        need = ["open", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in need):
            continue
        out[sym] = df[need].astype(float)
    return out
