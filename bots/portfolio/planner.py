"""STEWARD stage 3 — validator/planner. The only stage allowed to say no.

Takes the analyzer's target weights + the live account, applies every cap, and
emits a concrete rebalance plan (integer-share orders) with reasons. Trades only
what drifted beyond the band — investing shouldn't fidget.
"""
from __future__ import annotations


def plan(targets: dict[str, float], analysis: dict, account: dict,
         positions: list[dict], prices: dict[str, float], cfg: dict,
         kill_tripped: bool) -> dict:
    r = cfg["risk"]
    equity = account["equity"]
    notes: list[str] = []
    halts: list[str] = []
    targets = dict(targets)

    # ---- account-level gates ----
    dd_pct = (cfg["starting_equity"] - equity) / cfg["starting_equity"] * 100
    if kill_tripped or dd_pct >= r["kill_switch_drawdown_pct"]:
        halts.append(f"KILL SWITCH: drawdown {dd_pct:.1f}% — forcing risk-off targets; "
                     "manual review required to resume risk-on.")
        w = cfg["strategy"]["weights"]["risk_off"]
        dfns = cfg["universe"]["defensive_etfs"]
        targets = {sym: w["defensive"] / len(dfns) for sym in dfns}

    # ---- weight caps ----
    for sym in list(targets):
        if targets[sym] > r["max_position_weight"]:
            notes.append(f"{sym} capped at {r['max_position_weight']*100:.0f}% "
                         f"(was {targets[sym]*100:.1f}%).")
            targets[sym] = r["max_position_weight"]
    eq_syms = [s for s in targets
               if s not in cfg["universe"]["defensive_etfs"]]
    eq_w = sum(targets[s] for s in eq_syms)
    if eq_w > r["max_equity_weight"]:
        scale = r["max_equity_weight"] / eq_w
        for s in eq_syms:
            targets[s] *= scale
        notes.append(f"Equity sleeve scaled down to {r['max_equity_weight']*100:.0f}% cap.")
    total_w = sum(targets.values())
    if total_w > 1 - r["min_cash_weight"]:
        scale = (1 - r["min_cash_weight"]) / total_w
        targets = {s: w * scale for s, w in targets.items()}
        notes.append(f"All targets scaled to keep {r['min_cash_weight']*100:.0f}% cash floor.")

    # ---- current weights ----
    current: dict[str, float] = {}
    for p in positions:
        current[p["symbol"]] = p["market_value"] / equity if equity > 0 else 0

    # ---- orders: only where drift exceeds the band ----
    band = cfg["strategy"]["drift_band_abs"]
    orders = []
    all_syms = sorted(set(current) | set(targets))
    for sym in all_syms:
        cur, tgt = current.get(sym, 0.0), targets.get(sym, 0.0)
        drift = tgt - cur
        px = prices.get(sym)
        if px is None or px <= 0:
            if abs(drift) > band:
                notes.append(f"{sym}: no live price — skipped this cycle.")
            continue
        if abs(drift) <= band:
            continue
        qty = int(abs(drift) * equity / px)
        if qty < 1:
            continue
        orders.append({"symbol": sym, "side": "sell" if drift < 0 else "buy",
                       "qty": qty, "ref_price": px,
                       "from_w": round(cur, 4), "to_w": round(tgt, 4),
                       "reason": f"{'Trim' if drift < 0 else 'Add'} "
                                 f"{cur*100:.1f}% → {tgt*100:.1f}%"})
    # sells first so cash is available for buys
    orders.sort(key=lambda o: 0 if o["side"] == "sell" else 1)

    return {"targets_final": {k: round(v, 4) for k, v in sorted(targets.items())},
            "current_weights": {k: round(v, 4) for k, v in sorted(current.items())},
            "cash_target": round(1 - sum(targets.values()), 4),
            "orders": orders, "notes": notes, "halts": halts,
            "drawdown_pct": round(dd_pct, 2)}
