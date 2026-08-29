"""Stage 1 — Scanner.

Turns raw market data into a feature snapshot for every symbol in the universe.
Strategy-agnostic: it computes features, it does not pick trades. Knows nothing
about the account. Output is written to the journal as scan.json and is the ONLY
market data the analyzer is allowed to see.
"""
from __future__ import annotations

import pandas as pd

from .data import MarketData
from .common import now_et
from . import indicators as ind
from . import markov2


def _sym_df(bars: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    try:
        df = bars.xs(symbol, level="symbol")
        return df if len(df) else None
    except KeyError:
        return None


def scan(data: MarketData, cfg: dict, mode: str) -> dict:
    """mode: 'daily' (swing) or 'intraday' (adds session features)."""
    uni = cfg["universe"]
    symbols = sorted(set(uni["stocks"] + uni["etfs"]))
    benchmark = uni["benchmark"]

    daily = data.daily_bars(symbols)
    snapshot: dict[str, dict] = {}

    for sym in symbols:
        df = _sym_df(daily, sym)
        if df is None or len(df) < 210:
            continue
        close = df["close"]
        adv20 = float((close * df["volume"]).tail(20).mean())
        feats = {
            "close": float(close.iloc[-1]),
            "last_bar_date": str(df.index[-1].date()),
            "sma50": float(ind.sma(close, 50).iloc[-1]),
            "sma200": float(ind.sma(close, 200).iloc[-1]),
            "ema20": float(ind.ema(close, 20).iloc[-1]),
            "rsi2": float(ind.rsi(close, 2).iloc[-1]),
            "atr14": float(ind.atr(df, 14).iloc[-1]),
            "avg_dollar_vol_20d": adv20,
            "pct_below_52wk_high": ind.pct_from_52wk_high(close),
            "low_today": float(df["low"].iloc[-1]),
            "avg_vol_20d": float(df["volume"].tail(20).mean()),
            "is_etf": sym in uni["etfs"],
        }
        snapshot[sym] = feats

    if cfg["strategy"].get("regime_filter") == "markov2" and benchmark in snapshot:
        _add_markov2_regime(data, benchmark, snapshot)

    if mode == "intraday":
        _add_session_features(data, cfg, snapshot, benchmark)

    return {
        "mode": mode,
        "asof_et": now_et().isoformat(),
        "benchmark": benchmark,
        "universe_size": len(symbols),
        "scanned": len(snapshot),
        "symbols": snapshot,
    }


def _add_markov2_regime(data: MarketData, benchmark: str, snapshot: dict) -> None:
    """Markov 2.0 signal for the benchmark — needs one extra long-history request,
    because the default 320-day window is far too short for a stride-sampled
    transition matrix. Leaves the snapshot untouched (analyzer falls back to the
    200SMA gate) if history is short or the matrix immature."""
    bdf = _sym_df(data.daily_bars([benchmark], days=markov2.HISTORY_DAYS), benchmark)
    m = markov2.latest_signal(bdf["close"]) if bdf is not None else None
    if m:
        snapshot[benchmark]["markov2_signal"] = m["signal"]
        snapshot[benchmark]["markov2_state"] = m["state"]
        snapshot[benchmark]["markov2_n_transitions"] = m["n_transitions"]


def _add_session_features(data: MarketData, cfg: dict, snapshot: dict, benchmark: str) -> None:
    syms = list(snapshot.keys())
    intraday = data.minute_bars_today(syms)
    or_minutes = cfg["strategy"].get("opening_range_minutes", 60)
    elapsed_min = max(1.0, (now_et() - now_et().replace(hour=9, minute=30, second=0,
                                                        microsecond=0)).total_seconds() / 60)

    bench_ret = None
    bdf = _sym_df(intraday, benchmark)
    if bdf is not None and len(bdf):
        bench_ret = float(bdf["close"].iloc[-1] / bdf["open"].iloc[0] - 1)

    rets = {}
    for sym in syms:
        df = _sym_df(intraday, sym)
        if df is None or len(df) < 2:
            snapshot[sym]["session"] = None
            continue
        n_or = max(1, or_minutes // 5)
        or_bars = df.iloc[:n_or]
        last = float(df["close"].iloc[-1])
        session_open = float(df["open"].iloc[0])
        ret_since_open = last / session_open - 1
        rets[sym] = ret_since_open
        vol_so_far = float(df["volume"].sum())
        expected_frac = min(1.0, elapsed_min / 390)
        avg_vol = snapshot[sym]["avg_vol_20d"]
        snapshot[sym]["session"] = {
            "last": last,
            "last_bar_end_et": str(df.index[-1]),
            "open": session_open,
            "or_high": float(or_bars["high"].max()),
            "or_low": float(or_bars["low"].min()),
            "vwap": ind.session_vwap(df),
            "ret_since_open": ret_since_open,
            "rel_ret_vs_bench": (ret_since_open - bench_ret) if bench_ret is not None else None,
            "volume_pace": (vol_so_far / (avg_vol * expected_frac)) if avg_vol > 0 else 0.0,
        }

    # relative-strength percentile across everything that traded today
    if rets:
        ranked = pd.Series(rets).rank(pct=True)
        for sym, pct in ranked.items():
            if snapshot[sym].get("session"):
                snapshot[sym]["session"]["rs_percentile"] = float(pct)
