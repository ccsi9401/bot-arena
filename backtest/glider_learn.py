#!/usr/bin/env python3
"""GLIDER learner — gated, walk-forward parameter search.

Runs monthly on the Actions runner. Sweeps the learnable strategy knobs, replays the
LIVE analyzer over `learning.history_period` of daily data for every candidate, and
replaces config/glider.yaml ONLY if a candidate clears every one of these bars:

  1. passes the DESIGN.md gate on the full window (≥30 trades, expectancy>0, DD<15%)
  2. selection-window Sharpe beats the incumbent by ≥ noise_floor_sigmas × the
     incumbent's block-bootstrap Sharpe std  (i.e. the gain is bigger than luck)
  3. beats the incumbent in ≥ min_fold_win_frac of calendar-year folds  (consistency)
  4. on the untouched HOLDOUT year (never used for ranking): expectancy>0 and
     Sharpe ≥ incumbent's holdout Sharpe  (out-of-sample check)
  5. at least min_days_between_changes since the last change

Otherwise nothing changes and the report says why. Every run is journaled to
state/glider/learn_history.json and reports/glider_learn/<date>.md.

Usage:  python backtest/glider_learn.py [--dry-run] [--max-candidates N]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.common import load_config, config_hash  # noqa: E402
from backtest import data as bd                    # noqa: E402
from backtest import metrics                       # noqa: E402
from backtest.engine_glider import run as run_glider, precompute  # noqa: E402

CFG_PATH = ROOT / "config" / "glider.yaml"
STATE_DIR = ROOT / "state" / "glider"
REPORT_DIR = ROOT / "reports" / "glider_learn"

# ---- the search space ------------------------------------------------------------
GRID = {
    "pullback_rsi2_max":       [5, 10, 15],
    "max_pct_below_52wk_high": [10, 15, 25],
    "stop_atr_mult":           [1.5, 2.0, 2.5, 3.0],
    "max_hold_days":           [10, 15, 25],
    "exit":                    [("target", 1.5), ("target", 2.0), ("target", 3.0),
                                ("trail", 2.5), ("trail", 3.5)],
}
LEARNABLE = ["pullback_rsi2_max", "max_pct_below_52wk_high", "stop_atr_mult",
             "max_hold_days", "exit_mode", "target_r_mult", "trail_atr_mult"]


def candidates_from_grid(base: dict, max_n: int, seed: int) -> list[dict]:
    keys = list(GRID)
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    rng = random.Random(seed)
    if len(combos) > max_n:
        combos = rng.sample(combos, max_n)
    out = []
    for combo in combos:
        s = dict(base)
        for k, v in zip(keys, combo):
            if k == "exit":
                s["exit_mode"] = v[0]
                if v[0] == "target":
                    s["target_r_mult"] = v[1]
                else:
                    s["trail_atr_mult"] = v[1]
            else:
                s[k] = v
        out.append(s)
    return out


def params_of(strategy: dict) -> dict:
    return {k: strategy.get(k) for k in LEARNABLE}


# ---- scoring ----------------------------------------------------------------------
def sharpe(curve: pd.Series) -> float | None:
    rets = curve.pct_change().dropna()
    if len(rets) < 20 or rets.std() == 0:
        return None
    return float(rets.mean() / rets.std() * math.sqrt(252))


def bootstrap_sharpe_std(curve: pd.Series, n: int, block: int = 21, seed: int = 0) -> float:
    rets = curve.pct_change().dropna().to_numpy()
    if len(rets) < block * 3:
        return 1.0
    rng = np.random.default_rng(seed)
    nblocks = math.ceil(len(rets) / block)
    starts = np.arange(0, len(rets) - block + 1)
    vals = []
    for _ in range(n):
        idx = rng.choice(starts, nblocks)
        sample = np.concatenate([rets[i:i + block] for i in idx])[:len(rets)]
        sd = sample.std()
        if sd > 0:
            vals.append(sample.mean() / sd * math.sqrt(252))
    return float(np.std(vals)) if vals else 1.0


def yearly_sharpes(curve: pd.Series, min_days: int = 120) -> dict[int, float]:
    out = {}
    for yr, sub in curve.groupby(curve.index.year):
        if len(sub) >= min_days:
            s = sharpe(sub)
            if s is not None:
                out[int(yr)] = s
    return out


def evaluate(curve: pd.Series, trades: list[dict], holdout_start) -> dict:
    sel = curve[curve.index < holdout_start]
    hold = curve[curve.index >= holdout_start]
    hold_trades = [t for t in trades if pd.Timestamp(t["closed"]) >= holdout_start]
    full = metrics.summarize(curve, trades, "full")
    return {
        "full": full,
        "sel_sharpe": sharpe(sel),
        "sel_years": yearly_sharpes(sel),
        "hold_sharpe": sharpe(hold),
        "hold_expectancy": (sum(t["pnl"] for t in hold_trades) / len(hold_trades)
                            if hold_trades else None),
        "hold_trades": len(hold_trades),
    }


# ---- yaml editing that keeps comments -------------------------------------------
def set_yaml_key(text: str, key: str, value) -> str:
    val = value if isinstance(value, str) else json.dumps(value)
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*)([^#\n]*?)(\s*#.*)?$", re.M)
    if not pat.search(text):
        raise KeyError(f"{key} not found in glider.yaml")
    return pat.sub(lambda m: f"{m.group(1)}{val}{(m.group(3) or '')}", text, count=1)


# ---- main -------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="evaluate but never write config")
    ap.add_argument("--max-candidates", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config("glider")
    L = cfg.get("learning", {})
    today = date.today()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    hist_f = STATE_DIR / "learn_history.json"
    history = json.loads(hist_f.read_text()) if hist_f.exists() else []
    lines = [f"# GLIDER learner — {today}\n"]

    def finish(decision: str, detail: dict | None = None, changed: bool = False) -> int:
        entry = {"date": str(today), "decision": decision, "changed": changed,
                 "config_hash_before": config_hash(cfg), **(detail or {})}
        history.append(entry)
        hist_f.write_text(json.dumps(history, indent=2, default=str))
        lines.append(f"\n**Decision:** {decision}\n")
        (REPORT_DIR / f"{today}.md").write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        return 0

    if not L.get("enabled", False):
        return finish("learning disabled in config — no action")

    last_change = next((h["date"] for h in reversed(history) if h.get("changed")), None)
    if last_change:
        since = (today - date.fromisoformat(last_change)).days
        if since < L.get("min_days_between_changes", 28):
            return finish(f"last change {since}d ago < min {L.get('min_days_between_changes', 28)}d — evaluate only",
                          {"note": "cooldown"})

    # ---- data ----
    uni = sorted(set(cfg["universe"]["stocks"] + cfg["universe"]["etfs"]))
    period = L.get("history_period", "5y")
    print(f"Fetching daily history ({period}) for {len(uni)} symbols...")
    data = bd.daily_history(uni, period)
    pre = precompute(data, set(cfg["universe"]["etfs"]))
    bench_days = sorted(pre["feats"][cfg["universe"]["benchmark"]].keys())
    holdout_start = bench_days[-L.get("holdout_days", 252)]
    lines.append(f"- symbols: {len(data)} · trading days: {len(bench_days)} "
                 f"({bench_days[0].date()} → {bench_days[-1].date()})")
    lines.append(f"- holdout (never ranked on): from {holdout_start.date()}")

    # ---- incumbent ----
    inc_curve, inc_trades = run_glider(data, cfg, pre=pre)
    inc = evaluate(inc_curve, inc_trades, holdout_start)
    noise = bootstrap_sharpe_std(inc_curve[inc_curve.index < holdout_start],
                                 L.get("bootstrap_samples", 300))
    floor = L.get("noise_floor_sigmas", 1.0) * noise
    lines.append(f"\n## Incumbent  `{params_of(cfg['strategy'])}`")
    lines.append(f"- full window: {inc['full']['total_return_pct']}% · DD {inc['full']['max_drawdown_pct']}% · "
                 f"Sharpe {inc['full']['sharpe_daily_ann']} · {inc['full']['n_trades']} trades · "
                 f"gate {'PASS' if inc['full']['gate']['passed'] else 'FAIL'}")
    lines.append(f"- selection Sharpe {inc['sel_sharpe']:.2f} ± {noise:.2f} (bootstrap) → "
                 f"a challenger must reach {inc['sel_sharpe'] + floor:.2f}")
    lines.append(f"- yearly Sharpe: { {k: round(v, 2) for k, v in inc['sel_years'].items()} }")
    lines.append(f"- holdout Sharpe {inc['hold_sharpe']} · holdout expectancy {inc['hold_expectancy']}")
    if not inc["full"]["gate"]["passed"]:
        lines.append("- ⚠️ incumbent FAILS the gate on refreshed data — review before trusting live")

    # ---- candidates ----
    max_n = args.max_candidates or L.get("max_candidates", 400)
    seed = int(today.strftime("%Y%m"))
    cands = candidates_from_grid(cfg["strategy"], max_n, seed)
    lines.append(f"\n## Sweep — {len(cands)} candidates (seed {seed})")
    results = []
    n_years = len(inc["sel_years"])
    need_wins = math.ceil(L.get("min_fold_win_frac", 0.67) * n_years) if n_years else 0
    for i, strat in enumerate(cands):
        c = dict(cfg); c["strategy"] = strat
        curve, trades = run_glider(data, c, pre=pre)
        ev = evaluate(curve, trades, holdout_start)
        wins = sum(1 for y, s in ev["sel_years"].items()
                   if y in inc["sel_years"] and s > inc["sel_years"][y])
        checks = {
            "gate": ev["full"]["gate"]["passed"],
            "beats_noise": (ev["sel_sharpe"] or -9) >= (inc["sel_sharpe"] or -9) + floor,
            "fold_consistency": wins >= need_wins,
            "holdout": (ev["hold_expectancy"] or 0) > 0 and
                       (ev["hold_sharpe"] or -9) >= (inc["hold_sharpe"] or -9),
        }
        results.append({"params": params_of(strat), "eval": ev, "wins": wins,
                        "checks": checks, "eligible": all(checks.values()),
                        "trades": trades})
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(cands)}")

    results.sort(key=lambda r: r["eval"]["sel_sharpe"] or -9, reverse=True)
    lines.append("\n| rank | params | sel Sharpe | holdout Sharpe | full ret % | DD % | trades | yr wins | eligible |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for rank, r in enumerate(results[:15], 1):
        e = r["eval"]
        lines.append(f"| {rank} | `{r['params']}` | {e['sel_sharpe'] or 0:.2f} | {e['hold_sharpe'] or 0:.2f} | "
                     f"{e['full']['total_return_pct']} | {e['full']['max_drawdown_pct']} | "
                     f"{e['full']['n_trades']} | {r['wins']}/{n_years} | {'✅' if r['eligible'] else ''} |")
    eligible = [r for r in results if r["eligible"]]
    lines.append(f"\n{len(eligible)} of {len(results)} candidates cleared all four bars.")

    if not eligible:
        # still refresh the reference trade set for reflection
        (REPORT_DIR / "reference_trades.json").write_text(json.dumps(
            {"params": params_of(cfg["strategy"]), "asof": str(today),
             "trades": inc_trades}, default=str))
        return finish("no candidate beat the incumbent beyond noise on all checks — config unchanged",
                      {"noise_floor": floor, "incumbent_sel_sharpe": inc["sel_sharpe"]})

    best = eligible[0]
    lines.append(f"\n## Winner  `{best['params']}`")
    for k in ("full", "sel_sharpe", "hold_sharpe", "hold_expectancy"):
        lines.append(f"- {k}: {best['eval'][k] if k != 'full' else {kk: vv for kk, vv in best['eval']['full'].items() if kk != 'gate'}}")

    if args.dry_run:
        return finish("DRY RUN — winner found but config not written", {"winner": best["params"]})

    text = CFG_PATH.read_text(encoding="utf-8")
    for k, v in best["params"].items():
        if v is not None:
            text = set_yaml_key(text, k, v)
    CFG_PATH.write_text(text, encoding="utf-8")
    (REPORT_DIR / "reference_trades.json").write_text(json.dumps(
        {"params": best["params"], "asof": str(today), "trades": best["trades"]}, default=str))
    return finish("config/glider.yaml UPDATED to winner (bounded: one change, gated, journaled)",
                  {"from": params_of(cfg["strategy"]), "to": best["params"],
                   "noise_floor": floor,
                   "incumbent_sel_sharpe": inc["sel_sharpe"], "winner_sel_sharpe": best["eval"]["sel_sharpe"],
                   "incumbent_hold_sharpe": inc["hold_sharpe"], "winner_hold_sharpe": best["eval"]["hold_sharpe"]},
                  changed=True)


if __name__ == "__main__":
    sys.exit(main())
