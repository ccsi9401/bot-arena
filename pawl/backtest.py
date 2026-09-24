"""
The gate.

This calls pawl.strategy directly -- the same decide(), target_weights() and
orders_needed() the live runner uses. There is deliberately no second copy of
the sizing logic here, because a backtest that reimplements the strategy tests
the reimplementation, not the strategy.

Discipline enforced here:
  * Signals are read from bar t's CLOSE and executed at bar t+1's OPEN.
    No decision ever sees a price it could not have seen.
  * Every fill pays the taker fee plus a slippage assumption. Maker fills are
    not assumed -- if the strategy only works at maker rates, it does not work.
  * Control arms run on identical cost assumptions, so the comparison is fair.
  * The full universe is today's Alpaca list replayed backwards, which IS
    survivorship-biased. That is exactly why pawl_core (BTC+ETH only, both of
    which led the market for the whole window) exists as the honest control.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import strategy as st
from .floor import compute_floor, enabled as floor_enabled, floor_multiple

ANN = 365.0


@dataclass
class Result:
    name: str
    equity: pd.Series
    trades: int = 0
    fees_paid: float = 0.0
    notes: List[str] = field(default_factory=list)

    def metrics(self) -> dict:
        e = self.equity.dropna()
        if len(e) < 30:
            return {"error": "insufficient history"}
        rets = e.pct_change().dropna()
        years = len(e) / ANN
        total = e.iloc[-1] / e.iloc[0] - 1.0
        cagr = (e.iloc[-1] / e.iloc[0]) ** (1 / years) - 1.0 if years > 0 else 0.0
        dd = float((e / e.cummax() - 1.0).min())
        vol = float(rets.std() * np.sqrt(ANN))
        sharpe = float(rets.mean() / rets.std() * np.sqrt(ANN)) if rets.std() > 0 else 0.0
        downside = rets[rets < 0].std()
        sortino = float(rets.mean() / downside * np.sqrt(ANN)) if downside and downside > 0 else 0.0
        return {
            "total_return": round(total, 4), "cagr": round(cagr, 4),
            "max_drawdown": round(dd, 4), "vol_annual": round(vol, 4),
            "sharpe": round(sharpe, 3), "sortino": round(sortino, 3),
            "calmar": round(cagr / abs(dd), 3) if dd < 0 else None,
            "trades": self.trades, "fees_paid": round(self.fees_paid, 2),
            "days": len(e),
        }


def _cost(cfg: dict) -> float:
    return cfg["costs"]["taker_fee"] + cfg["costs"]["assumed_slippage"]


def run_pawl(bars: Dict[str, pd.DataFrame], cfg: dict, rotation_enabled: bool,
             start_equity: float = 10_000.0, name: str = "pawl",
             floor_mode: Optional[str] = None) -> Result:
    if floor_mode is not None:
        import copy as _copy
        cfg = _copy.deepcopy(cfg)
        cfg["floor"]["mode"] = floor_mode
    universe = list(cfg["universe"]["core"]) + list(cfg["universe"]["rotation"])
    bars = {k: v for k, v in bars.items() if k in universe and v is not None and not v.empty}
    if cfg["regime"]["anchor"] not in bars:
        return Result(name, pd.Series(dtype=float), notes=["anchor asset missing"])

    idx = sorted(set().union(*[set(df.index) for df in bars.values()]))
    idx = pd.DatetimeIndex(idx)
    warm = max(cfg["regime"]["sma_days"], cfg["momentum"]["lookback_days"],
               cfg["floor"]["atr_days"], 30) + 5
    if len(idx) < warm + 90:
        return Result(name, pd.Series(dtype=float), notes=["not enough history"])

    cash = start_equity
    qty: Dict[str, float] = {}
    floors: Dict[str, dict] = {}
    curve: Dict[pd.Timestamp, float] = {}
    trades = 0
    fees = 0.0
    cost = _cost(cfg)
    cooldown: Dict[str, pd.Timestamp] = {}
    roundtrips: Dict[str, int] = {}
    cool_days = int(cfg["momentum"].get("reentry_cooldown_days", 0))
    use_floor = floor_enabled(cfg)
    fmult = floor_multiple(cfg)
    budget = int(cfg["risk"]["monthly_roundtrip_budget"])

    atr_cache = {s: st.atr(d["high"], d["low"], d["close"], cfg["floor"]["atr_days"]) for s, d in bars.items()}

    def px(sym, t, field="close"):
        df = bars.get(sym)
        if df is None or t not in df.index:
            return None
        v = df.at[t, field]
        return float(v) if np.isfinite(v) else None

    def equity_at(t) -> float:
        v = cash
        for s, q in qty.items():
            p = px(s, t)
            if p:
                v += q * p
        return v

    for i in range(warm, len(idx) - 1):
        t, nxt = idx[i], idx[i + 1]

        # ---- floors are checked FIRST, on the next bar's low, and always fire
        for s in list(qty):
            fl = floors.get(s)
            lo = px(s, nxt, "low")
            if not fl or lo is None:
                continue
            if use_floor and lo <= fl["floor_price"]:
                fill = min(fl["floor_price"], px(s, nxt, "open") or fl["floor_price"])
                proceeds = qty[s] * fill * (1 - cost)
                fees += qty[s] * fill * cost
                cash += proceeds
                del qty[s]; floors.pop(s, None); trades += 1
                cooldown[s] = nxt + pd.Timedelta(days=cool_days)
                mk = nxt.strftime("%Y-%m"); roundtrips[mk] = roundtrips.get(mk, 0) + 1

        eq = equity_at(t)
        if eq <= 0:
            break

        blocked = {s for s, until in cooldown.items() if t < until}
        mkey = t.strftime("%Y-%m")
        allow_entries = roundtrips.get(mkey, 0) < budget
        dec = st.decide(bars, cfg, held=set(qty), asof=t, rotation_enabled=rotation_enabled,
                        blocked=blocked, allow_entries=allow_entries)
        current_w = {s: (q * (px(s, t) or 0)) / eq for s, q in qty.items() if eq > 0}
        deltas = st.orders_needed(dec.targets, current_w, eq, cfg)

        for s in dec.exits:
            deltas[s] = -current_w.get(s, 0.0) * eq

        for s, usd in sorted(deltas.items(), key=lambda kv: kv[1]):   # sells first
            p = px(s, nxt, "open")
            if not p:
                continue
            if usd < 0:
                sell_qty = min(qty.get(s, 0.0), abs(usd) / p)
                if sell_qty <= 0:
                    continue
                cash += sell_qty * p * (1 - cost)
                fees += sell_qty * p * cost
                qty[s] = qty.get(s, 0.0) - sell_qty
                if qty[s] * p < 1.0:
                    qty.pop(s, None); floors.pop(s, None)
                    cooldown[s] = nxt + pd.Timedelta(days=cool_days)
                    roundtrips[mkey] = roundtrips.get(mkey, 0) + 1
                trades += 1
            else:
                spend = min(usd, cash)
                if spend < cfg["sizing"]["min_notional_usd"]:
                    continue
                buy_qty = spend / (p * (1 + cost))
                cash -= spend
                fees += buy_qty * p * cost
                qty[s] = qty.get(s, 0.0) + buy_qty
                roundtrips[mkey] = roundtrips.get(mkey, 0) + 1
                a = atr_cache[s].get(nxt, np.nan)
                if use_floor and np.isfinite(a):
                    hw = max(p, floors.get(s, {}).get("high_water", 0.0))
                    floors[s] = {"high_water": hw,
                                 "floor_price": compute_floor(hw, float(a), fmult, cfg)}
                trades += 1

        # ---- ratchet every held floor on the new close (never downward)
        for s in list(qty):
            if not use_floor:
                break
            c = px(s, nxt)
            a = atr_cache[s].get(nxt, np.nan)
            if c is None or not np.isfinite(a):
                continue
            fl = floors.setdefault(s, {"high_water": c, "floor_price": compute_floor(c, float(a), fmult, cfg)})
            fl["high_water"] = max(fl["high_water"], c)
            fl["floor_price"] = max(fl["floor_price"], compute_floor(fl["high_water"], float(a), fmult, cfg))

        curve[nxt] = equity_at(nxt)

    res = Result(name, pd.Series(curve).sort_index(), trades=trades, fees_paid=fees)
    res.notes.append(f"turnover cap {budget}/mo and {cool_days}d re-entry cooldown enforced in-sim")
    return res


def run_buy_hold(bars: Dict[str, pd.DataFrame], symbols: List[str], cfg: dict,
                 start_equity: float = 10_000.0, name: str = "bh") -> Result:
    have = [s for s in symbols if s in bars and not bars[s].empty]
    if not have:
        return Result(name, pd.Series(dtype=float), notes=["no data"])
    idx = pd.DatetimeIndex(sorted(set().union(*[set(bars[s].index) for s in have])))
    w = 1.0 / len(have)
    cost = _cost(cfg)
    q = {}
    first = idx[0]
    for s in have:
        p = float(bars[s]["close"].reindex(idx).ffill().loc[first])
        q[s] = (start_equity * w * (1 - cost)) / p
    curve = {}
    for t in idx:
        curve[t] = sum(q[s] * float(bars[s]["close"].reindex(idx).ffill().loc[t]) for s in have)
    return Result(name, pd.Series(curve).sort_index(), trades=len(have), fees_paid=start_equity * cost)


def run_blend(bars: Dict[str, pd.DataFrame], weights: Dict[str, float], cfg: dict,
              start_equity: float = 10_000.0, name: str = "blend",
              drift_pp: float = 0.05) -> Result:
    """Fixed-weight blend, always fully invested, rebalanced on the first day of
    each month OR whenever any weight drifts more than `drift_pp` from target.
    This is the benchmark every active arm has to beat: if a bot cannot beat a
    monthly-rebalanced 70/30, it is not earning its complexity."""
    have = [s for s in weights if s in bars and not bars[s].empty]
    if len(have) != len(weights):
        return Result(name, pd.Series(dtype=float), notes=["blend asset missing"])
    idx = pd.DatetimeIndex(sorted(set.intersection(*[set(bars[s].index) for s in have])))
    close = pd.DataFrame({s: bars[s]["close"].reindex(idx) for s in have}).ffill().dropna()
    cost = _cost(cfg)
    q = {s: 0.0 for s in have}
    cash = start_equity
    fees = 0.0
    trades = 0
    curve = {}
    last_month = None
    for t, row in close.iterrows():
        eq = cash + sum(q[s] * row[s] for s in have)
        cur = {s: q[s] * row[s] / eq for s in have}
        drift = max(abs(cur[s] - weights[s]) for s in have)
        if last_month != t.month or drift > drift_pp:
            for s in sorted(have, key=lambda s: cur[s] - weights[s], reverse=True):  # sells first
                delta = (weights[s] - cur[s]) * eq
                if abs(delta) < 1.0:
                    continue
                fee = abs(delta) * cost
                q[s] += delta / row[s]
                cash -= delta + fee
                fees += fee
                trades += 1
            last_month = t.month
            eq = cash + sum(q[s] * row[s] for s in have)
        curve[t] = eq
    return Result(name, pd.Series(curve).sort_index(), trades=trades, fees_paid=fees)


def run_regime_only(bars: Dict[str, pd.DataFrame], cfg: dict, start_equity: float = 10_000.0) -> Result:
    """BTC, long when above its 200d SMA, flat otherwise. No floor, no rotation.
    This isolates how much of PAWL is just the regime gate."""
    sym = cfg["regime"]["anchor"]
    df = bars.get(sym)
    if df is None or df.empty:
        return Result("btc_regime", pd.Series(dtype=float), notes=["no anchor data"])
    c = df["close"]
    s = st.sma(c, cfg["regime"]["sma_days"])
    sig = (c > s).astype(float).shift(1).fillna(0.0)     # act on yesterday's close
    ret = c.pct_change().fillna(0.0)
    cost = _cost(cfg)
    turns = sig.diff().abs().fillna(0.0)
    net = sig * ret - turns * cost
    eq = (1 + net).cumprod() * start_equity
    return Result("btc_regime", eq.dropna(), trades=int(turns.sum()), fees_paid=float((turns * cost).sum() * start_equity))


def gate(bars: Dict[str, pd.DataFrame], cfg: dict) -> dict:
    arms = {
        "pawl_full": run_pawl(bars, cfg, rotation_enabled=True, name="pawl_full"),
        "pawl_core": run_pawl(bars, cfg, rotation_enabled=False, name="pawl_core"),
        "floor_tight": run_pawl(bars, cfg, rotation_enabled=False, name="floor_tight", floor_mode="tight"),
        "floor_catastrophe": run_pawl(bars, cfg, rotation_enabled=False, name="floor_catastrophe", floor_mode="catastrophe"),
        "floor_none": run_pawl(bars, cfg, rotation_enabled=False, name="floor_none", floor_mode="none"),
        "bh_btc": run_buy_hold(bars, [cfg["regime"]["anchor"]], cfg, name="bh_btc"),
        "bh_5050": run_buy_hold(bars, cfg["universe"]["core"], cfg, name="bh_5050"),
        "btc_regime": run_regime_only(bars, cfg),
    }
    blend_w = cfg["gate"].get("benchmark_blend")
    if blend_w:
        arms["blend"] = run_blend(bars, blend_w, cfg, name="blend")
    m = {k: v.metrics() for k, v in arms.items()}

    days = m.get("pawl_core", {}).get("days", 0)
    checks: List[dict] = []

    def check(name, ok, detail):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    check("history", days >= cfg["gate"]["min_history_days"],
          f"{days} days tested, {cfg['gate']['min_history_days']} required")

    # Decide the rotation sleeve on evidence, not on hope.
    edge = (m.get("pawl_full", {}).get("sharpe", 0) or 0) - (m.get("pawl_core", {}).get("sharpe", 0) or 0)
    rotation_enabled = edge >= cfg["gate"]["rotation_sharpe_edge_required"]
    checks.append({
        "check": "rotation_sleeve", "pass": True,
        "detail": (f"rotation Sharpe edge {edge:+.3f} vs required "
                   f"{cfg['gate']['rotation_sharpe_edge_required']:+.3f} -> "
                   f"{'ENABLED' if rotation_enabled else 'DISABLED, running core-only'}"),
    })

    # Decide the floor on evidence too. A tight trailing stop is intuitive and
    # was measurably harmful; but "no floor" means nothing protects a position
    # between daily cycles, and the worst day in a 6-year sample is not the
    # worst possible day. So: pick on Sharpe, but keep the catastrophe floor
    # whenever it is within the noise of going bare -- insurance you cannot
    # measure is still worth buying when it is free.
    sh = lambda k: (m.get(k, {}).get("sharpe") or -9)
    best_floor = max(["tight", "catastrophe", "none"], key=lambda k: sh(f"floor_{k}"))
    if best_floor == "none" and sh("floor_catastrophe") >= sh("floor_none") - 0.10:
        best_floor = "catastrophe"
    checks.append({
        "check": "floor_mode", "pass": True,
        "detail": (f"tight {sh('floor_tight'):.2f} / catastrophe {sh('floor_catastrophe'):.2f} / "
                   f"none {sh('floor_none'):.2f} Sharpe -> {best_floor.upper()}"),
    })

    live = "pawl_full" if rotation_enabled else "pawl_core"
    arms[live] = run_pawl(bars, cfg, rotation_enabled=rotation_enabled, name=live, floor_mode=best_floor)
    m[live] = arms[live].metrics()
    lm, bm = m.get(live, {}), m.get("bh_btc", {})

    if cfg["gate"]["must_beat_buy_hold_sharpe"]:
        check("sharpe_vs_buy_hold", (lm.get("sharpe") or -9) > (bm.get("sharpe") or 9),
              f"{live} Sharpe {lm.get('sharpe')} vs buy-hold BTC {bm.get('sharpe')}")
    if cfg["gate"]["must_beat_buy_hold_drawdown"]:
        check("drawdown_vs_buy_hold", (lm.get("max_drawdown") or -9) > (bm.get("max_drawdown") or -9),
              f"{live} max DD {lm.get('max_drawdown')} vs buy-hold BTC {bm.get('max_drawdown')}")

    if blend_w and cfg["gate"].get("must_beat_blend_sharpe", True):
        bl = m.get("blend", {})
        check("sharpe_vs_blend", (lm.get("sharpe") or -9) > (bl.get("sharpe") or 9),
              f"{live} Sharpe {lm.get('sharpe')} vs rebalanced blend {bl.get('sharpe')} "
              f"({', '.join(f'{k} {v:.0%}' for k, v in blend_w.items())})")

    check("drawdown_limit",abs(lm.get("max_drawdown") or 1) <= cfg["gate"]["max_drawdown_limit"],
          f"{live} max DD {lm.get('max_drawdown')} vs limit -{cfg['gate']['max_drawdown_limit']}")

    years = max(days / ANN, 0.5)
    # Fees as a share of AVERAGE equity. Dividing by starting equity made the
    # check fail on compounding alone: a long winning window grows the dollar
    # fees with the account while the denominator stays fixed.
    avg_eq = float(arms[live].equity.mean()) if len(arms[live].equity) else 10_000.0
    fee_drag_annual = ((lm.get("fees_paid") or 0) / avg_eq) / years
    check("fee_drag_annual", fee_drag_annual < 0.12,
          f"fees run {fee_drag_annual:.1%}/yr of average equity "
          f"(limit 12%/yr; Alpaca base tier is 25bps taker a side)")

    passed = all(c["pass"] for c in checks)
    return {
        "passed": passed,
        "live_arm": live,
        "rotation_enabled": rotation_enabled,
        "floor_mode": best_floor,
        "checks": checks,
        "arms": m,
        "cost_model": {"taker": cfg["costs"]["taker_fee"], "slippage": cfg["costs"]["assumed_slippage"]},
        "caveats": [
            "Universe is today's Alpaca crypto list replayed historically -- survivorship-biased. "
            "pawl_core (BTC+ETH) is the bias-free control and is the arm to trust.",
            "Fills are modelled at next-bar open with taker fees; real slippage on thin alt pairs will be worse.",
            "Crypto history is short and dominated by two bull cycles. A 4-year window is not a lot of regimes.",
        ],
    }


def write_gate(result: dict, path: str = "state/pawl_gate.json") -> None:
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from datetime import datetime, timezone
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
