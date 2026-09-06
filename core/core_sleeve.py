"""Core sleeve — GLIDER's idle cash lives in the benchmark ETF (core-satellite).

Why: the pullback overlay is ~40-45% invested on average. The other half sat in
cash, which is the whole gap to SPY (10y, 200SMA gate: overlay alone +105% vs
SPY +264%; overlay + sleeve +323%). Standard core-satellite: hold SPY with every
idle dollar, sell SPY to fund each overlay entry, sweep cash back after exits.

Two parts:
  plan()    — pure, deterministic, unit-tested. Decides buy/sell/hold and the
              dollar amount from account + positions + the validator's approvals.
  execute() — places ONE notional market order (fractional) and waits for the
              fill. Sells run BEFORE the bracket buys so no margin is ever used;
              buys run AFTER them so the sweep never eats cash the buys need.

The sleeve position is invisible to the rest of the pipeline: run_cycle strips it
from the position list before reconcile/validate, the analyzer never proposes the
core symbol as a setup, and reflection ignores its fills.
"""
from __future__ import annotations

import time


def settings(cfg: dict) -> dict:
    return cfg.get("core_sleeve") or {}


def enabled(cfg: dict) -> bool:
    return bool(settings(cfg).get("enabled", False))


def symbol(cfg: dict) -> str:
    return settings(cfg).get("symbol", cfg.get("universe", {}).get("benchmark", "SPY"))


def split_positions(positions: list[dict], cfg: dict) -> tuple[dict | None, list[dict]]:
    """(core position or None, every other position)."""
    if not enabled(cfg):
        return None, list(positions)
    sym = symbol(cfg)
    core = next((p for p in positions if p["symbol"] == sym), None)
    return core, [p for p in positions if p["symbol"] != sym]


def plan(account: dict, core_pos: dict | None, other_positions: list[dict],
         open_orders: list[dict], approved: list[dict], cfg: dict,
         regime_ok: bool = True, kill: bool = False) -> dict:
    """Target core = equity − overlay positions − pending buys − approved buys − buffer.

    mode 'always': hold the target every day.  mode 'gated': target 0 while the
    regime gate is closed.  Kill switch tripped → target 0 (the bot goes flat).
    """
    c = settings(cfg)
    sym = symbol(cfg)
    equity = float(account["equity"])
    current = float(core_pos["market_value"]) if core_pos else 0.0
    overlay_mv = sum(abs(float(p["market_value"])) for p in other_positions)
    pending = sum(float(o.get("limit_price") or 0) * float(o.get("qty") or 0)
                  for o in open_orders
                  if "buy" in str(o.get("side", "")).lower() and o.get("symbol") != sym)
    new_buys = sum(float(o.get("notional") or 0) for o in approved if o.get("action") == "buy")
    buffer = equity * float(c.get("cash_buffer_pct", 1.0)) / 100
    mode = c.get("mode", "always")
    want = enabled(cfg) and not kill and (mode == "always" or (mode == "gated" and regime_ok))
    target = max(0.0, equity - overlay_mv - pending - new_buys - buffer) if want else 0.0
    delta = target - current
    min_order = float(c.get("min_order_dollars", 25))
    if abs(delta) < min_order:
        action, notional = "hold", 0.0
    elif delta < 0:
        action, notional = "sell", min(-delta, current)
    else:
        action, notional = "buy", delta
    return {"symbol": sym, "mode": mode, "want": want, "kill": kill, "regime_ok": regime_ok,
            "equity": round(equity, 2), "current": round(current, 2), "target": round(target, 2),
            "overlay_mv": round(overlay_mv, 2), "pending_buys": round(pending, 2),
            "approved_buys": round(new_buys, 2), "buffer": round(buffer, 2),
            "action": action, "notional": round(notional, 2)}


def execute(p: dict, broker, wait_s: float = 25.0, poll_s: float = 1.0) -> dict:
    """Place the planned order and wait for the fill. Never raises."""
    try:
        if p["action"] == "sell" and p["current"] > 0 and p["notional"] >= p["current"] * 0.995:
            broker.close_position(p["symbol"])  # whole position: avoids fractional dust
            return {"action": "sell", "symbol": p["symbol"], "notional": p["current"],
                    "via": "close_position", "filled": True, "ok": True}
        ack = broker.submit_notional_market(p["symbol"], p["notional"], p["action"])
        deadline = time.time() + wait_s
        status = ack
        while time.time() < deadline:
            status = broker.order_status(ack["id"])
            st = status["status"].lower()
            if ("filled" in st and "partial" not in st) or \
                    any(k in st for k in ("rejected", "canceled", "expired")):
                break
            time.sleep(poll_s)
        st = status["status"].lower()
        filled = "filled" in st and "partial" not in st
        return {**ack, "final": status, "filled": filled, "ok": filled}
    except Exception as e:  # journal it; the cycle must not die on the sleeve
        return {"action": p["action"], "symbol": p["symbol"], "notional": p["notional"],
                "filled": False, "ok": False, "error": f"{type(e).__name__}: {e}"}
