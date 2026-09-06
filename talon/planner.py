"""Scoring, ranking, sizing and risk gates.

``plan()`` is THE sizing loop. The live cycle calls it with ``i=None`` (last bar);
the backtest calls it with an integer bar index. There is no second copy anywhere —
if you are tempted to write sizing logic in the backtest, import this instead.

Order of operations inside plan():
  1. regime gate          (off + block_new_entries_when_off -> return empty, nothing else runs)
  2. score each symbol    (signal_frame -> ensemble_score, plus ATR, realised vol, momentum)
  3. cross-sectional momentum (top_k names with positive momentum get the xs weight, re-normalised)
  4. filter + rank        (min_score, skip open symbols, free slots only)
  5. size                 (risk_dollars / r_unit, inverse-vol scale, position cap, min notional)
  6. gross cap            (pro-rata haircut, never drop-the-tail)
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from talon import strategies as S
from talon.floor import r_unit_for


# ---------------------------------------------------------------------------
# Per-symbol features (memoisable — frames do not change during a backtest)
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """One row per bar: score, atr, vol, mom, plus one 0/1 column per active voter."""
    scfg = cfg["strategies"]
    pcfg = cfg["rising_floor"]["position"]
    rcfg = cfg["risk"]
    sigs = S.signal_frame(df, scfg)
    feats = pd.DataFrame(index=df.index)
    for col in sigs.columns:
        feats[col] = sigs[col]
    feats["score"] = S.ensemble_score(sigs, scfg)
    feats["atr"] = S.atr(df, pcfg["atr_period"])
    feats["vol"] = S.realised_vol(df["close"], rcfg["vol_window"], cfg["data"]["periods_per_year"])
    feats["mom"] = S.momentum_score(df, scfg["momentum_score"])
    feats["close"] = df["close"]
    feats["valid"] = df["close"].notna().cumsum()   # point-in-time eligibility counter
    return feats


def _features(symbol: str, df: pd.DataFrame, cfg: dict, cache: dict | None) -> pd.DataFrame:
    if cache is not None:
        hit = cache.get(symbol)
        if hit is not None and len(hit) == len(df) and hit.index[-1] == df.index[-1]:
            return hit
    feats = compute_features(df, cfg)
    if cache is not None:
        cache[symbol] = feats
    return feats


def _bar_index(df: pd.DataFrame, i: int | None) -> int:
    n = len(df)
    if i is None:
        return n - 1
    if i < 0:
        return n + i
    return i


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------

def plan(bars: Mapping[str, pd.DataFrame], benchmark: pd.DataFrame, equity: float, cfg: dict,
         open_symbols: Iterable[str] | Mapping[str, float], i: int | None = None,
         feature_cache: dict | None = None, exclude: Iterable[str] = ()) -> dict:
    """Return {regime_on, targets, candidates, notes}.

    bars          {symbol: frame} — frames should be aligned (data.align) so bar ``i``
                  means the same timestamp everywhere.
    benchmark     the regime frame (e.g. BTC/USD).
    equity        the equity to size on. The live cycle passes TRADEABLE equity
                  (total minus the locked reserve), never total.
    open_symbols  names already held. A dict {symbol: notional} lets the gross cap
                  count existing exposure; a plain list counts it as unknown (0).
    i             None = last bar (live); an int = that bar (backtest).
    feature_cache optional dict the caller owns; features are memoised per symbol.
    exclude       names that may not be entered this bar — the caller passes the symbols it
                  just stopped out of, so a floor exit is never followed by a same-bar re-entry.
    """
    notes: list[str] = []
    ecfg, rcfg, xcfg = cfg["ensemble"], cfg["risk"], cfg["strategies"]["momentum_score"]
    min_bars = int(cfg["data"]["min_bars"])

    if isinstance(open_symbols, Mapping):
        open_notional = {k: float(v) for k, v in open_symbols.items()}
    else:
        open_notional = {k: 0.0 for k in open_symbols}
    open_set = set(open_notional)

    # 1. regime gate ---------------------------------------------------------
    bi = _bar_index(benchmark, i)
    reg = S.regime_on(benchmark, cfg)
    on = bool(reg.iloc[bi]) if 0 <= bi < len(reg) else False
    if not on and cfg["regime"].get("block_new_entries_when_off", True):
        notes.append(f"Regime OFF: benchmark below its {cfg['regime']['sma_days']}-day SMA — no new entries.")
        return {"regime_on": False, "targets": {}, "candidates": [], "notes": notes}
    notes.append("Regime ON." if on else "Regime OFF but entries not blocked by config.")

    # 2. score ---------------------------------------------------------------
    voters = S.active_strategies(cfg["strategies"])
    cands: list[dict] = []
    for sym, df in bars.items():
        k = _bar_index(df, i)
        if k < 0 or k >= len(df):
            continue
        f = _features(sym, df, cfg, feature_cache)
        row = f.iloc[k]
        if int(row["valid"]) < min_bars:
            continue                        # not enough of its OWN history yet (late listing / gap)
        score, atr_v, vol_v, mom_v, px = (row["score"], row["atr"], row["vol"], row["mom"], row["close"])
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (score, atr_v, px)):
            continue
        cands.append({
            "symbol": sym,
            "base_score": float(score),
            "score": float(score),
            "atr": float(atr_v),
            "vol": float(vol_v) if not math.isnan(float(vol_v)) else float("nan"),
            "momentum": float(mom_v) if not math.isnan(float(mom_v)) else float("nan"),
            "price": float(px),
            "reasons": [v for v in voters if int(row[v]) == 1],
            "xs_momentum": False,
        })

    # 3. cross-sectional momentum -------------------------------------------
    w_sig = sum(float(cfg["strategies"][v]["weight"]) for v in voters)
    w_xs = float(xcfg.get("weight", 0.0))
    if w_xs > 0 and cands:
        ranked = sorted([c for c in cands if not math.isnan(c["momentum"]) and c["momentum"] > 0],
                        key=lambda c: c["momentum"], reverse=True)[: int(xcfg["top_k"])]
        top = {c["symbol"] for c in ranked}
        for c in cands:
            flag = 1.0 if c["symbol"] in top else 0.0
            c["xs_momentum"] = bool(flag)
            c["score"] = (c["base_score"] * w_sig + w_xs * flag) / (w_sig + w_xs)
            if flag:
                c["reasons"] = c["reasons"] + ["momentum_score"]

    # 4. filter and rank -----------------------------------------------------
    min_score = float(ecfg["min_score"])
    slots = int(ecfg["max_positions"]) - len(open_set)
    blocked = open_set | set(exclude)
    eligible = [c for c in cands if c["symbol"] not in blocked and c["score"] >= min_score]
    eligible.sort(key=lambda c: (c["score"], 0.0 if math.isnan(c["momentum"]) else c["momentum"]), reverse=True)
    if slots <= 0:
        notes.append(f"No free slots ({len(open_set)}/{ecfg['max_positions']} open).")
        return {"regime_on": on, "targets": {}, "candidates": cands, "notes": notes}
    chosen = eligible[:slots]
    notes.append(f"{len(eligible)} eligible (score >= {min_score}), {len(chosen)} selected for {slots} free slot(s).")
    if not chosen:
        return {"regime_on": on, "targets": {}, "candidates": cands, "notes": notes}

    # 5. size ----------------------------------------------------------------
    equity = float(equity)
    risk_dollars = equity * float(rcfg["risk_per_trade_pct"])
    for c in chosen:
        c["r_unit"] = r_unit_for(c["price"], c["atr"], cfg)
        c["qty"] = risk_dollars / c["r_unit"]
        c["vol_scale"] = 1.0

    if rcfg.get("inverse_vol_sizing", True) and len(chosen) > 0:
        vols = np.array([c["vol"] for c in chosen], dtype=float)
        ok = np.isfinite(vols) & (vols > 0)
        if ok.any():
            inv = np.where(ok, 1.0 / np.where(ok, vols, 1.0), np.nan)
            inv = np.where(ok, inv, np.nanmean(inv))          # unknown vol -> average scale
            scale = inv / inv.mean()                           # mean 1.0: total risk budget unchanged
            lo, hi = (float(x) for x in rcfg["inverse_vol_clip"])
            scale = np.clip(scale, lo, hi)
            for c, s in zip(chosen, scale):
                c["vol_scale"] = float(s)
                c["qty"] *= float(s)

    cap = equity * float(rcfg["max_position_weight"])
    min_notional = float(rcfg["min_order_notional"])
    sized: list[dict] = []
    for c in chosen:
        notional = c["qty"] * c["price"]
        if notional > cap:
            notional = cap
            c["capped"] = True
        c["notional"] = notional
        c["qty"] = notional / c["price"]
        if notional < min_notional:
            notes.append(f"{c['symbol']}: {notional:.2f} below min_order_notional, dropped.")
            continue
        sized.append(c)

    # 6. gross cap -----------------------------------------------------------
    gross_cap = equity * float(rcfg["max_gross_exposure"])
    existing = sum(open_notional.values())
    new_total = sum(c["notional"] for c in sized)
    if sized and existing + new_total > gross_cap:
        room = max(gross_cap - existing, 0.0)
        factor = room / new_total if new_total > 0 else 0.0
        notes.append(f"Gross cap: {existing + new_total:.0f} > {gross_cap:.0f}; new entries haircut x{factor:.3f}.")
        kept = []
        for c in sized:
            c["notional"] *= factor
            c["qty"] = c["notional"] / c["price"]
            c["haircut"] = factor
            if c["notional"] >= min_notional:
                kept.append(c)
            else:
                notes.append(f"{c['symbol']}: below min_order_notional after haircut, dropped.")
        sized = kept

    targets = {
        c["symbol"]: {
            "notional": round(c["notional"], 2),
            "qty": c["qty"],
            "price": c["price"],
            "score": round(c["score"], 4),
            "atr": c["atr"],
            "r_unit": c["r_unit"],
            "vol": c["vol"],
            "vol_scale": round(c["vol_scale"], 4),
            "momentum": c["momentum"],
            "reasons": c["reasons"],
        }
        for c in sized
    }
    return {"regime_on": on, "targets": targets, "candidates": cands, "notes": notes}


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------

def kill_switch_state(equity: float, high_water: float, day_start: float, cfg: dict) -> tuple[bool, str]:
    """(halted, reason). Fires on drawdown from high-water and on the daily loss breaker."""
    rcfg = cfg["risk"]
    equity, high_water, day_start = float(equity), float(high_water), float(day_start)
    if high_water > 0:
        dd = 1.0 - equity / high_water
        if dd >= float(rcfg["kill_switch_dd_pct"]):
            return True, (f"kill switch: drawdown {dd:.1%} from high-water {high_water:.2f} "
                          f">= {float(rcfg['kill_switch_dd_pct']):.0%}")
    if day_start > 0:
        day_loss = 1.0 - equity / day_start
        if day_loss >= float(rcfg["daily_loss_breaker_pct"]):
            return True, (f"daily loss breaker: down {day_loss:.1%} from day start {day_start:.2f} "
                          f">= {float(rcfg['daily_loss_breaker_pct']):.0%}")
    return False, ""


HALT_FLATTEN = "flatten"
HALT_BLOCK_ENTRIES = "block_entries"
DAILY_BREAKER_PREFIX = "daily loss breaker"


def halt_action(reason: str, cfg: dict) -> str:
    """What a halt does. The kill switch always flattens. The daily breaker follows
    risk.daily_breaker_action: block_entries (no new risk; the position floors handle exits,
    the circuit-breaker convention) or flatten."""
    if reason.startswith(DAILY_BREAKER_PREFIX):
        action = str(cfg["risk"].get("daily_breaker_action", HALT_FLATTEN))
        if action not in (HALT_FLATTEN, HALT_BLOCK_ENTRIES):
            raise ValueError(f"risk.daily_breaker_action must be {HALT_FLATTEN} or {HALT_BLOCK_ENTRIES}, got {action!r}")
        return action
    return HALT_FLATTEN
