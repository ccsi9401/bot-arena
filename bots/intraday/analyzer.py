"""Stage 2 — SCALPEL analyzer (intraday). Two selectable styles:

  orb_momentum  — v1: opening-range breakout + relative strength (kept for
                  reproducibility of journaled runs; failed the launch gate)
  dip_reversion — v3: buy sharp morning weakness in structurally strong stocks,
                  target the bounce; flat by close

Input: scan.json snapshot + config + current cycle time. Output: trade intents.
Deterministic — no I/O, no broker, no randomness. Sizing happens in the validator.
"""
from __future__ import annotations


def analyze(scan: dict, cfg: dict, cycle_et_hhmm: str) -> dict:
    s = cfg["strategy"]
    style = s.get("style", "orb_momentum")

    liquidate_cycle = cfg["schedule"]["liquidate_cycle_et"]
    last_entry = cfg["schedule"]["last_entry_cycle_et"]

    if cycle_et_hhmm >= liquidate_cycle:
        return {
            "cycle_et": cycle_et_hhmm, "style": style,
            "intents": [{"action": "liquidate_all",
                         "reasoning": "End-of-day cycle: SCALPEL never holds overnight."}],
            "notes": ["liquidation cycle"], "candidates_considered": 0,
        }

    notes: list[str] = []
    entries_allowed = cycle_et_hhmm <= last_entry
    if not entries_allowed:
        notes.append(f"cycle {cycle_et_hhmm} past last-entry {last_entry}; manage only")

    if style == "dip_reversion":
        candidates = _dip_reversion_candidates(scan, s)
    else:
        candidates = _orb_momentum_candidates(scan, s)

    intents: list[dict] = []
    if entries_allowed:
        candidates.sort(key=lambda c: c["score"], reverse=True)
        intents = candidates[: cfg["risk"]["max_positions"]]
        notes.append(f"{len(candidates)} passed filters; proposing top {len(intents)}")
    return {"cycle_et": cycle_et_hhmm, "style": style, "intents": intents,
            "notes": notes, "candidates_considered": len(candidates)}


def _base_ok(f: dict, s: dict) -> bool:
    return (f.get("session") is not None
            and f["session"].get("rs_percentile") is not None
            and f["close"] >= s["min_price"]
            and f["avg_dollar_vol_20d"] >= s["min_avg_dollar_vol"])


def _orb_momentum_candidates(scan: dict, s: dict) -> list[dict]:
    out = []
    for sym, f in scan["symbols"].items():
        if not _base_ok(f, s):
            continue
        sess = f["session"]
        last = sess["last"]
        checks = {
            "above_or_high": last > sess["or_high"],
            "above_vwap": (not s.get("require_above_vwap", True)) or last > sess["vwap"],
            "rs_top_quintile": sess["rs_percentile"] >= s["min_rel_strength_quintile"],
            "volume_pace": sess["volume_pace"] >= s["min_volume_pace"],
        }
        if not all(checks.values()):
            continue
        stop = last - s["stop_atr_mult"] * f["atr14"]
        if stop >= last:
            continue
        rps = last - stop
        out.append({
            "action": "buy", "symbol": sym,
            "entry_limit": round(last * 1.001, 2),
            "stop": round(stop, 2),
            "target": round(last + s["target_r_mult"] * rps, 2),
            "score": sess["rs_percentile"] * min(sess["volume_pace"], 3.0),
            "reasoning": (
                f"Broke opening-range high {sess['or_high']:.2f} (last {last:.2f}), "
                f"above VWAP {sess['vwap']:.2f}, RS pct {sess['rs_percentile']:.2f}, "
                f"volume pace {sess['volume_pace']:.2f}x. "
                f"Stop {s['stop_atr_mult']}xATR={f['atr14']:.2f} below."
            ),
            "checks": checks,
        })
    return out


def _dip_reversion_candidates(scan: dict, s: dict) -> list[dict]:
    """Buy the dip: daily-uptrend stock, down hard vs its own normal range today,
    on real volume, still below VWAP (stretched). Bounce target, EOD flat."""
    out = []
    for sym, f in scan["symbols"].items():
        if not _base_ok(f, s):
            continue
        sess = f["session"]
        last = sess["last"]
        atr_pct = f["atr14"] / f["close"] if f["close"] > 0 else 0
        if atr_pct <= 0:
            continue
        stretch = -sess["ret_since_open"] / atr_pct  # how many "daily ranges" down
        checks = {
            "daily_uptrend": f["sma50"] > f["sma200"] and f["close"] > f["sma50"],
            "deep_dip": stretch >= s["dip_atr_frac"],
            "below_vwap": last < sess["vwap"],
            "volume_pace": sess["volume_pace"] >= s["min_volume_pace"],
            "not_etf": not f.get("is_etf", False),  # single names revert; ETFs trend
        }
        if not all(checks.values()):
            continue
        stop = last - s["stop_atr_mult"] * f["atr14"]
        if stop >= last:
            continue
        rps = last - stop
        out.append({
            "action": "buy", "symbol": sym,
            "entry_limit": round(last * 1.001, 2),
            "stop": round(stop, 2),
            "target": round(last + s["target_r_mult"] * rps, 2),
            "score": stretch * min(sess["volume_pace"], 3.0),
            "reasoning": (
                f"Dip-buy: daily uptrend (50>200 SMA), down "
                f"{sess['ret_since_open']*100:.2f}% since open = {stretch:.2f}x its "
                f"daily ATR ({atr_pct*100:.2f}%), below VWAP {sess['vwap']:.2f} on "
                f"{sess['volume_pace']:.2f}x volume. Betting on the bounce; "
                f"stop {s['stop_atr_mult']}xATR below, flat by close."
            ),
            "checks": checks,
        })
    return out
