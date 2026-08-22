#!/usr/bin/env python3
"""STEWARD pre-launch validation — replays the LIVE analyzer AND the LIVE planner
weekly over ~3 years of daily data (yfinance, runs on the GitHub Actions runner).

Gate (must all pass before STEWARD may trade):
  - positive total return
  - max drawdown under 20%
  - annualized Sharpe >= 0.4
Also reported (not gated): the same figures for SPY buy-and-hold, so we know
whether the machinery adds anything over doing nothing, plus the average cash
weight AND its gap against the planner's own cash_target — the invariant that has
drifted twice now, so the gate should show it. Raw cash alone can't distinguish drag
from a legitimately defensive week, because cash_target itself rises when fewer than
six stocks qualify; the gap can.

2026-08-22: this file used to re-implement the sizing loop instead of calling
plan(). The copy floored every order to whole shares and dropped any gap under
one share, so the gate was validating a portfolio that behaved like the v1
planner — which is precisely how both cash-drag bugs cleared it. The rebalance
now goes through the real planner, so caps, the drift band, the cash floor, the
kill switch and the cash-drag sweep are all exercised by the gate.

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
from bots.portfolio.planner import plan
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
        # Same config key as the live scanner, so a swept value changes both together.
        n_sma = s.get("trend_sma_days", 200)
        sma200 = float(close.rolling(n_sma).mean().iloc[-1]) if len(close) >= n_sma else None
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


def fill(orders: list[dict], prices: dict[str, float], cash: float,
         shares: dict[str, float]) -> float:
    """Apply a planner order list to the simulated book. Sells first (the planner
    already orders them that way) so their proceeds fund the buys, same as live."""
    for o in orders:
        p = prices.get(o["symbol"])
        if not p:
            continue
        if o["side"] == "sell":
            qty = min(o["qty"] or 0.0, shares.get(o["symbol"], 0.0))
            if qty <= 0:
                continue
            cash += qty * p * (1 - SLIP)
            shares[o["symbol"]] = shares.get(o["symbol"], 0.0) - qty
            if shares[o["symbol"]] <= 1e-9:
                shares.pop(o["symbol"], None)
        else:
            # notional where the planner priced in dollars, shares otherwise
            want = o["notional"] if o["notional"] is not None else (o["qty"] or 0.0) * p
            qty = want / p
            cost = qty * p * (1 + SLIP)
            if cost > cash:                     # slippage can push a funded plan over
                qty, cost = cash / (p * (1 + SLIP)), cash
            if qty <= 0:
                continue
            cash -= cost
            shares[o["symbol"]] = shares.get(o["symbol"], 0.0) + qty
    return cash


def run(data: dict[str, pd.DataFrame], cfg: dict, start_equity: float | None = None):
    # Take the size from config. Hardcoding it meant the gate simulated a $50k book
    # no matter what steward.yaml said — and since the planner measures inception
    # drawdown against cfg["starting_equity"], a mismatched pair silently disables
    # the kill switch inside the very backtest that is supposed to exercise it.
    start_equity = float(start_equity or cfg["starting_equity"])
    days = sorted(set().union(*[set(df.index) for df in data.values()]))
    days = [d for d in days if d in data[cfg["benchmark"]].index]
    warm = cfg["strategy"]["momentum_lookback_days"] + 30
    days = days[warm:]
    fridays = [d for d in days if d.weekday() == 4]

    cash = start_equity
    shares: dict[str, float] = {}
    curve, cash_weights, cash_gaps = {}, [], []
    rebalances = 0
    kill = False
    peak = start_equity

    def px(sym, d):
        return float(data[sym].loc[d, "close"]) if d in data[sym].index else None

    for day in days:
        if day in fridays:
            scan = build_scan(data, day, cfg)
            analysis = analyze(scan, cfg)
            equity = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
            prices = {s: px(s, day) for s in set(shares) | set(analysis["targets"])}
            prices = {s: p for s, p in prices.items() if p}
            positions = [{"symbol": s, "qty": q, "avg_entry": prices[s],
                          "market_value": q * prices[s], "unrealized_pl": 0.0,
                          "current_price": prices[s]}
                         for s, q in shares.items() if s in prices]
            acct = {"equity": equity, "cash": cash, "last_equity": equity,
                    "buying_power": cash, "status": "ACTIVE"}
            # Alpaca fills these names fractionally, so the live path is notional.
            p = plan(analysis["targets"], analysis, acct, positions, prices, cfg,
                     kill_tripped=kill, fractionable={s: True for s in prices},
                     peak_equity=peak)
            if any("KILL" in h for h in p["halts"]):
                kill = True                     # latches, exactly as live state does
            cash = fill(p["orders"], prices, cash, shares)
            rebalances += len(p["orders"])
            # The invariant worth grading: cash against the planner's OWN target for
            # that week, not raw cash. cash_target legitimately rises when fewer than
            # six stocks qualify, so raw cash can't tell drag from a defensive week.
            post = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
            if post > 0:
                cash_gaps.append(cash / post - p["cash_target"])
        eq = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
        curve[day] = eq
        peak = max(peak, eq)
        if eq > 0:
            cash_weights.append(cash / eq)

    stats = {
        "avg_cash_weight_pct": round(float(np.mean(cash_weights)) * 100, 2)
        if cash_weights else None,
        "cash_gap_mean_pp": round(float(np.mean(cash_gaps)) * 100, 2) if cash_gaps else None,
        "cash_gap_worst_pp": round(float(np.max(cash_gaps)) * 100, 2) if cash_gaps else None,
        "weeks_cash_over_target_1pp": int(sum(1 for g in cash_gaps if g > 0.01)),
        "rebalance_weeks": len(cash_gaps),
    }
    return pd.Series(curve).sort_index(), rebalances, stats


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
    period = cfg.get("backtest_period", "4y")
    print(f"Fetching daily history ({period})...")
    data = bd.daily_history(symbols, period)
    print(f"  {len(data)} symbols")

    curve, rebalances, cash_stats = run(data, cfg)
    s = summarize(curve, "STEWARD 3y weekly")
    s.update(cash_stats)
    # Record the window. backtest/cache/ is gitignored, so each CI run pulls whatever
    # yfinance serves that day: two runs a week apart are NOT comparing like with like,
    # and without these dates there is no way to tell a strategy change from a data shift.
    s["window_start"] = str(curve.index[0].date())
    s["window_end"] = str(curve.index[-1].date())
    s["trading_days"] = int(len(curve))
    spy = summarize(data[cfg["benchmark"]].loc[curve.index[0]:curve.index[-1]]["close"],
                    "SPY buy & hold (same window)")

    gate = {"positive_return": bool(s["total_return_pct"] > 0),
            "max_dd_under_20pct": bool(abs(s["max_drawdown_pct"]) < 20),
            "sharpe_at_least_0_4": bool((s["sharpe_daily_ann"] or 0) >= 0.4)}
    passed = all(gate.values())

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "steward.json").write_text(json.dumps(
        {"summary": s, "benchmark": spy, "gate": gate, "passed": passed,
         "rebalance_trades": rebalances, "planner_driven": True}, indent=2, default=str))
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
