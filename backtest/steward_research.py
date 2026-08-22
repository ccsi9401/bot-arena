#!/usr/bin/env python3
"""STEWARD research harness — the questions the GATE cannot answer.

The gate asks "does this strategy clear our thresholds?" and it now drives the
real planner, so that answer is trustworthy. But a passing gate says nothing
about WHY the strategy passed, and there are three uncomfortable possibilities
it cannot distinguish:

  1. The edge is real.
  2. The edge is the universe. `config/steward.yaml` lists the large caps of
     August 2026 and replays them through 2022. The strategy therefore only ever
     chose among companies that did well enough to still be on that list — it
     never had the chance to buy something that later cratered out of the index.
     PANW (joined the S&P 500 in Jun 2023), UBER (Dec 2023) and PLTR (Sep 2024)
     were not index members when the window opens.
  3. The edge is a handful of names. Momentum concentrates; if dropping the three
     best performers erases the outperformance, what we have is a lottery ticket
     with extra steps, not a strategy.

Each variant below isolates one of those. Nothing here touches live trading or
the gate — this file only reads config and prints a comparison.

Run: python backtest/steward_research.py
"""
from __future__ import annotations

import copy
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
import backtest.steward_backtest as _bt
from backtest.steward_backtest import build_scan, fill, summarize

OUT = ROOT / "reports" / "backtest"

# Universe names that were NOT S&P 500 members when the backtest window opens.
# Source: Wikipedia "Selected changes to the list of S&P 500 components".
JOINED_MID_WINDOW = {"PANW": "2023-06-20", "UBER": "2023-12-18", "PLTR": "2024-09-23"}


def _next_day_map(days: list) -> dict:
    return {d: days[i + 1] for i, d in enumerate(days[:-1])}


def run_variant(data, cfg, *, drop: set[str] | None = None, slip=0.0005,
                fill_next_open=False, start_equity=50000.0):
    """The gate's loop, with knobs. drop = symbols removed from the universe."""
    drop = drop or set()
    data = {s: df for s, df in data.items() if s not in drop}
    cfg = copy.deepcopy(cfg)
    cfg["universe"]["stocks"] = [s for s in cfg["universe"]["stocks"] if s not in drop]

    days = sorted(set().union(*[set(df.index) for df in data.values()]))
    days = [d for d in days if d in data[cfg["benchmark"]].index]
    days = days[cfg["strategy"]["momentum_lookback_days"] + 30:]
    fridays = [d for d in days if d.weekday() == 4]
    nxt = _next_day_map(days)

    cash, shares, curve = start_equity, {}, {}
    peak, kill, orders = start_equity, False, 0

    def px(sym, d, col="close"):
        df = data.get(sym)
        if df is None or d not in df.index:
            return None
        if col not in df.columns:          # some feeds omit open; close is the fallback
            col = "close"
        v = float(df.loc[d, col])
        return v if v > 0 else None

    for day in days:
        if day in fridays:
            analysis = analyze(build_scan(data, day, cfg), cfg)
            equity = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
            prices = {s: px(s, day) for s in set(shares) | set(analysis["targets"])}
            prices = {s: p for s, p in prices.items() if p}
            positions = [{"symbol": s, "qty": q, "avg_entry": prices[s],
                          "market_value": q * prices[s], "unrealized_pl": 0.0,
                          "current_price": prices[s]}
                         for s, q in shares.items() if s in prices]
            p = plan(analysis["targets"], analysis,
                     {"equity": equity, "cash": cash, "last_equity": equity,
                      "buying_power": cash, "status": "ACTIVE"},
                     positions, prices, cfg, kill_tripped=kill,
                     fractionable={s: True for s in prices}, peak_equity=peak)
            if any("KILL" in h for h in p["halts"]):
                kill = True
            # The planner prices off the last trade it can see; execution happens
            # later and at a different price. fill_next_open models that honestly
            # instead of pretending plan and fill are simultaneous.
            fill_px = prices
            if fill_next_open and day in nxt:
                nd = nxt[day]
                fill_px = {s: px(s, nd, "open") or prices[s] for s in prices}
            _saved, _bt.SLIP = _bt.SLIP, slip
            try:
                cash = fill(p["orders"], fill_px, cash, shares)
            finally:
                _bt.SLIP = _saved
            orders += len(p["orders"])
        eq = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
        curve[day] = eq
        peak = max(peak, eq)

    return pd.Series(curve).sort_index(), orders


def run_static(data, cfg, targets: dict, *, slip=0.0005, start_equity=50000.0):
    """No regime gate, no stock picking: fixed weights, same weekly cadence and
    drift band. The control that asks whether any of the machinery earns its keep."""
    days = sorted(set().union(*[set(df.index) for df in data.values()]))
    days = [d for d in days if d in data[cfg["benchmark"]].index]
    days = days[cfg["strategy"]["momentum_lookback_days"] + 30:]
    band = cfg["strategy"]["drift_band_abs"]
    cash, shares, curve, orders = start_equity, {}, {}, 0

    def px(sym, d):
        return float(data[sym].loc[d, "close"]) if d in data[sym].index else None

    for day in days:
        if day.weekday() == 4:
            equity = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
            for sym, tgt in targets.items():
                p = px(sym, day)
                if not p:
                    continue
                cur = shares.get(sym, 0) * p / equity if equity else 0
                if abs(tgt - cur) <= band:
                    continue
                delta = (tgt - cur) * equity
                if delta > 0:
                    spend = min(delta, cash / (1 + slip))
                    if spend <= 0:
                        continue
                    cash -= spend * (1 + slip)
                    shares[sym] = shares.get(sym, 0) + spend / p
                else:
                    q = min(abs(delta) / p, shares.get(sym, 0))
                    if q <= 0:
                        continue
                    cash += q * p * (1 - slip)
                    shares[sym] = shares.get(sym, 0) - q
                orders += 1
        curve[day] = cash + sum(q * (px(s, day) or 0) for s, q in shares.items())
    return pd.Series(curve).sort_index(), orders


