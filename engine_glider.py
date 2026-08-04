"""GLIDER backtest — replays the LIVE analyzer (bots/swing/analyzer.py) over history.

For every trading day it builds the exact scan-snapshot dict the live scanner would
emit, calls the live analyze(), and simulates fills. The alpha code path is therefore
identical to production; only broker mechanics are simulated:
  - entry at day close + slippage (live enters at 15:30 near close)
  - stop/target checked intrabar on later days, CONSERVATIVE: gap-open, then stop
    before target when both are inside one bar
  - breakeven ratchet & time stop applied at the daily decision point, like live
Sizing mirrors the validator: 1% equity risk, max positions, cash-bounded.
"""
from __future__ import annotations

import pandas as pd

from core import indicators as ind
from bots.swing.analyzer import analyze

SLIP = 0.0005  # 5 bps each way


def _features(df: pd.DataFrame) -> dict | None:
    if len(df) < 210:
        return None
    close = df["close"]
    return {
        "close": float(close.iloc[-1]),
        "last_bar_date": str(df.index[-1].date()),
        "sma50": float(ind.sma(close, 50).iloc[-1]),
        "sma200": float(ind.sma(close, 200).iloc[-1]),
        "ema20": float(ind.ema(close, 20).iloc[-1]),
        "rsi2": float(ind.rsi(close, 2).iloc[-1]),
        "atr14": float(ind.atr(df, 14).iloc[-1]),
        "avg_dollar_vol_20d": float((close * df["volume"]).tail(20).mean()),
        "pct_below_52wk_high": ind.pct_from_52wk_high(close),
        "low_today": float(df["low"].iloc[-1]),
        "avg_vol_20d": float(df["volume"].tail(20).mean()),
        "is_etf": False,
        "session": None,
    }


def run(data: dict[str, pd.DataFrame], cfg: dict, start_equity: float = 50000) -> tuple:
    cfg = dict(cfg)
    etfs = set(cfg["universe"]["etfs"])
    days = sorted(set().union(*[set(df.index) for df in data.values()]))
    days = [d for d in days if all(len(data[s].loc[:d]) >= 210
            for s in (cfg["universe"]["benchmark"],) if s in data)]

    cash = start_equity
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    curve = {}

    for day in days:
        # ---------- 1. resolve exits on today's bar (positions opened before today) ----------
        for sym in list(positions):
            p = positions[sym]
            if sym not in data or day not in data[sym].index:
                continue
            bar = data[sym].loc[day]
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
        snapshot = {}
        for sym, df in data.items():
            hist = df.loc[:day]
            if not len(hist) or hist.index[-1] != day:
                continue
            f = _features(hist)
            if f:
                f["is_etf"] = sym in etfs
                snapshot[sym] = f
        scan = {"mode": "daily", "asof_et": str(day), "benchmark": cfg["universe"]["benchmark"],
                "universe_size": len(data), "scanned": len(snapshot), "symbols": snapshot}
        open_ctx = [{"symbol": s, "entry": p["entry"], "stop": p["stop"],
                     "age_days": (day.date() - pd.Timestamp(p["opened"]).date()).days}
                    for s, p in positions.items()]
        result = analyze(scan, cfg, open_ctx)

        equity = cash + sum(p["qty"] * float(data[s].loc[day, "close"])
                            for s, p in positions.items()
                            if day in data[s].index)

        # ---------- 3. apply intents at today's close (mirrors validator arithmetic) ----------
        for intent in result["intents"]:
            a = intent["action"]
            if a == "close" and intent["symbol"] in positions:
                sym = intent["symbol"]; p = positions[sym]
                px = float(data[sym].loc[day, "close"]) * (1 - SLIP)
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
                entry = float(data[sym].loc[day, "close"]) * (1 + SLIP)
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

        curve[day] = cash + sum(p["qty"] * float(data[s].loc[day, "close"])
                                for s, p in positions.items() if day in data[s].index)

    return pd.Series(curve).sort_index(), trades
