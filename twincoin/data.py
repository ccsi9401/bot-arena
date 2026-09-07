"""Bars: Alpaca daily + 4H (paginated, retried), Coinbase daily for the phantom-wick cross-check.

Alpaca bar timestamps are bar OPEN times. ``closed_only`` drops the forming bar so a daily
bar is used only after its 00:00 UTC close and a 4H bar only after its close.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from twincoin import credentials

ALPACA_BARS = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"
_RETRY = {429, 500, 502, 503, 504}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get(session: requests.Session, url: str, params: dict, headers: dict, dcfg: dict) -> requests.Response:
    last = None
    for attempt in range(int(dcfg["retries"]) + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=float(dcfg["timeout_s"]))
            if r.status_code in _RETRY:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            if attempt >= int(dcfg["retries"]):
                break
            time.sleep(float(dcfg["backoff_base_s"]) * (2 ** attempt))
    raise RuntimeError(f"request failed after retries: {last}")


def _headers(cfg: dict) -> dict:
    k, s = credentials(cfg)
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s} if k and s else {}


def _frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_alpaca(symbols: list[str], timeframe: str, start: datetime, cfg: dict,
                 session: requests.Session | None = None) -> dict[str, pd.DataFrame]:
    sess = session or requests.Session()
    out: dict[str, list] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "limit": 10000, "sort": "asc"}
    token = None
    while True:
        if token:
            params["page_token"] = token
        j = _get(sess, ALPACA_BARS, params, _headers(cfg), cfg["data"]).json()
        for s, rows in (j.get("bars") or {}).items():
            out.setdefault(s, []).extend(rows)
        token = j.get("next_page_token")
        if not token:
            break
    return {s: _frame(rows) for s, rows in out.items()}


def fetch_coinbase_daily(symbol: str, start: datetime, cfg: dict, session: requests.Session | None = None) -> pd.DataFrame | None:
    """Daily candles from Coinbase's public endpoint, 300 per request. None on failure (caller falls back)."""
    product = symbol.replace("/", "-")
    sess = session or requests.Session()
    rows = []
    s = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = utcnow()
    try:
        while s < end:
            t = min(s + timedelta(days=300), end)
            r = _get(sess, COINBASE_CANDLES.format(product=product),
                     {"granularity": 86400, "start": s.isoformat(), "end": t.isoformat()}, {}, cfg["data"])
            rows += r.json()
            s = t
            time.sleep(0.2)
    except Exception:
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"]).drop_duplicates("ts")
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)


def closed_only(df: pd.DataFrame, bar: timedelta, now: datetime | None = None) -> pd.DataFrame:
    """Keep bars whose close time (open + bar) is at or before now."""
    now = now or utcnow()
    return df[df.index + bar <= pd.Timestamp(now)]


def load_market(cfg: dict, now: datetime | None = None, session: requests.Session | None = None) -> tuple[dict, dict, dict]:
    """(daily, h4, notes). daily/h4: {symbol: closed-bar DataFrame}. notes: what was sanitized."""
    now = now or utcnow()
    syms = list(cfg["universe"]["symbols"])
    d_start = datetime.fromisoformat(cfg["data"]["daily_start"]).replace(tzinfo=timezone.utc)
    h_start = now - timedelta(days=int(cfg["data"]["h4_lookback_days"]))
    daily = fetch_alpaca(syms, "1Day", d_start, cfg, session)
    h4 = fetch_alpaca(syms, "4Hour", h_start, cfg, session)
    notes = {}
    from twincoin.strategy import sanitize_daily
    for s in syms:
        daily[s] = closed_only(daily[s], timedelta(days=1), now)
        h4[s] = closed_only(h4[s], timedelta(hours=4), now)
        cb = fetch_coinbase_daily(s, d_start, cfg, session) if cfg["data"].get("coinbase_sanitize", True) else None
        before = daily[s][["high", "low"]].copy()
        daily[s] = sanitize_daily(daily[s], cb)
        # count only material clips (>1% of price); venues differ by a few dollars on most bars
        phantom = int(((before["low"] < daily[s]["low"] * 0.99) | (before["high"] > daily[s]["high"] * 1.01)).sum())
        notes[s] = {"coinbase": cb is not None, "phantom_wicks_clipped": phantom, "daily_bars": len(daily[s]), "h4_bars": len(h4[s])}
    return daily, h4, notes
