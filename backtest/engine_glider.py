"""GLIDER backtest — replays the LIVE analyzer (bots/swing/analyzer.py) over history.

For every trading day it builds the exact scan-snapshot dict the live scanner would
emit, calls the live analyze(), and simulates fills. The alpha code path is therefore
identical to production; only broker mechanics are simulated:
  - entry at day close + slippage (live enters at 15:30 near close)
  - stop/target checked intrabar on later days, CONSERVATIVE: gap-open, then stop
    before target when both are inside one bar
  - breakeven ratchet, trailing stop & time stop applied at the daily decision point
Sizing mirrors the validator: risk_per_trade_pct of equity, max positions, cash-bounded.

Features are precomputed vectorised once per symbol (precompute()). Every indicator is
either a fixed rolling window or an EWM seeded from the start of the series, so the
value on day d is identical to the old per-day slice method — just ~100x faster, which
is what lets glider_learn.py sweep hundreds of candidate configs in one run.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import indicators as ind
from core import markov2
from bots.swing.analyzer import analyze

SLIP = 0.0005  # 5 bps each way
WARMUP = 210   # bars of history required before a symbol is eligible


def precompute(data: dict[str, pd.DataFrame], etfs: set[str]) -> dict:
    """Return {"feats": {sym: {day: feature_dict}}, "bars": {sym: DataFrame}, "days": [...]}."""
    feats: dict[str, dict] = {}
    for sym, df in data.items():
        if len(df) < WARMUP:
            continue
        close = df["close"]
        hi252 = close.rolling(252, min_periods=1).max()
        f = pd.DataFrame({
            "close": close,
            "sma50": ind.sma(close, 50),
            "sma200": ind.sma(close, 200),
            "ema20": ind.ema(close, 20),
            "rsi2": ind.rsi(close, 2),
            "atr14": ind.atr(df, 14),
            "avg_dollar_vol_20d": (close * df["volume"]).rolling(20).mean(),
            "pct_below_52wk_high": ((hi252 - close) / hi252 * 100).where(hi252 > 0, 100.0),
            "low_today": df["low"],
            "avg_vol_20d": df["volume"].rolling(20).mean(),
        }, index=df.index).iloc[WARMUP - 1:]
        recs = {}
        is_etf = sym in etfs
        # walk-forward Markov 2.0 signal for ETFs (the analyzer reads it off the
        # benchmark when regime_filter: markov2; days with an immature matrix get
        # no key, so the analyzer falls back to the 200SMA gate — same as live)
        mk: dict = {}
        if is_etf and len(close) > markov2.VOL_LOOKBACK + markov2.WINDOW:
            ms = markov2.signal_series(close)
            for mday, sig, st in zip(ms.index, ms["signal"], ms["state"]):
                if not np.isnan(sig):
                    mk[mday] = (float(sig), markov2.NAMES[int(st)])
        for day, row in zip(f.index, f.to_dict("records")):
            row["last_bar_date"] = str(day.date())
            row["is_etf"] = is_etf
            row["session"] = None
            if day in mk:
                row["markov2_signal"], row["markov2_state"] = mk[day]
            recs[day] = row
        feats[sym] = recs
    return {"feats": feats, "bars": data}


def run(data: dict[str, pd.DataFrame], cfg: dict, start_equity: float | None = None,
        pre: dict | None = None) -> tuple:
    cfg = dict(cfg)
    if start_equity is None:
        start_equity = float(cfg.get("starting_equity", 50000))
    etfs = set(cfg["universe"]["etfs"])
    bench = cfg["universe"]["benchmark"]
    if pre is None:
        pre = precompute(data, etfs)
    feats = pre["feats"]
    if bench not in feats:
        raise ValueError(f"benchmark {bench} lacks {WARMUP} bars of history")

    # trading days = every day the benchmark is eligible (mirrors the old filter)
    days = sorted(feats[bench].keys())
    bars = {s: df for s, df in data.items()}

    cash = start_equity
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    curve = {}

    for day in days:
        # ---------- 1. resolve exits on today's bar (positions opened before today) ----------
        for sym in list(positions):
            p = positions[sym]
            if sym not in bars or day not in bars[sym].index:
                continue
            bar = bars[sym].loc[day]
            exit_px = None; why = None
            if bar["open"] <= p["stop"]:
                exit_px, why = bar["open"], "gap_stop"
            elif bar["low"] <= p["stop"]:
                exit_px, why = p["stop"], "stop"
            elif bar["high"] >= p["target"]:
                exit_px, why = p["target"], "target"
            if exit_px is not None:
                exit_px *= (1 - SLIP)
                cash += p["qty"] * exit_px
                trades.append({"symbol": sym, "entry": p["entry"], "exit": exit_px,
                               "qty": p["qty"], "pnl": p["qty"] * (exit_px - p["entry"]),
                               "risk": p["risk"], "why": why,
                               "opened": p["opened"], "closed": str(day.date())})
                del positions[sym]

        # ---------- 2. build snapshot as of today's close; call LIVE analyzer ----------
        snapshot = {sym: recs[day] for sym, recs in feats.items() if day in recs}
        scan = {"mode": "daily", "asof_et": str(day), "benchmark": bench,
                "universe_size": len(data), "scanned": len(snapshot), "symbols": snapshot}
        open_ctx = [{"symbol": s, "entry": p["entry"], "stop": p["stop"],
                     "age_days": (day.date() - pd.Timestamp(p["opened"]).date()).days}
                    for s, p in positions.items()]
        result = analyze(scan, cfg, open_ctx)

        equity = cash + sum(p["qty"] * snapshot[s]["close"]
                            for s, p in positions.items() if s in snapshot)

        # ---------- 3. apply intents at today's close (mirrors validator arithmetic) ----------
        for intent in result["intents"]:
            a = intent["action"]
            if a == "close" and intent["symbol"] in positions:
                sym = intent["symbol"]; p = positions[sym]
                px = snapshot[sym]["close"] * (1 - SLIP)
                cash += p["qty"] * px
                trades.append({"symbol": sym, "entry": p["entry"], "exit": px,
                               "qty": p["qty"], "pnl": p["qty"] * (px - p["entry"]),
                               "risk": p["risk"], "why": "time_stop",
                               "opened": p["opened"], "closed": str(day.date())})
                del positions[sym]
            elif a == "raise_stop" and intent["symbol"] in positions:
                positions[intent["symbol"]]["stop"] = intent["new_stop"]
            elif a == "buy" and len(positions) < cfg["risk"]["max_positions"]:
                sym = intent["symbol"]
                entry = snapshot[sym]["close"] * (1 + SLIP)
                rps = entry - intent["stop"]
                if rps <= 0:
                    continue
                qty = int(equity * cfg["risk"]["risk_per_trade_pct"] / 100 / rps)
                qty = min(qty, int(cash / entry)) if entry > 0 else 0
                if qty < 1:
                    continue
                cash -= qty * entry
                positions[sym] = {"entry": entry, "stop": intent["stop"],
                                  "target": intent["target"], "qty": qty,
                                  "risk": qty * rps, "opened": str(day.date())}

        curve[day] = cash + sum(p["qty"] * snapshot[s]["close"]
                                for s, p in positions.items() if s in snapshot)

    return pd.Series(curve).sort_index(), trades
