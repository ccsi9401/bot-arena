"""STEWARD stage 2 — portfolio analyzer.

Pure function: scan snapshot + config in, target weights + reasoning out.
Same input always produces the same portfolio (the reproducibility contract).

Logic ("more than balanced, not too aggressive"):
  Regime gate ......... risk-on only while SPY holds its 200-day trend.
  Stock sleeve ........ top-N large caps by 12-1 momentum, equal weight, each
                        above its own 200-day with positive momentum; unfilled
                        slots fall back to cash (natural de-risking).
                        DISABLED since 2026-08-22 — see config/steward.yaml.
  Index sleeve ........ SPY/QQQ, split by 6-month momentum, trend-gated, each
                        subject to risk.max_position_weight. Whatever the cap or
                        the trend filter will not allocate is disposed of
                        explicitly via strategy.index_residue_to.
  Defensive ........... IEF / GLD / SHY equal thirds, always on — the ballast,
                        plus any index residue routed here.
  Cash ................ floor, plus whatever the filters release.
  Risk-off ............ 60% defensive, 40% cash. Patience is a position.

Sleeve weights are config, not code: the numbers above move with
strategy.weights, which is currently 0/70/20/10 (index-only).
"""
from __future__ import annotations


def analyze(scan: dict, cfg: dict) -> dict:
    s = cfg["strategy"]
    syms = scan["symbols"]
    notes: list[str] = []

    bench = syms.get(cfg["benchmark"], {})
    regime_on = bool(bench.get("above_200sma"))
    weights_cfg = s["weights"]["risk_on" if regime_on else "risk_off"]
    notes.append(f"Regime: {'RISK-ON' if regime_on else 'RISK-OFF'} — "
                 f"{cfg['benchmark']} {'above' if regime_on else 'below'} its 200-day SMA.")

    targets: dict[str, float] = {}
    leaderboard = []

    # ---- stock sleeve ----
    if weights_cfg["stocks"] > 0:
        cands = []
        for sym, f in syms.items():
            if f["sleeve"] != "stock" or f["mom_12_1"] is None:
                continue
            leaderboard.append({"symbol": sym, "mom_12_1": round(f["mom_12_1"], 4),
                                "above_200sma": f["above_200sma"]})
            if (f["close"] >= s["min_price"]
                    and f["avg_dollar_vol_20d"] >= s["min_avg_dollar_vol"]
                    and f["above_200sma"] and f["mom_12_1"] > 0):
                cands.append((sym, f["mom_12_1"]))
        cands.sort(key=lambda kv: kv[1], reverse=True)
        picked = cands[: s["top_n_stocks"]]
        per = weights_cfg["stocks"] / s["top_n_stocks"]
        for sym, mom in picked:
            targets[sym] = per
        notes.append(
            f"Stock sleeve: {len(picked)}/{s['top_n_stocks']} qualified "
            f"({', '.join(f'{sym} +{m*100:.0f}%' for sym, m in picked) or 'none'}); "
            f"{(s['top_n_stocks'] - len(picked)) * per * 100:.1f}% of the sleeve "
            f"falls back to cash.")

    # ---- index sleeve (position cap respected here, not left to the planner) ----
    # Whatever the cap or the trend filter refuses to allocate comes back here as
    # `index_residue` and is disposed of explicitly below, rather than falling
    # through to cash by accident. See the note on strategy.index_residue_to.
    index_residue = 0.0
    index_qualified = 0
    if weights_cfg["index"] > 0:
        cap = cfg["risk"]["max_position_weight"]
        idx = [(sym, f.get("mom_6m") or 0.0) for sym, f in syms.items()
               if f["sleeve"] == "index" and f["above_200sma"]]
        pos = [(sym, max(m, 0.0)) for sym, m in idx]
        total = sum(m for _, m in pos)
        if pos:
            if total > 0:
                raw = {sym: weights_cfg["index"] * m / total for sym, m in pos}
            else:
                raw = {sym: weights_cfg["index"] / len(pos) for sym, _ in pos}
            # cap each ETF; redistribute overflow to uncapped ETFs, residue to cash
            for _ in range(3):
                over = sum(max(w - cap, 0) for w in raw.values())
                if over <= 1e-9:
                    break
                raw = {k: min(w, cap) for k, w in raw.items()}
                room = {k: cap - w for k, w in raw.items() if cap - w > 1e-9}
                total_room = sum(room.values())
                # NB: this loop used to bind `s`, clobbering `s = cfg["strategy"]`
                # from the top of the function with a ticker string. Nothing read
                # `s` after it, so it never bit — a trap armed for the next edit.
                for k, r_ in room.items():
                    raw[k] += over * (r_ / total_room) if total_room > 0 else 0
            raw = {k: min(w, cap) for k, w in raw.items()}
            index_qualified = len(raw)
            # The cap can only absorb `cap` per ETF. With one qualifying ETF and a
            # 70% sleeve that leaves 30pp with nowhere to go — the redistribution
            # loop above finds no uncapped sibling to hand it to.
            index_residue = max(0.0, weights_cfg["index"] - sum(raw.values()))
            for sym, w in raw.items():
                targets[sym] = targets.get(sym, 0) + w
            notes.append("Index sleeve: " +
                         ", ".join(f"{sym} {w*100:.1f}%" for sym, w in raw.items()) +
                         f" (per-position cap {cap*100:.0f}%).")
        else:
            index_residue = weights_cfg["index"]
            notes.append("Index sleeve: no index ETF above trend.")

    # ---- index residue: ballast, not idle cash ----
    # Before 2026-08-29 this was implicit and invisible. Index-only weights the
    # sleeve at 70% across SPY and QQQ with a 40% per-position cap, so ANY week
    # where QQQ sits below its own 200-day — an ordinary rotation out of tech,
    # not a crisis — left SPY capped at 40%, 20% defensive and **40% in cash**,
    # in a RISK-ON regime. It was undetectable downstream: the planner derives
    # cash_target from the targets it is handed, so target and actual agreed
    # perfectly at 40% and the cash-drag sweep saw a healthy portfolio.
    # Routing the residue to the ballast keeps the book ~90% invested and leaves
    # cash_target at its configured 10%, so the cash invariant still means
    # something. Set index_residue_to: cash to restore the old behaviour.
    dfns = [sym for sym, f in syms.items() if f["sleeve"] == "defensive"]
    defensive_w = weights_cfg["defensive"]
    residue_to = s.get("index_residue_to", "cash")
    if index_residue > 1e-9 and residue_to == "defensive" and dfns:
        defensive_w += index_residue
        notes.append(
            f"Index sleeve short {index_residue*100:.1f}pp "
            f"({index_qualified} of {len(cfg['universe']['index_etfs'])} index ETFs "
            f"above trend, capped at {cfg['risk']['max_position_weight']*100:.0f}%) — "
            "residue routed to the defensive ballast rather than left idle in cash.")
        index_residue = 0.0
    elif index_residue > 1e-9:
        notes.append(f"Index sleeve short {index_residue*100:.1f}pp — held as cash "
                     f"(index_residue_to: {residue_to}).")

    # ---- defensive sleeve ----
    if dfns and defensive_w > 0:
        per = defensive_w / len(dfns)
        for sym in dfns:
            targets[sym] = targets.get(sym, 0) + per
        notes.append(f"Defensive ballast: {defensive_w*100:.0f}% split "
                     f"across {', '.join(dfns)} — always on.")

    cash = 1.0 - sum(targets.values())
    leaderboard.sort(key=lambda r: r["mom_12_1"], reverse=True)
    return {"regime_on": regime_on,
            "targets": {k: round(v, 4) for k, v in sorted(targets.items())},
            "cash_weight": round(cash, 4),
            "index_residue_pp": round(index_residue * 100, 2),
            "momentum_leaderboard": leaderboard[:15],
            "notes": notes}
