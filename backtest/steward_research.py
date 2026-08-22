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

# When each universe name joined the S&P 500, from Wikipedia's "Selected changes to
# the list of S&P 500 components". Which of these count as mid-window depends on where
# the window actually starts, so it is computed at runtime rather than hardcoded — the
# window moves when backtest_period changes, and a stale list silently drops names that
# were legitimately members all along (PANW joined Jun 2023; under a window opening in
# Oct 2023 it is NOT a mid-window joiner, though under a 10y window it is).
#
# Caveat: this log begins in 2014. Anything absent is treated as a member throughout,
# which is right for the old mega-caps and wrong for anything that joined pre-2014 —
# so the correction is a floor on the bias, never the whole of it.
ADDED_TO_INDEX = {
    "AVGO": "2014-05-08", "AMD": "2017-03-20", "TSLA": "2020-12-21",
    "PANW": "2023-06-20", "UBER": "2023-12-18", "PLTR": "2024-09-23",
}


def mid_window_joiners(universe, window_start) -> set[str]:
    """Universe names that were not yet index members when the window opened —
    i.e. names the strategy could not plausibly have been choosing among."""
    start = str(window_start)[:10]
    return {s for s in universe if s in ADDED_TO_INDEX and ADDED_TO_INDEX[s] > start}


def _next_day_map(days: list) -> dict:
    return {d: days[i + 1] for i, d in enumerate(days[:-1])}


def run_variant(data, cfg, *, drop: set[str] | None = None, slip=0.0005,
                fill_next_open=False, start_equity: float | None = None):
    """The gate's loop, with knobs. drop = symbols removed from the universe."""
    start_equity = float(start_equity or cfg["starting_equity"])
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


def run_static(data, cfg, targets: dict, *, slip=0.0005, start_equity: float | None = None):
    """No regime gate, no stock picking: fixed weights, same weekly cadence and
    drift band. The control that asks whether any of the machinery earns its keep."""
    start_equity = float(start_equity or cfg["starting_equity"])
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
    period = cfg.get("backtest_period", "4y")
    print(f"Fetching daily history ({period})...")
    data = bd.daily_history(symbols, period)
    print(f"  {len(data)}/{len(symbols)} symbols returned data")
    missing = sorted(set(symbols) - set(data))
    if missing:
        print(f"  NOT FETCHABLE (residual survivorship bias lives here): {missing}")

    idx_cfg = copy.deepcopy(cfg)
    idx_cfg["strategy"]["weights"]["risk_on"] = {"stocks": 0.0, "index": 0.70,
                                                 "defensive": 0.20, "cash": 0.10}
    idx_cfg["risk"]["max_position_weight"] = 0.40   # two ETFs cannot fill 70% at a 12% cap

    # The window is only known after the warmup is trimmed, so take it from a real run.
    probe, probe_orders = run_variant(data, cfg)
    win_start = probe.index[0]
    print(f"  tested window opens {str(win_start)[:10]}")

    top3 = top_performers(data, cfg, 3)
    frozen = mid_window_joiners(uni["stocks"], win_start)
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

    span_y = len(probe) / 252
    print(f"\nRunning 7 variants, each a full {span_y:.1f}y replay — several minutes:")
    base = record("baseline", "what the gate measures", probe, probe_orders)
    record("index_only", "does stock picking beat the index at the same risk?",
           *run_variant(data, idx_cfg))
    record("static_70_20_10", "does ANY of the machinery earn its keep?",
           *run_static(data, cfg, dict({cfg["benchmark"]: 0.70}, **ballast)))
    record("frozen_universe", "how much is hindsight in the universe?",
           *run_variant(data, cfg, drop=frozen),
           note=f"dropped {sorted(frozen) or 'nothing'} — joined the index after "
                f"{str(win_start)[:10]}, so the strategy could not have been picking them")
    record("drop_top3", "is the edge broad, or three lucky names?",
           *run_variant(data, cfg, drop=top3),
           note=f"dropped {sorted(top3)} — best performers, knowable only after the fact")
    record("pessimistic_fills", "does the edge survive realistic execution?",
           *run_variant(data, cfg, slip=0.0025, fill_next_open=True),
           note="filled at next open, 25bps each way")
    record("bias_corrected", "the closest thing to an unbiased estimate",
           *run_variant(data, cfg, drop=frozen, slip=0.0025, fill_next_open=True),
           note="frozen universe AND realistic fills — both are corrections for things "
                "that genuinely bias the result, with no stress test stacked on top. "
                "This is the row to weigh against SPY.")
    record("honest_worst_case", "all of the above at once",
           *run_variant(data, cfg, drop=frozen | top3, slip=0.0025, fill_next_open=True),
           note="a FLOOR, not an estimate — it stacks the drop_top3 stress test on top of "
                "the real corrections, penalising the stock sleeve twice. The true "
                "bias-corrected figure sits between this and bias_corrected.")

    # SPY over exactly the baseline's span, so the comparison is like for like.
    bc = curves["baseline"]
    spy = summarize(data[cfg["benchmark"]]["close"].loc[bc.index[0]:bc.index[-1]],
                    "SPY buy & hold")

    # The TESTED window, not the raw fetch: the momentum lookback plus warmup consumes
    # the first ~282 trading days, so the data range overstates what was actually replayed.
    w0, w1 = str(bc.index[0])[:10], str(bc.index[-1])[:10]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "steward_research.json").write_text(json.dumps(
        {"variants": runs, "benchmark": spy,
         "window": {"start": w0, "end": w1, "trading_days": len(bc),
                    "data_fetched_from": str(min(data[cfg["benchmark"]].index).date())},
         "unfetchable_symbols": missing}, indent=2, default=str))

    md = ["# STEWARD research — what the gate cannot tell you\n",
          f"Tested window: **{w0} to {w1}** ({len(bc)/252:.1f}y, {len(bc)} trading days). "
          f"Data was fetched from {str(min(data[cfg['benchmark']].index).date())}; the momentum "
          f"lookback and warmup consume the difference. SPY buy & hold over the tested span: "
          f"**{spy['total_return_pct']}%**, max drawdown **{spy['max_drawdown_pct']}%**.\n",
          "| variant | question | return | max DD | Sharpe | vs baseline |",
          "|---|---|---|---|---|---|"]
    for r in runs:
        delta = r["total_return_pct"] - base["total_return_pct"]
        md.append(f"| `{r['label']}` | {r['question']} | {r['total_return_pct']}% | "
                  f"{r['max_drawdown_pct']}% | {r['sharpe_daily_ann']} | "
                  f"{delta:+.2f} pts |")
    # SPY belongs in the table, not a footnote: over a full cycle the drawdown gap is
    # the whole argument for owning any of this machinery.
    md.append(f"| **SPY buy & hold** | the thing to beat | {spy['total_return_pct']}% | "
              f"{spy['max_drawdown_pct']}% | {spy['sharpe_daily_ann']} | "
              f"{spy['total_return_pct'] - base['total_return_pct']:+.2f} pts |")
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
