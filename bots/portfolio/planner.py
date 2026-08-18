"""STEWARD stage 3 — validator/planner. The only stage allowed to say no.

Takes the analyzer's target weights + the live account, applies every cap, and
emits a concrete rebalance plan with reasons. Trades only what drifted beyond
the band — investing shouldn't fidget — but once a trade IS triggered it is
sized to actually close the gap.

Sizing (v2, 2026-08-18): orders are sized in dollars, not floored share counts.
  - fractionable symbols  -> notional (dollar) orders, exact to the cent
  - non-fractionable ones -> integer shares plus a largest-remainder sweep that
    spends the truncation residue instead of parking it in cash
v1 floored every order to whole shares, which left the portfolio ~5 points
under-invested (see journal/steward_20260814_1551) with no way to self-correct,
because the top-up trades were themselves smaller than one share.
"""
from __future__ import annotations

SIZING_VERSION = 2

# Don't spend the last cent of buying power: prices move between planning and fill.
_BUY_BUFFER_FRAC = 0.0025


def _order(sym, side, *, qty=None, notional=None, px, cur, tgt, reason):
    return {"symbol": sym, "side": side, "qty": qty, "notional": notional,
            "ref_price": px, "from_w": round(cur, 4), "to_w": round(tgt, 4),
            "reason": reason}


def plan(targets: dict[str, float], analysis: dict, account: dict,
         positions: list[dict], prices: dict[str, float], cfg: dict,
         kill_tripped: bool, fractionable: dict[str, bool] | None = None) -> dict:
    r = cfg["risk"]
    equity = account["equity"]
    frac_map = fractionable or {}
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
    eq_syms = [s for s in targets if s not in cfg["universe"]["defensive_etfs"]]
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
    held_qty: dict[str, float] = {}
    for p in positions:
        current[p["symbol"]] = p["market_value"] / equity if equity > 0 else 0
        held_qty[p["symbol"]] = float(p["qty"])

    # ---- sizing: dollars first, shares only where forced ----
    band = cfg["strategy"]["drift_band_abs"]
    sells: list[dict] = []
    buys: list[dict] = []
    whole_share_gaps: list[tuple[str, float, float, float, float]] = []  # sym,resid$,px,cur,tgt
    skipped_dust: list[str] = []

    for sym in sorted(set(current) | set(targets)):
        cur, tgt = current.get(sym, 0.0), targets.get(sym, 0.0)
        drift = tgt - cur
        px = prices.get(sym)
        if px is None or px <= 0:
            if abs(drift) > band:
                notes.append(f"{sym}: no live price — skipped this cycle.")
            continue
        if abs(drift) <= band:
            continue

        delta = abs(drift) * equity
        is_frac = bool(frac_map.get(sym, False))
        why = f"{'Trim' if drift < 0 else 'Add'} {cur*100:.1f}% → {tgt*100:.1f}%"

        if drift < 0:
            # Full exit: sell the exact position, never leave fractional dust behind.
            if tgt <= 1e-9 and sym in held_qty:
                sells.append(_order(sym, "sell", qty=held_qty[sym], px=px, cur=cur,
                                    tgt=tgt, reason=f"Exit {cur*100:.1f}% → 0.0%"))
                continue
            if is_frac:
                q = round(min(delta / px, held_qty.get(sym, delta / px)), 6)
                if q > 0:
                    sells.append(_order(sym, "sell", qty=q, px=px, cur=cur, tgt=tgt,
                                        reason=why))
            else:
                q = int(min(delta / px, held_qty.get(sym, delta / px)))
                if q >= 1:
                    sells.append(_order(sym, "sell", qty=q, px=px, cur=cur, tgt=tgt,
                                        reason=why))
                else:
                    skipped_dust.append(sym)
            continue

        if is_frac:
            # qty is carried alongside for the report/journal; the executor prefers
            # notional so the fill is exact to the cent regardless of price drift.
            buys.append(_order(sym, "buy", qty=round(delta / px, 4),
                               notional=round(delta, 2), px=px, cur=cur,
                               tgt=tgt, reason=why))
        else:
            q = int(delta / px)
            if q >= 1:
                buys.append(_order(sym, "buy", qty=q, px=px, cur=cur, tgt=tgt, reason=why))
            whole_share_gaps.append((sym, delta - q * px, px, cur, tgt))

    # ---- largest-remainder sweep for the non-fractionable leftovers ----
    # Whole-share truncation is what parked 5.3% of the book in cash on 2026-08-14.
    # Spend the residue on whoever is furthest below target, while the cash floor holds.
    sell_proceeds = sum((o["qty"] or 0) * o["ref_price"] for o in sells)
    budget = (account.get("cash", 0.0) + sell_proceeds
              - r["min_cash_weight"] * equity - _BUY_BUFFER_FRAC * equity)
    spent = sum(o["notional"] if o["notional"] is not None
                else (o["qty"] or 0) * o["ref_price"] for o in buys)
    swept = 0
    for _ in range(200):
        cands = [(resid, sym, px, cur, tgt) for sym, resid, px, cur, tgt in whole_share_gaps
                 if resid >= px * 0.5 and spent + px <= budget]
        if not cands:
            break
        resid, sym, px, cur, tgt = max(cands)
        existing = next((o for o in buys if o["symbol"] == sym and o["side"] == "buy"), None)
        if existing and existing["qty"] is not None:
            existing["qty"] += 1
        else:
            buys.append(_order(sym, "buy", qty=1, px=px, cur=cur, tgt=tgt,
                               reason=f"Add {cur*100:.1f}% → {tgt*100:.1f}%"))
        whole_share_gaps = [(s, (resid - px) if s == sym else rr, p, c, t)
                            for s, rr, p, c, t in whole_share_gaps]
        spent += px
        swept += 1
    if swept:
        notes.append(f"Whole-share sweep: {swept} extra share(s) bought to spend "
                     f"rounding residue instead of leaving it in cash.")

    # ---- cash guard: never plan more buying than the account can fund ----
    if spent > budget > 0:
        scale = budget / spent
        for o in buys:
            if o["notional"] is not None:
                o["notional"] = round(o["notional"] * scale, 2)
            elif o["qty"] is not None:
                o["qty"] = int(o["qty"] * scale)
        buys = [o for o in buys if (o["notional"] or 0) > 1 or (o["qty"] or 0) >= 1]
        notes.append(f"Buys scaled to {scale*100:.0f}% of plan to respect the "
                     f"{r['min_cash_weight']*100:.0f}% cash floor.")
    if skipped_dust:
        notes.append("Below one share, left as-is: " + ", ".join(sorted(skipped_dust)) + ".")

    orders = sells + buys  # sells first so cash is available for buys

    planned_invested = sum(
        (o["notional"] if o["notional"] is not None else (o["qty"] or 0) * o["ref_price"])
        * (1 if o["side"] == "buy" else -1) for o in orders)
    held_value = sum(p["market_value"] for p in positions)
    projected_cash_w = round(1 - (held_value + planned_invested) / equity, 4) if equity else 0

    return {"targets_final": {k: round(v, 4) for k, v in sorted(targets.items())},
            "current_weights": {k: round(v, 4) for k, v in sorted(current.items())},
            "cash_target": round(1 - sum(targets.values()), 4),
            "projected_cash_weight": projected_cash_w,
            "sizing_version": SIZING_VERSION,
            "orders": orders, "notes": notes, "halts": halts,
            "drawdown_pct": round(dd_pct, 2)}
