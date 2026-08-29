"""Stage 2 — GLIDER analyzer (daily trend-pullback).

Input: scan.json + config + current open positions (symbol, entry, stop, age, order ids)
passed as context so it can emit exit/management intents. Entry logic itself uses only
the scan. Deterministic; sizing in validator.

Exit modes (strategy.exit_mode):
  target — fixed take-profit at target_r_mult × initial risk (original behaviour)
  trail  — chandelier: stop ratchets up to close − trail_atr_mult × ATR14, never down;
           the bracket's take-profit leg is parked far away (trail_target_r_mult × R)
           so winners are cut by the trailing stop, not a fixed cap.
Both modes keep the +breakeven_at_r breakeven ratchet and the time stop.
"""
from __future__ import annotations


def analyze(scan: dict, cfg: dict, open_positions: list[dict]) -> dict:
    s = cfg["strategy"]
    exit_mode = s.get("exit_mode", "target")
    trail_mult = float(s.get("trail_atr_mult", 3.0))
    intents: list[dict] = []
    notes: list[str] = []
    held = {p["symbol"] for p in open_positions}

    # ---- regime gate ----
    bench = scan["symbols"].get(scan["benchmark"])
    regime_ok = bool(bench and bench["close"] > bench["sma200"])
    if not regime_ok:
        notes.append(f"Regime gate CLOSED: {scan['benchmark']} below 200SMA — no new entries.")

    # ---- manage existing positions ----
    for p in open_positions:
        f = scan["symbols"].get(p["symbol"])
        if f is None:
            continue
        r = p["entry"] - p["stop"]
        # time stop
        if p["age_days"] >= s["max_hold_days"]:
            intents.append({"action": "close", "symbol": p["symbol"],
                            "reasoning": f"Time stop: held {p['age_days']}d ≥ {s['max_hold_days']}d."})
            continue
        new_stop = p["stop"]
        why: list[str] = []
        # breakeven ratchet
        if r > 0 and f["close"] >= p["entry"] + s["breakeven_at_r"] * r and new_stop < p["entry"]:
            new_stop = p["entry"]
            why.append(f"+{s['breakeven_at_r']}R reached (close {f['close']:.2f}); stop → breakeven")
        # chandelier trail
        if exit_mode == "trail":
            trail = f["close"] - trail_mult * f["atr14"]
            if trail > new_stop:
                new_stop = trail
                why.append(f"trail {trail_mult}xATR={f['atr14']:.2f} below close {f['close']:.2f}")
        if new_stop > p["stop"] + 0.005:
            intents.append({"action": "raise_stop", "symbol": p["symbol"],
                            "new_stop": round(new_stop, 2),
                            "reasoning": "; ".join(why) + "."})

    # ---- new entries ----
    candidates = []
    if regime_ok:
        for sym, f in scan["symbols"].items():
            if sym in held or f["close"] < s["min_price"]:
                continue
            if f["avg_dollar_vol_20d"] < s["min_avg_dollar_vol"]:
                continue
            checks = {
                "uptrend": f["sma50"] > f["sma200"] and f["close"] > f["sma200"],
                "near_high": f["pct_below_52wk_high"] <= s["max_pct_below_52wk_high"],
                "pullback": (f["rsi2"] < s["pullback_rsi2_max"]) or
                            (f["low_today"] <= f["ema20"] and f["close"] > f["ema20"]),
            }
            if all(checks.values()):
                stop = f["close"] - s["stop_atr_mult"] * f["atr14"]
                if stop >= f["close"]:
                    continue
                rps = f["close"] - stop
                tgt_mult = (s.get("trail_target_r_mult", 8.0) if exit_mode == "trail"
                            else s["target_r_mult"])
                # prefer stronger trends: distance above 200sma, tempered by pullback depth
                trend_strength = (f["close"] - f["sma200"]) / f["sma200"]
                candidates.append({
                    "action": "buy",
                    "symbol": sym,
                    "entry_limit": round(f["close"] * 1.002, 2),
                    "stop": round(stop, 2),
                    "target": round(f["close"] + tgt_mult * rps, 2),
                    "score": trend_strength,
                    "reasoning": (
                        f"Uptrend (50>200 SMA, {f['pct_below_52wk_high']:.1f}% off 52wk high), "
                        f"pullback trigger RSI2={f['rsi2']:.1f} / EMA20 touch. "
                        f"Stop {s['stop_atr_mult']}xATR={f['atr14']:.2f} below close {f['close']:.2f}; "
                        f"exit_mode={exit_mode}."
                    ),
                    "checks": checks,
                })
        candidates.sort(key=lambda c: c["score"], reverse=True)
        slots = max(0, cfg["risk"]["max_positions"] - len(held))
        entries = candidates[:slots]
        intents.extend(entries)
        notes.append(f"{len(candidates)} setups; {slots} slots free; proposing {len(entries)}")

    return {"intents": intents, "notes": notes, "regime_ok": regime_ok,
            "candidates_considered": len(candidates)}
