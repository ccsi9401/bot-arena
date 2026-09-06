"""Alpaca crypto bars.

``fetch_bars`` hits https://data.alpaca.markets/v1beta3/crypto/us/bars, follows
``next_page_token`` pagination, retries with exponential backoff, and returns
``{symbol: DataFrame[open, high, low, close, volume]}`` indexed by UTC timestamp
ascending. API-key headers are sent when TALON_API_KEY / TALON_API_SECRET are set
(crypto bars are also served without them). ``align`` trims every frame to the
intersection of their indices so bar ``i`` means the same timestamp everywhere.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import requests

from talon import credentials, load_config

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TF_UNITS = {"MIN": "minutes", "H": "hours", "D": "days", "W": "weeks"}


def timeframe_delta(timeframe: str) -> timedelta:
    """'1D' -> 1 day, '4H' -> 4 hours, '15Min' -> 15 minutes."""
    m = re.fullmatch(r"(\d+)\s*([A-Za-z]+)", timeframe.strip())
    if not m:
        raise ValueError(f"unrecognised timeframe {timeframe!r}")
    n, unit = int(m.group(1)), m.group(2).upper()
    if unit not in _TF_UNITS:
        raise ValueError(f"unrecognised timeframe unit in {timeframe!r}")
    return timedelta(**{_TF_UNITS[unit]: n})


def _headers(cfg: dict | None) -> dict:
    key, secret = credentials(cfg)
    if key and secret:
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    return {}


def _get_with_retry(session: requests.Session, url: str, params: dict, headers: dict, dcfg: dict) -> dict:
    retries = int(dcfg["retries"])
    base = float(dcfg["backoff_base_s"])
    timeout = float(dcfg["timeout_s"])
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code in _RETRY_STATUSES:
                raise requests.HTTPError(f"HTTP {resp.status_code} from bars endpoint")
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError, ValueError) as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(base * (2 ** attempt))
    raise RuntimeError(f"bars request failed after {retries + 1} attempts: {last_err}")


def _to_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_bars(symbols: Iterable[str], timeframe: str, lookback_bars: int,
               data_cfg: dict | None = None, cfg: dict | None = None,
               session: requests.Session | None = None) -> dict[str, pd.DataFrame]:
    """Fetch the last ``lookback_bars`` bars of ``timeframe`` for every symbol."""
    if cfg is None:
        cfg = load_config()
    dcfg = data_cfg or cfg["data"]
    symbols = list(symbols)
    if not symbols:
        return {}
    sess = session or requests.Session()
    headers = _headers(cfg)
    start = datetime.now(timezone.utc) - timeframe_delta(timeframe) * int(lookback_bars)
    params = {
        "symbols": ",".join(symbols),
        "timeframe": timeframe,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": int(dcfg["page_limit"]),
        "sort": "asc",
    }
    rows: dict[str, list[dict]] = {s: [] for s in symbols}
    token: str | None = None
    while True:
        p = dict(params)
        if token:
            p["page_token"] = token
        payload = _get_with_retry(sess, dcfg["base_url"], p, headers, dcfg)
        for sym, bars in (payload.get("bars") or {}).items():
            rows.setdefault(sym, []).extend(bars or [])
        token = payload.get("next_page_token")
        if not token:
            break
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = _to_frame(rows.get(sym, []))
        out[sym] = df.tail(int(lookback_bars))
    return out


def align(bars: dict[str, pd.DataFrame], mode: str = "intersection") -> dict[str, pd.DataFrame]:
    """Put every frame on one calendar so bar ``i`` means the same timestamp everywhere.

    intersection   trim to the common timestamps (a late listing shortens everyone).
    point_in_time  union calendar; NaN where a name has no bar. Indicators stay NaN through
                   a gap and recover as their windows refill; the planner requires min_bars
                   of VALID bars before a name is scored. Standard point-in-time universe.
    """
    frames = {s: df for s, df in bars.items() if df is not None and len(df) > 0}
    if not frames:
        return {}
    if mode == "point_in_time":
        calendar = None
        for df in frames.values():
            calendar = df.index if calendar is None else calendar.union(df.index)
        calendar = calendar.sort_values()
        return {s: df.reindex(calendar).astype(float) for s, df in frames.items()}
    if mode != "intersection":
        raise ValueError(f"unknown alignment mode {mode!r}")
    common = None
    for df in frames.values():
        common = df.index if common is None else common.intersection(df.index)
    common = common.sort_values()
    return {s: df.loc[common].copy() for s, df in frames.items()}


def valid_bars(df: pd.DataFrame) -> pd.Series:
    """Cumulative count of bars with a real close — the point-in-time eligibility counter."""
    return df["close"].notna().cumsum()


def drop_incomplete_bar(bars: dict[str, pd.DataFrame], timeframe: str,
                        now: datetime | None = None) -> dict[str, pd.DataFrame]:
    """Drop the trailing bar if it is still in progress (its period has not ended).
    Signals are computed on completed bars only — mirrors the backtest's bar-i / open-i+1 rule."""
    now = now or datetime.now(timezone.utc)
    delta = timeframe_delta(timeframe)
    out = {}
    for s, df in bars.items():
        if len(df) and df.index[-1] + delta > now:
            out[s] = df.iloc[:-1].copy()
        else:
            out[s] = df
    return out
