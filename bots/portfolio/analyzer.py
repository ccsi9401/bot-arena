"""STEWARD stage 2 — portfolio analyzer.

Pure function: scan snapshot + config in, target weights + reasoning out.
Same input always produces the same portfolio (the reproducibility contract).

Logic ("more than balanced, not too aggressive"):
  Regime gate ......... risk-on only while SPY holds its 200-day trend.
  Stock sleeve (45%) .. top-6 large caps by 12-1 momentum, equal weight,
                        each must be above its own 200-day with positive momentum;
                        unfilled slots fall back to cash (natural de-risking).
  Index sleeve (25%) .. SPY/QQQ, split by 6-month momentum, trend-gated.
  Defensive (20%) ..... IEF / GLD / SHY equal thirds, always on — the ballast.
  Cash (10%+) ......... floor, plus whatever the filters release.
  Risk-off ............ 60% defensive, 40% cash. Patience is a position.
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
                raw = {s: min(w, cap) for s, w in raw.items()}
                room = {s: cap - w for s, w in raw.items() if cap - w > 1e-9}
                total_room = sum(room.values())
                for s, r_ in room.items():
                    raw[s] += over * (r_ / total_room) if total_room > 0 else 0
            raw = {s: min(w, cap) for s, w in raw.items()}
            for sym, w in raw.items():
                targets[sym] = targets.get(sym, 0) + w
            notes.append("Index sleeve: " +
                         ", ".join(f"{sym} {w*100:.1f}%" for sym, w in raw.items()) +
                         f" (per-position cap {cap*100:.0f}%).")
        else:
            notes.append("Index sleeve: no index ETF above trend — sleeve to cash.")

    # ---- defensive sleeve ----
    dfns = [sym for sym, f in syms.items() if f["sleeve"] == "defensive"]
    if dfns and weights_cfg["defensive"] > 0:
        per = weights_cfg["defensive"] / len(dfns)
        for sym in dfns:
            targets[sym] = targets.get(sym, 0) + per
        notes.append(f"Defensive ballast: {weights_cfg['defensive']*100:.0f}% split "
                     f"across {', '.join(dfns)} — always on.")

    cash = 1.0 - sum(targets.values())
    leaderboard.sort(key=lambda r: r["mom_12_1"], reverse=True)
    return {"regime_on": regime_on,
            "targets": {k: round(v, 4) for k, v in sorted(targets.items())},
            "cash_weight": round(cash, 4),
            "momentum_leaderboard": leaderboard[:15],
            "notes": notes}
