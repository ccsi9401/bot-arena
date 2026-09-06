#!/usr/bin/env python3
"""TALON backtest, control arms and launch gate.

  python backtest/backtest.py              # real Alpaca bars -> state/talon/gate.json, exit 1 on FAIL
  python backtest/backtest.py --synthetic  # random-walk smoke test: NO gate.json, result is meaningless

Rules this file lives by:
  * Entries are decided on bar i and filled at the OPEN of bar i+1. Never at a close that
    was just used to generate the signal.
  * Exits follow rising_floor.position.evaluate_on, the same key the live cycle reads.
    completed_bar: the floor ratchets and is checked on the close of bar i, exit fills at
    the open of bar i+1 (a close-based stop). current_price: a floor set at the previous
    close is breached during bar i if the LOW touches it, filled at the floor, or at the
    open if the bar gapped through (the live cycle checking the mark six times a day).
  * fee_bps + slippage_bps are applied to every fill, adversely.
  * The sizing loop is planner.plan() and the exit logic is floor.PositionFloor — both
    IMPORTED from the talon package and called per bar with i=<bar>. There is no second
    copy of either here. If you feel the urge to write sizing logic below, stop and import.
  * Point-in-time universe: with data.alignment point_in_time the panel sits on the union
    calendar with NaN where a name has no bar. A name is scored once it has min_bars of its
    own history; a held name that loses its bars (delisting) is force-closed at its last
    valid close. Nothing is forward-filled into a signal.
  * Two control arms are mandatory: BTC buy-and-hold and equal-weight buy-and-hold across
    the names tradeable at the window start, same window, same entry cost. A bot that makes
    20% while the market makes 60% has lost; the controls make that visible.
  * The gate also splits the tested window into backtest_gate.sub_windows equal parts and
    requires each to clear the drawdown cap and beat the controls. One long bull leg is not
    a pass.

The kill switch: live, a fresh halt clears on the next UTC day but the drawdown check
re-fires against the same high-water mark until a human resets it in state. Here that
human reset is modelled as backtest.kill_switch_restart_bars flat bars, after which the
high-water mark is reset to current equity. Reported as kill_switch_events.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talon import ROOT, load_config  # noqa: E402
from talon.data import align, fetch_bars  # noqa: E402
from talon.floor import BPS, EVAL_COMPLETED_BAR, EVAL_CURRENT_PRICE, AccountFloor, PositionFloor  # noqa: E402
from talon.planner import HALT_FLATTEN, halt_action, kill_switch_state, plan  # noqa: E402
from talon.strategies import regime_on  # noqa: E402

EXIT_PASS, EXIT_FAIL = 0, 1
EXIT_CHECK_INTRABAR, EXIT_CHECK_CLOSE = "intrabar_low", "close"


def exit_check_for(cfg: dict) -> str:
    """The backtest's exit convention is DERIVED from rising_floor.position.evaluate_on, the
    same key the live cycle reads, so the two cannot disagree."""
    mode = str(cfg["rising_floor"]["position"].get("evaluate_on", EVAL_COMPLETED_BAR))
    if mode == EVAL_COMPLETED_BAR:
        return EXIT_CHECK_CLOSE
    if mode == EVAL_CURRENT_PRICE:
        return EXIT_CHECK_INTRABAR
    raise ValueError(f"rising_floor.position.evaluate_on must be {EVAL_COMPLETED_BAR} or {EVAL_CURRENT_PRICE}")


# ---------------------------------------------------------------------------
# synthetic bars — geometric random walk, three trend regimes, no edge by design
# ---------------------------------------------------------------------------

def synthetic_bars(cfg: dict, bars: int | None = None, seed: int | None = None) -> dict[str, pd.DataFrame]:
    s = cfg["backtest"]["synthetic"]
    n = int(bars or s["bars"])
    rng = np.random.default_rng(int(s["seed"] if seed is None else seed))
    symbols = list(cfg["universe"]["symbols"])
    vol = float(s["daily_vol"])
    cf = float(s["common_factor"])
    regimes = s["regimes"]
    seg = n // len(regimes)
    drift = np.concatenate([np.full(seg, float(r["drift"])) for r in regimes] + [np.full(n - seg * len(regimes), float(regimes[-1]["drift"]))])
    market = rng.standard_normal(n)
    idx = pd.date_range(end=datetime.now(timezone.utc).replace(hour=5, minute=0, second=0, microsecond=0),
                        periods=n, freq="D")
    out = {}
    for k, sym in enumerate(symbols):
        eps = rng.standard_normal(n)
        r = drift + vol * (cf * market + math.sqrt(1.0 - cf * cf) * eps)
        close = float(s["start_price"]) * (1.0 + k) * np.exp(np.cumsum(r))
        open_ = np.concatenate([[close[0] * math.exp(-r[0])], close[:-1]])
        wick = np.abs(rng.standard_normal(n)) * vol * float(s["intrabar_range"])
        high = np.maximum(open_, close) * np.exp(wick)
        low = np.minimum(open_, close) * np.exp(-wick)
        out[sym] = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)
    return out


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def metrics(curve: pd.Series, periods_per_year: int) -> dict:
    curve = curve.astype(float)
    rets = curve.pct_change().dropna()
    total = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
    years = (len(curve) - 1) / float(periods_per_year)
    cagr = float((curve.iloc[-1] / curve.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 and curve.iloc[0] > 0 else 0.0
    sd = float(rets.std(ddof=0))
    sharpe = float(rets.mean() / sd * math.sqrt(periods_per_year)) if sd > 0 else 0.0
    dd = float((curve / curve.cummax() - 1.0).min())
    return {
        "total_return_pct": round(total, 4),
        "cagr_pct": round(cagr, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(-dd, 4),
        "years": round(years, 3),
        "final_equity": round(float(curve.iloc[-1]), 2),
    }


# ---------------------------------------------------------------------------
# strategy arm — calls planner.plan() and PositionFloor per bar
# ---------------------------------------------------------------------------

def run_strategy(bars: dict[str, pd.DataFrame], cfg: dict, verbose: bool = False) -> dict:
    rcfg, dcfg, btcfg = cfg["risk"], cfg["data"], cfg["backtest"]
    bench = bars[cfg["universe"]["benchmark"]]
    n = len(bench)
    min_bars = int(dcfg["min_bars"])
    if n <= min_bars + 1:
        raise RuntimeError(f"need more than {min_bars + 1} aligned bars, have {n}")
    if bench["close"].isna().any():
        raise RuntimeError("benchmark has gaps; the regime gate needs gap-free history")
    cost = (float(cfg["costs"]["fee_bps"]) + float(cfg["costs"]["slippage_bps"])) / BPS
    min_notional = float(rcfg["min_order_notional"])
    restart_bars = int(btcfg["kill_switch_restart_bars"])
    exit_check = exit_check_for(cfg)
    start_equity = float(rcfg["starting_equity"])

    closes = {s: df["close"].to_numpy(dtype=float) for s, df in bars.items()}
    opens = {s: df["open"].to_numpy(dtype=float) for s, df in bars.items()}
    lows = {s: df["low"].to_numpy(dtype=float) for s, df in bars.items()}
    marks = {s: df["close"].ffill().to_numpy(dtype=float) for s, df in bars.items()}   # marking only, never signals
    avail = {s: ~np.isnan(closes[s]) for s in bars}
    reg = regime_on(bench, cfg).to_numpy()

    cash = start_equity
    positions: dict[str, dict] = {}
    af = AccountFloor(basis=start_equity)
    hw = start_equity
    prev_equity = start_equity
    halt_until = -1
    kill_events = 0
    breaker_events = 0
    trades: list[dict] = []
    curve_ts, curve_val = [], []
    feature_cache: dict = {}
    regime_on_bars = 0
    swept_total = 0.0

    def close_trade(sym: str, fill: float, exit_bar: int, kind: str) -> None:
        nonlocal cash
        p = positions.pop(sym)
        fl: PositionFloor = p["floor"]
        proceeds = p["qty"] * fill
        cash += proceeds
        trades.append({
            "symbol": sym, "entry_ts": str(p["entry_ts"]), "exit_ts": str(bench.index[exit_bar]),
            "entry": fl.entry, "exit": fill, "qty": p["qty"], "floor": fl.floor,
            "pnl": proceeds - p["qty"] * fl.entry,
            "r": fl.r_multiple(fill), "stage": fl.stage,
            "bars_held": exit_bar - p["entry_bar"],
            "reason": kind,
        })

    first = min_bars - 1                      # signal bar whose fill lands on bar min_bars
    for i in range(first, n - 1):
        ts_i = bench.index[i]
        exited: list[str] = []

        # (a) intrabar exits during bar i against floors set at the previous close — the
        #     live cycle checks the current price six times a day, this is its daily-bar twin
        if exit_check == EXIT_CHECK_INTRABAR:
            for sym in list(positions):
                fl: PositionFloor = positions[sym]["floor"]
                if not avail[sym][i]:
                    close_trade(sym, marks[sym][i] * (1.0 - cost), i, "delisted")
                elif opens[sym][i] <= fl.floor:
                    close_trade(sym, opens[sym][i] * (1.0 - cost), i, "gap")
                elif lows[sym][i] <= fl.floor:
                    close_trade(sym, fl.floor * (1.0 - cost), i, "floor")
                else:
                    continue
                exited.append(sym)

        # (b) mark to market at close i, account floor, kill switches
        equity = cash + sum(p["qty"] * marks[s][i] for s, p in positions.items())
        day_start = prev_equity
        hw = max(hw, equity)
        regime_on_bars += int(reg[i])
        swept = af.update(equity, cfg, str(ts_i))
        swept_total += swept
        tradeable = af.tradeable(equity)

        if i < halt_until:
            halted, reason = True, "kill switch: cooldown"
        else:
            if halt_until != -1 and i == halt_until:
                hw = equity                  # the modelled human reset
                halt_until = -1
            halted, reason = kill_switch_state(equity, hw, day_start, cfg)
            if halted and reason.startswith("kill switch"):
                halt_until = i + 1 + restart_bars
                kill_events += 1
            elif halted:
                breaker_events += 1
        flatten = halted and halt_action(reason, cfg) == HALT_FLATTEN

        # (c) floors ratchet on close i; close-based breach (if configured), halt flatten and
        #     next-bar delistings fill at open i+1
        for sym in list(positions):
            fl = positions[sym]["floor"]
            fl.update(marks[sym][i], cfg)
            breached_close = exit_check == EXIT_CHECK_CLOSE and fl.breached(marks[sym][i])
            if not avail[sym][i + 1]:
                close_trade(sym, marks[sym][i] * (1.0 - cost), i + 1, "delisted")
            elif flatten:
                close_trade(sym, opens[sym][i + 1] * (1.0 - cost), i + 1, "halt")
            elif breached_close:
                close_trade(sym, opens[sym][i + 1] * (1.0 - cost), i + 1, "floor")
            else:
                continue
            exited.append(sym)

        # (d) entries: the planner at bar i on TRADEABLE equity, filled at open i+1
        if not halted:
            open_map = {s: p["qty"] * marks[s][i] for s, p in positions.items()}
            res = plan(bars, bench, tradeable, cfg, open_map, i=i, feature_cache=feature_cache, exclude=exited)
            for sym, t in res["targets"].items():
                if not avail[sym][i + 1] or np.isnan(opens[sym][i + 1]):
                    continue                     # no bar to fill on
                notional = min(float(t["notional"]), cash)
                if notional < min_notional:
                    continue
                fill = opens[sym][i + 1] * (1.0 + cost)
                qty = notional / fill
                cash -= notional
                positions[sym] = {
                    "qty": qty,
                    "floor": PositionFloor.open(sym, fill, t["atr"], cfg),
                    "entry_ts": bench.index[i + 1],
                    "entry_bar": i + 1,
                }
                if verbose:
                    print(f"{bench.index[i + 1]:%Y-%m-%d} BUY {sym} {notional:.0f} score={t['score']} {t['reasons']}")

        curve_ts.append(ts_i)
        curve_val.append(equity)
        prev_equity = equity

    # final mark at the last close; open positions are reported, not force-closed into the trade stats
    last = n - 1
    equity = cash + sum(p["qty"] * marks[s][last] for s, p in positions.items())
    curve_ts.append(bench.index[last])
    curve_val.append(equity)
    open_at_end = [{"symbol": s, "qty": p["qty"], "entry": p["floor"].entry,
                    "r": p["floor"].r_multiple(marks[s][last]), "stage": p["floor"].stage}
                   for s, p in positions.items()]

    curve = pd.Series(curve_val, index=pd.DatetimeIndex(curve_ts))
    m = metrics(curve, int(dcfg["periods_per_year"]))
    wins = [t for t in trades if t["pnl"] > 0]
    m.update({
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "avg_r": round(float(np.mean([t["r"] for t in trades])), 3) if trades else 0.0,
        "avg_bars_held": round(float(np.mean([t["bars_held"] for t in trades])), 1) if trades else 0.0,
        "locked_reserve": round(af.locked, 2),
        "swept_total": round(swept_total, 2),
        "kill_switch_events": kill_events,
        "breaker_events": breaker_events,
        "regime_on_share": round(regime_on_bars / max(n - 1 - first, 1), 3),
        "open_at_end": len(open_at_end),
        "exit_reasons": {k: sum(1 for t in trades if t["reason"] == k) for k in ("floor", "gap", "halt", "delisted")},
    })
    return {"metrics": m, "curve": curve, "trades": trades, "open_at_end": open_at_end,
            "window": {"start": str(curve.index[0]), "end": str(curve.index[-1]), "bars": len(curve)}}


# ---------------------------------------------------------------------------
# control arms — buy at the same first fill bar, same entry cost, hold to the end
# ---------------------------------------------------------------------------

def buy_and_hold(bars: dict[str, pd.DataFrame], cfg: dict, weights: dict[str, float]) -> dict:
    """Names without a bar at the first fill are dropped and the weights re-normalised —
    the basket you could actually have bought on day one."""
    dcfg = cfg["data"]
    bench = bars[cfg["universe"]["benchmark"]]
    n = len(bench)
    first = int(dcfg["min_bars"]) - 1
    start_equity = float(cfg["risk"]["starting_equity"])
    cost = (float(cfg["costs"]["fee_bps"]) + float(cfg["costs"]["slippage_bps"])) / BPS
    live = {s: w for s, w in weights.items() if not np.isnan(bars[s]["open"].iloc[first + 1])}
    wsum = sum(live.values())
    qty = {s: start_equity * (w / wsum) / (bars[s]["open"].iloc[first + 1] * (1.0 + cost)) for s, w in live.items()}
    marks = {s: bars[s]["close"].ffill().to_numpy(dtype=float) for s in qty}
    vals = [start_equity]
    for i in range(first + 1, n):
        vals.append(sum(q * marks[s][i] for s, q in qty.items()))
    curve = pd.Series(vals, index=bench.index[first:])
    m = metrics(curve, int(dcfg["periods_per_year"]))
    m["names"] = len(qty)
    return {"metrics": m, "curve": curve}


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def sub_window_checks(strategy: dict, controls: dict[str, dict], cfg: dict) -> list[dict]:
    """Split the tested window into equal parts; each must clear the drawdown cap and beat
    the controls on must_beat_control_on. Guards against a single-regime pass."""
    g = cfg["backtest_gate"]
    k = int(g.get("sub_windows", 1))
    if k <= 1:
        return []
    key = g["must_beat_control_on"]
    ppy = int(cfg["data"]["periods_per_year"])
    curve = strategy["curve"]
    edges = np.linspace(0, len(curve) - 1, k + 1).astype(int)
    checks = []
    for j in range(k):
        a, b = int(edges[j]), int(edges[j + 1])
        seg = curve.iloc[a:b + 1]
        ms = metrics(seg, ppy)
        label = f"{seg.index[0]:%Y-%m-%d}..{seg.index[-1]:%Y-%m-%d}"
        checks.append({"check": f"sub-window {j + 1}/{k} max_drawdown <= max_drawdown_pct", "window": label,
                       "value": ms["max_drawdown_pct"], "threshold": g["max_drawdown_pct"],
                       "ok": ms["max_drawdown_pct"] <= g["max_drawdown_pct"]})
        for name in g["controls_to_beat"]:
            mc = metrics(controls[name]["curve"].iloc[a:b + 1], ppy)
            checks.append({"check": f"sub-window {j + 1}/{k} beat {name} on {key}", "window": label,
                           "value": ms[key], "threshold": mc[key], "ok": ms[key] > mc[key]})
    return checks


def evaluate_gate(strategy: dict, controls: dict[str, dict], cfg: dict) -> tuple[bool, str, list[dict]]:
    g = cfg["backtest_gate"]
    s = strategy["metrics"]
    key = g["must_beat_control_on"]
    checks = [
        {"check": "sharpe >= min_sharpe", "value": s["sharpe"], "threshold": g["min_sharpe"], "ok": s["sharpe"] >= g["min_sharpe"]},
        {"check": "max_drawdown <= max_drawdown_pct", "value": s["max_drawdown_pct"], "threshold": g["max_drawdown_pct"], "ok": s["max_drawdown_pct"] <= g["max_drawdown_pct"]},
        {"check": "total_return >= min_total_return_pct", "value": s["total_return_pct"], "threshold": g["min_total_return_pct"], "ok": s["total_return_pct"] >= g["min_total_return_pct"]},
        {"check": "trades >= min_trades", "value": s["trades"], "threshold": g["min_trades"], "ok": s["trades"] >= g["min_trades"]},
        {"check": "years_tested >= years_required", "value": s["years"], "threshold": g["years_required"], "ok": s["years"] >= g["years_required"]},
    ]
    for name in g["controls_to_beat"]:
        c = controls[name]["metrics"][key]
        checks.append({"check": f"beat {name} on {key}", "value": s[key], "threshold": c, "ok": s[key] > c})
    if "curve" in strategy and all("curve" in c for c in controls.values()):
        checks += sub_window_checks(strategy, controls, cfg)
    failed = [c["check"] for c in checks if not c["ok"]]
    passed = not failed
    reason = "all checks passed" if passed else "FAILED: " + "; ".join(failed)
    return passed, reason, checks


def print_report(strategy: dict, controls: dict[str, dict], checks: list[dict], passed: bool, synthetic: bool) -> None:
    rows = {"TALON": strategy["metrics"], **{k: v["metrics"] for k, v in controls.items()}}
    cols = ["total_return_pct", "cagr_pct", "sharpe", "max_drawdown_pct", "years", "final_equity"]
    print()
    print(f"window: {strategy['window']['start']} -> {strategy['window']['end']} ({strategy['window']['bars']} bars)")
    print(f"{'arm':<14}" + "".join(f"{c:>20}" for c in cols))
    for name, m in rows.items():
        print(f"{name:<14}" + "".join(f"{m[c]:>20}" for c in cols))
    s = strategy["metrics"]
    print()
    print(f"trades={s['trades']} win_rate={s['win_rate']} avg_R={s['avg_r']} avg_bars_held={s['avg_bars_held']} "
          f"open_at_end={s['open_at_end']} exits={s['exit_reasons']}")
    print(f"kill_switch_events={s['kill_switch_events']} breaker_events={s['breaker_events']} "
          f"regime_on_share={s['regime_on_share']} locked_reserve={s['locked_reserve']} (swept_total={s['swept_total']})")
    print()
    for c in checks:
        win = f"  [{c['window']}]" if "window" in c else ""
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['check']:<52} value={c['value']} threshold={c['threshold']}{win}")
    print()
    verdict = "PASS" if passed else "FAIL"
    if synthetic:
        print("=" * 78)
        print(f"  SYNTHETIC DATA — GATE VERDICT '{verdict}' IS MEANINGLESS AND WAS NOT WRITTEN.")
        print("  Random walks have no edge. A PASS here would indicate a look-ahead bug.")
        print("=" * 78)
    else:
        print(f"GATE: {verdict}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--synthetic", action="store_true", help="random-walk smoke test; never writes gate.json")
    ap.add_argument("--bars", type=int, default=None, help="synthetic: number of bars")
    ap.add_argument("--seed", type=int, default=None, help="synthetic: RNG seed")
    ap.add_argument("--lookback", type=int, default=None, help="real: override backtest.lookback_bars")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.synthetic:
        print("*** SYNTHETIC MODE: geometric random walk with three trend regimes. NO EDGE EXISTS. ***")
        bars = synthetic_bars(cfg, bars=args.bars, seed=args.seed)
    else:
        symbols = list(dict.fromkeys(list(cfg["universe"]["symbols"]) + [cfg["universe"]["benchmark"]]))
        lookback = int(args.lookback or cfg["backtest"]["lookback_bars"])
        print(f"fetching {lookback} x {cfg['data']['timeframe']} bars for {len(symbols)} symbols ...")
        bars = fetch_bars(symbols, cfg["data"]["timeframe"], lookback, cfg=cfg)
        for s, df in sorted(bars.items()):
            print(f"  {s:<10} {len(df):>5} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
    mode = cfg["data"].get("alignment", "intersection")
    bars = align(bars, mode=mode)
    if cfg["universe"]["benchmark"] not in bars:
        print("benchmark missing after alignment")
        return EXIT_FAIL
    print(f"aligned panel ({mode}): {len(bars)} symbols x {len(next(iter(bars.values())))} bars")

    strategy = run_strategy(bars, cfg, verbose=args.verbose)
    universe = [s for s in cfg["universe"]["symbols"] if s in bars]
    controls = {
        "btc_hold": buy_and_hold(bars, cfg, {cfg["universe"]["benchmark"]: 1.0}),
        "equal_weight": buy_and_hold(bars, cfg, {s: 1.0 / len(universe) for s in universe}),
    }
    passed, reason, checks = evaluate_gate(strategy, controls, cfg)
    print_report(strategy, controls, checks, passed, args.synthetic)

    if args.synthetic:
        return EXIT_PASS  # the engine ran; the verdict is deliberately not recorded

    gate = {
        "passed": bool(passed),
        "reason": reason,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_version": cfg["meta"]["version"],
        "universe": universe,
        "alignment": mode,
        "window": strategy["window"],
        "checks": checks,
        "metrics": {"strategy": strategy["metrics"], **{k: v["metrics"] for k, v in controls.items()}},
    }
    out = ROOT / cfg["paths"]["gate"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gate, indent=2, default=str), encoding="utf-8")
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"wrote {shown}: passed={passed} — {reason}")
    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
