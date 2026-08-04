"""SCALPEL backtest — replays the LIVE analyzer (bots/intraday/analyzer.py) on hourly bars.

Hourly bars approximate the live 5-minute scan: the first hourly bar IS the 60-min
opening range; VWAP is cumulative typical-price VWAP over hourly bars; volume pace is
cumulative volume vs the 20-day average scaled by session fraction. Decision points are
bar closes at 10:30–14:30 ET (entries) and 15:30 (forced liquidation), exactly the live
cycle times. Fills: entry at decision-bar close + slippage; stop/target intrabar on
later bars, conservative (gap, then stop before target). Daily −2% breaker enforced.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import indicators as ind
from bots.intraday.analyzer import analyze

SLIP = 0.0005
CYCLES = ["10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]


def _daily_from_hourly(df: pd.DataFrame) -> pd.DataFrame:
    d = df.resample("1D").agg({"open": "first", "high": "max", "low": "min",
                               "close": "last", "volume": "sum"}).dropna()
    return d


def run(hourly: dict[str, pd.DataFrame], cfg: dict, start_equity: float = 50000) -> tuple:
    tz = "America/New_York"
    hourly = {s: (df.tz_convert(tz) if df.index.tz is not None else df.tz_localize(tz))
              for s, df in hourly.items()}
    daily = {s: _daily_from_hourly(df) for s, df in hourly.items()}
    bench = cfg["universe"]["benchmark"]
    etfs = set(cfg["universe"]["etfs"])

    all_days = sorted({ts.date() for df in hourly.values() for ts in df.index})
    warm = 30  # need 20d avg volume + ATR from daily aggregates
    cash = start_equity
    trades = []
    curve = {}

    for day in all_days[warm:]:
        day_bars = {s: df[df.index.date == day] for s, df in hourly.items()}
        day_bars = {s: b for s, b in day_bars.items() if len(b) >= 2}
        if bench not in day_bars:
            continue
        positions: dict[str, dict] = {}
        day_start_equity = cash
        halted = False

        for cyc in CYCLES:
            cutoff = pd.Timestamp(f"{day} {cyc}", tz=tz)
            # ---- resolve stops/targets on bars since last cycle ----
            for sym in list(positions):
                p = positions[sym]
                bars = day_bars[sym]
                seg = bars[(bars.index > p["last_checked"]) & (bars.index <= cutoff)]
                for ts, bar in seg.iterrows():
                    exit_px = None; why = None
                    if bar["open"] <= p["stop"]:
                        exit_px, why = bar["open"], "gap_stop"
                    elif bar["low"] <= p["stop"]:
                        exit_px, why = p["stop"], "stop"
                    elif bar["high"] >= p["target"]:
                        exit_px, why = p["target"], "target"
                    if exit_px:
                        exit_px *= (1 - SLIP)
                        cash += p["qty"] * exit_px
                        trades.append({"symbol": sym, "pnl": p["qty"] * (exit_px - p["entry"]),
                                       "risk": p["risk"], "why": why, "day": str(day)})
                        del positions[sym]
                        break
                if sym in positions:
                    positions[sym]["last_checked"] = cutoff

            # ---- mark equity; daily breaker ----
            def mark(sym, ts):
                bars = day_bars[sym][day_bars[sym].index <= ts]
                return float(bars["close"].iloc[-1]) if len(bars) else positions[sym]["entry"]
            equity = cash + sum(p["qty"] * mark(s, cutoff) for s, p in positions.items())
            limit = cfg["risk"].get("daily_loss_limit_pct")
            if limit and not halted and equity <= day_start_equity * (1 - limit / 100):
                halted = True
                for sym in list(positions):  # flatten
                    px = mark(sym, cutoff) * (1 - SLIP)
                    p = positions.pop(sym)
                    cash += p["qty"] * px
                    trades.append({"symbol": sym, "pnl": p["qty"] * (px - p["entry"]),
                                   "risk": p["risk"], "why": "daily_breaker", "day": str(day)})

            # ---- build snapshot at this cycle; call LIVE analyzer ----
            if halted:
                continue
            snapshot = {}
            bench_bars = day_bars[bench][day_bars[bench].index <= cutoff]
            bench_ret = float(bench_bars["close"].iloc[-1] / bench_bars["open"].iloc[0] - 1) \
                if len(bench_bars) else 0.0
            rets = {}
            elapsed = max(1.0, (cutoff - pd.Timestamp(f"{day} 09:30", tz=tz)).seconds / 60)
            for sym, bars_all in day_bars.items():
                dhist = daily[sym][daily[sym].index.date < day]
                if len(dhist) < 21:
                    continue
                bars = bars_all[bars_all.index <= cutoff]
                if len(bars) < 1:
                    continue
                close = dhist["close"]
                typical = (bars["high"] + bars["low"] + bars["close"]) / 3
                vsum = bars["volume"].sum()
                last = float(bars["close"].iloc[-1])
                r = last / float(bars["open"].iloc[0]) - 1
                rets[sym] = r
                avg_vol = float(dhist["volume"].tail(20).mean())
                snapshot[sym] = {
                    "close": float(close.iloc[-1]),
                    "last_bar_date": str(dhist.index[-1].date()),
                    "sma50": float(ind.sma(close, 50).iloc[-1]) if len(close) >= 50 else 0.0,
                    "sma200": float(ind.sma(close, 200).iloc[-1]) if len(close) >= 200 else 0.0,
                    "ema20": float(ind.ema(close, 20).iloc[-1]),
                    "rsi2": float(ind.rsi(close, 2).iloc[-1]),
                    "atr14": float(ind.atr(dhist, 14).iloc[-1]),
                    "avg_dollar_vol_20d": float((close * dhist["volume"]).tail(20).mean()),
                    "pct_below_52wk_high": ind.pct_from_52wk_high(close),
                    "low_today": float(bars["low"].min()),
                    "avg_vol_20d": avg_vol,
                    "is_etf": sym in etfs,
                    "session": {
                        "last": last,
                        "last_bar_end_et": str(bars.index[-1]),
                        "open": float(bars["open"].iloc[0]),
                        "or_high": float(bars_all.iloc[0]["high"]),
                        "or_low": float(bars_all.iloc[0]["low"]),
                        "vwap": float((typical * bars["volume"]).sum() / vsum) if vsum > 0 else last,
                        "ret_since_open": r,
                        "rel_ret_vs_bench": r - bench_ret,
                        "volume_pace": float(vsum / (avg_vol * min(1.0, elapsed / 390)))
                        if avg_vol > 0 else 0.0,
                    },
                }
            if rets:
                ranked = pd.Series(rets).rank(pct=True)
                for sym, pct in ranked.items():
                    snapshot[sym]["session"]["rs_percentile"] = float(pct)

            scan = {"mode": "intraday", "asof_et": str(cutoff), "benchmark": bench,
                    "universe_size": len(hourly), "scanned": len(snapshot),
                    "symbols": snapshot}
            result = analyze(scan, cfg, cyc)

            for intent in result["intents"]:
                if intent["action"] == "liquidate_all":
                    for sym in list(positions):
                        px = mark(sym, cutoff) * (1 - SLIP)
                        p = positions.pop(sym)
                        cash += p["qty"] * px
                        trades.append({"symbol": sym, "pnl": p["qty"] * (px - p["entry"]),
                                       "risk": p["risk"], "why": "eod", "day": str(day)})
                elif intent["action"] == "buy" and intent["symbol"] not in positions \
                        and len(positions) < cfg["risk"]["max_positions"]:
                    sym = intent["symbol"]
                    entry = snapshot[sym]["session"]["last"] * (1 + SLIP)
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
                                      "risk": qty * rps, "last_checked": cutoff}

        # safety: anything left after 15:30 cycle closes at day's last bar
        for sym in list(positions):
            p = positions.pop(sym)
            px = float(day_bars[sym]["close"].iloc[-1]) * (1 - SLIP)
            cash += p["qty"] * px
            trades.append({"symbol": sym, "pnl": p["qty"] * (px - p["entry"]),
                           "risk": p["risk"], "why": "eod_sweep", "day": str(day)})
        curve[pd.Timestamp(day)] = cash

    return pd.Series(curve).sort_index(), trades
