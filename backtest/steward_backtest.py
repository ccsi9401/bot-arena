#!/usr/bin/env python3
"""STEWARD pre-launch validation — replays the LIVE analyzer weekly over ~3 years
of daily data (yfinance, runs on the GitHub Actions runner).

Gate (must all pass before STEWARD may trade):
  - positive total return
  - max drawdown under 20%
  - annualized Sharpe >= 0.4
Also reported (not gated): the same figures for SPY buy-and-hold, so we know
whether the machinery adds anything over doing nothing.

On success the workflow commits state/steward/gate.json — run_steward.py refuses
to trade until that file exists. Fills at close +5 bps each way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from core.common import ROOT
from bots.portfolio.analyzer import analyze
from backtest import data as bd

OUT = ROOT / "reports" / "backtest"
SLIP = 0.0005


def build_scan(data: dict[str, pd.DataFrame], day, cfg: dict) -> dict:
    s = cfg["strategy"]
    uni = cfg["universe"]
    snapshot = {}
    for sym, df in data.items():
        hist = df.loc[:day]
        if len(hist) < 60 or hist.index[-1] != day:
            continue
        close = hist["close"]
        lb, skip = s["momentum_lookback_days"], s["momentum_skip_days"]
        sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        rets = close.pct_change().dropna()
        snapshot[sym] = {
            "close": float(close.iloc[-1]),
            "last_bar_date": str(day.date()),
            "mom_12_1": float(close.iloc[-skip] / close.iloc[-lb] - 1)
            if len(close) >= lb else None,
            "mom_6m": float(close.iloc[-1] / close.iloc[-s["index_trend_lookback_days"]] - 1)
            if len(close) >= s["index_trend_lookback_days"] else None,
            "sma200": sma200,
            "above_200sma": bool(sma200 and close.iloc[-1] > sma200),
            "vol_63d_ann": float(rets.tail(63).std() * np.sqrt(252)) if len(rets) >= 30 else None,
            "avg_dollar_vol_20d": float((close * hist["volume"]).tail(20).mean()),
            "sleeve": ("stock" if sym in uni["stocks"] else
                       "index" if sym in uni["index_etfs"] else "defensive"),
        }
    return {"mode": "portfolio", "asof_et": str(day), "benchmark": cfg["benchmark"],
            "universe_size": len(data), "scanned": len(snapshot), "symbols": snapshot}


def run(data: dict[str, pd.DataFrame], cfg: dict, start_equity=50000.0):
    days = sorted(set().union(*[set(df.index) for df in data.values()]))
    days = [d for d in days if d in data[cfg["benchmark"]].index]
    warm = cfg["strategy"]["momentum_lookback_days"] + 30
    days = days[warm:]
    fridays = [d for d in days if d.weekday() == 4]

    cash = start_equity
    shares: dict[str, float] = {}
    curve = {}
    rebalances = 0

    def px(sym, d):
        return float(data[sym].loc[d, "close"]) if d in data[sym].index else None

    for day in days:
        if day in fridays:
            scan = build_scan(data, day, cfg)
            targets = analyze(scan, cfg)["targets"]
            equity = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
            # cap check mirrors planner
            r = cfg["risk"]
            targets = {s: min(w, r["max_position_weight"]) for s, w in targets.items()}
            band = cfg["strategy"]["drift_band_abs"]
            for sym in sorted(set(shares) | set(targets)):
                p = px(sym, day)
                if not p:
                    continue
                cur_w = shares.get(sym, 0) * p / equity
                tgt_w = targets.get(sym, 0)
                if abs(tgt_w - cur_w) <= band:
                    continue
                delta_val = (tgt_w - cur_w) * equity
                qty = int(abs(delta_val) / p)
                if qty < 1:
                    continue
                if delta_val < 0:
                    qty = min(qty, int(shares.get(sym, 0)))
                    cash += qty * p * (1 - SLIP)
                    shares[sym] = shares.get(sym, 0) - qty
                    if shares[sym] <= 0:
                        shares.pop(sym, None)
                else:
                    cost = qty * p * (1 + SLIP)
                    if cost > cash:
                        qty = int(cash / (p * (1 + SLIP)))
                        cost = qty * p * (1 + SLIP)
                    if qty >= 1:
                        cash -= cost
                        shares[sym] = shares.get(sym, 0) + qty
                rebalances += 1
        curve[day] = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())

    return pd.Series(curve).sort_index(), rebalances


def summarize(curve: pd.Series, label: str) -> dict:
    rets = curve.pct_change().dropna()
    dd = float((curve / curve.cummax() - 1).min() * 100)
    years = len(curve) / 252
    return {"label": label,
            "total_return_pct": round((curve.iloc[-1] / curve.iloc[0] - 1) * 100, 2),
            "cagr_pct": round(((curve.iloc[-1] / curve.iloc[0]) ** (1 / max(years, 0.1)) - 1) * 100, 2),
            "max_drawdown_pct": round(dd, 2),
            "sharpe_daily_ann": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2)
            if rets.std() > 0 else None}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "steward.yaml").read_text())
    uni = cfg["universe"]
    symbols = sorted(set(uni["stocks"] + uni["index_etfs"] + uni["defensive_etfs"]))
    print("Fetching daily history (4y)...")
    data = bd.daily_history(symbols, "4y")
    print(f"  {len(data)} symbols")

    curve, rebalances = run(data, cfg)
    s = summarize(curve, "STEWARD 3y weekly")
    spy = summarize(data[cfg["benchmark"]].loc[curve.index[0]:curve.index[-1]]["close"],
                    "SPY buy & hold (same window)")

    gate = {"positive_return": s["total_return_pct"] > 0,
            "max_dd_under_20pct": abs(s["max_drawdown_pct"]) < 20,
            "sharpe_at_least_0_4": (s["sharpe_daily_ann"] or 0) >= 0.4}
    passed = all(gate.values())

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "steward.json").write_text(json.dumps(
        {"summary": s, "benchmark": spy, "gate": gate, "passed": passed,
         "rebalance_trades": rebalances}, indent=2, default=str))
    md = [f"# STEWARD pre-launch validation\n", f"## {s['label']}\n"]
    for k, v in s.items():
        if k != "label":
            md.append(f"- {k}: {v}")
    md.append(f"- rebalance_trades: {rebalances}")
    md.append(f"\n## {spy['label']}\n")
    for k, v in spy.items():
        if k != "label":
            md.append(f"- {k}: {v}")
    md.append(f"\n**GATE: {'PASSED' if passed else 'FAILED'}** {gate}\n")
    (OUT / "steward_summary.md").write_text("\n".join(md))
    print("\n".join(md))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