def top_performers(data, cfg, n: int) -> set[str]:
    """The n best stock returns over the whole window — knowable only in hindsight,
    which is exactly the point of removing them."""
    rets = {}
    for s in cfg["universe"]["stocks"]:
        df = data.get(s)
        if df is None or len(df) < 2:
            continue
        rets[s] = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
    return set(sorted(rets, key=rets.get, reverse=True)[:n])


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "steward.yaml").read_text())
    uni = cfg["universe"]
    symbols = sorted(set(uni["stocks"] + uni["index_etfs"] + uni["defensive_etfs"]))
    print("Fetching daily history (4y)...")
    data = bd.daily_history(symbols, "4y")
    print(f"  {len(data)}/{len(symbols)} symbols returned data")
    missing = sorted(set(symbols) - set(data))
    if missing:
        print(f"  NOT FETCHABLE (residual survivorship bias lives here): {missing}")

    idx_cfg = copy.deepcopy(cfg)
    idx_cfg["strategy"]["weights"]["risk_on"] = {"stocks": 0.0, "index": 0.70,
                                                 "defensive": 0.20, "cash": 0.10}
    idx_cfg["risk"]["max_position_weight"] = 0.40   # two ETFs cannot fill 70% at a 12% cap

    top3 = top_performers(data, cfg, 3)
    frozen = set(JOINED_MID_WINDOW) & set(uni["stocks"])
    ballast = {d: 0.20 / len(uni["defensive_etfs"]) for d in uni["defensive_etfs"]}

    runs = []

    curves = {}

    def record(name, question, curve, orders, note=""):
        curves[name] = curve
        s = summarize(curve, name)
        s.update({"question": question, "orders": orders, "note": note})
        runs.append(s)
        print(f"  {name:<22} {s['total_return_pct']:>8.2f}%  "
              f"DD {s['max_drawdown_pct']:>7.2f}%  Sharpe {s['sharpe_daily_ann']:>5.2f}")
        return s

    print("\nRunning variants (each is a full 3y replay — this takes a few minutes):")
    base = record("baseline", "what the gate measures", *run_variant(data, cfg))
    record("index_only", "does stock picking beat the index at the same risk?",
           *run_variant(data, idx_cfg))
    record("static_70_20_10", "does ANY of the machinery earn its keep?",
           *run_static(data, cfg, dict({cfg["benchmark"]: 0.70}, **ballast)))
    record("frozen_universe", "how much is hindsight in the universe?",
           *run_variant(data, cfg, drop=frozen),
           note=f"dropped {sorted(frozen)} — joined the index mid-window")
    record("drop_top3", "is the edge broad, or three lucky names?",
           *run_variant(data, cfg, drop=top3),
           note=f"dropped {sorted(top3)} — best performers, knowable only after the fact")
    record("pessimistic_fills", "does the edge survive realistic execution?",
           *run_variant(data, cfg, slip=0.0025, fill_next_open=True),
           note="filled at next open, 25bps each way")
    record("honest_worst_case", "all of the above at once",
           *run_variant(data, cfg, drop=frozen | top3, slip=0.0025, fill_next_open=True))

    # SPY over exactly the baseline's span, so the comparison is like for like.
    bc = curves["baseline"]
    spy = summarize(data[cfg["benchmark"]]["close"].loc[bc.index[0]:bc.index[-1]],
                    "SPY buy & hold")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "steward_research.json").write_text(json.dumps(
        {"variants": runs, "benchmark": spy,
         "window": {"start": str(min(data[cfg["benchmark"]].index).date()),
                    "end": str(max(data[cfg["benchmark"]].index).date())},
         "unfetchable_symbols": missing}, indent=2, default=str))

    md = ["# STEWARD research — what the gate cannot tell you\n",
          f"Window: {min(data[cfg['benchmark']].index).date()} to "
          f"{max(data[cfg['benchmark']].index).date()}. "
          f"SPY buy & hold over the same span: {spy['total_return_pct']}%.\n",
          "| variant | question | return | max DD | Sharpe | vs baseline |",
          "|---|---|---|---|---|---|"]
    for r in runs:
        delta = r["total_return_pct"] - base["total_return_pct"]
        md.append(f"| `{r['label']}` | {r['question']} | {r['total_return_pct']}% | "
                  f"{r['max_drawdown_pct']}% | {r['sharpe_daily_ann']} | "
                  f"{delta:+.2f} pts |")
    md.append("")
    for r in runs:
        if r["note"]:
            md.append(f"- **{r['label']}**: {r['note']}")
    if missing:
        md.append(f"\n**Residual bias:** {len(missing)} symbol(s) could not be fetched "
                  f"and are silently absent from every run: {missing}. Delisted names "
                  "are the ones survivorship bias is made of, so treat these figures as "
                  "an upper bound on the true edge, not a measurement of it.")
    (OUT / "steward_research.md").write_text("\n".join(md))
    print("\n" + "\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
