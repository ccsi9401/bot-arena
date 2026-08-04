"""Stage 2 — SCALPEL analyzer (intraday session momentum).

Input: scan.json snapshot + config + current cycle time. Output: trade intents.
Deterministic — no I/O, no broker, no randomness. Sizing happens in the validator.
"""
from __future__ import annotations

from datetime import datetime


def analyze(scan: dict, cfg: dict, cycle_et_hhmm: str) -> dict:
    s = cfg["strategy"]
    intents: list[dict] = []
    notes: list[str] = []

    liquidate_cycle = cfg["schedule"]["liquidate_cycle_et"]
    last_entry = cfg["schedule"]["last_entry_cycle_et"]

    if cycle_et_hhmm >= liquidate_cycle:
        return {
            "cycle_et": cycle_et_hhmm,
            "intents": [{"action": "liquidate_all",
                         "reasoning": "End-of-day cycle: SCALPEL never holds overnight."}],
            "notes": ["liquidation cycle"],
        }

    entries_allowed = cycle_et_hhmm <= last_entry
    if not entries_allowed:
        notes.append(f"cycle {cycle_et_hhmm} past last-entry {last_entry}; manage only")

    candidates = []
    for sym, f in scan["symbols"].items():
        sess = f.get("session")
        if not sess or sess.get("rs_percentile") is None:
            continue
        reasons = []
        if f["close"] < s["min_price"]:
            continue
        if f["avg_dollar_vol_20d"] < s["min_avg_dollar_vol"]:
            continue
        last = sess["last"]
        checks = {
            "above_or_high": last > sess["or_high"],
            "above_vwap": (not s["require_above_vwap"]) or last > sess["vwap"],
            "rs_top_quintile": sess["rs_percentile"] >= s["min_rel_strength_quintile"],
            "volume_pace": sess["volume_pace"] >= s["min_volume_pace"],
        }
        if all(checks.values()):
            stop = last - s["stop_atr_mult"] * f["atr14"]
            if stop >= last:  # degenerate ATR
                continue
            risk_per_share = last - stop
            candidates.append({
                "action": "buy",
                "symbol": sym,
                "entry_limit": round(last * 1.001, 2),  # tiny cushion over last
                "stop": round(stop, 2),
                "target": round(last + s["target_r_mult"] * risk_per_share, 2),
                "score": sess["rs_percentile"] * min(sess["volume_pace"], 3.0),
                "reasoning": (
                    f"Broke opening-range high {sess['or_high']:.2f} (last {last:.2f}), "
                    f"above VWAP {sess['vwap']:.2f}, RS pct {sess['rs_percentile']:.2f}, "
                    f"volume pace {sess['volume_pace']:.2f}x. "
                    f"Stop {s['stop_atr_mult']}xATR={f['atr14']:.2f} below."
                ),
                "checks": checks,
            })

    if entries_allowed:
        candidates.sort(key=lambda c: c["score"], reverse=True)
        intents = candidates[: cfg["risk"]["max_positions"]]
        notes.append(f"{len(candidates)} passed filters; proposing top {len(intents)}")
    return {"cycle_et": cycle_et_hhmm, "intents": intents, "notes": notes,
            "candidates_considered": len(candidates)}
