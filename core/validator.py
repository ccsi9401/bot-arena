"""Stage 3 — Validator. The only stage allowed to say no.

Takes analyzer intents + live account state, applies every risk gate, sizes the
approved buys, and emits an order list. Every rejection carries its reason.
"""
from __future__ import annotations

from datetime import datetime

from .common import now_et, State


def validate(intents: list[dict], account: dict, positions: list[dict],
             open_orders: list[dict], cfg: dict, state: State,
             scan_asof_iso: str, market_open: bool,
             last_trades: dict[str, float]) -> dict:
    r = cfg["risk"]
    approved: list[dict] = []
    rejected: list[dict] = []
    equity = account["equity"]
    start_eq = cfg["starting_equity"]
    held = {p["symbol"] for p in positions}
    pending_buy = {o["symbol"] for o in open_orders if "buy" in o["side"].lower()}
    gross = sum(abs(p["market_value"]) for p in positions)

    def reject(intent, reason):
        rejected.append({**intent, "reject_reason": reason})

    # ---------- account-level gates (evaluated once) ----------
    halts: list[str] = []

    dd_pct = (start_eq - equity) / start_eq * 100
    if state.kill_switch_tripped():
        halts.append("kill switch previously tripped")
    elif dd_pct >= r["kill_switch_drawdown_pct"]:
        state.trip_kill_switch(f"drawdown {dd_pct:.2f}% ≥ {r['kill_switch_drawdown_pct']}%")
        halts.append(f"KILL SWITCH TRIPPED: drawdown {dd_pct:.2f}%")

    daily_pl_pct = (equity - account["last_equity"]) / account["last_equity"] * 100 \
        if account["last_equity"] else 0.0
    daily_breaker = False
    if r.get("daily_loss_limit_pct") is not None:
        if state.daily_halt_active():
            halts.append("daily circuit breaker active")
            daily_breaker = True
        elif daily_pl_pct <= -r["daily_loss_limit_pct"]:
            state.trip_daily_halt(f"day P&L {daily_pl_pct:.2f}%")
            halts.append(f"DAILY BREAKER TRIPPED: day P&L {daily_pl_pct:.2f}%")
            daily_breaker = True

    try:
        asof = datetime.fromisoformat(scan_asof_iso)
        stale_min = (now_et() - asof).total_seconds() / 60
    except Exception:
        stale_min = 9e9
    data_fresh = stale_min <= r["data_freshness_minutes"]
    if not data_fresh:
        halts.append(f"scan stale ({stale_min:.0f} min) — no new entries")

    kill = any("kill" in h.lower() for h in halts)

    # ---------- per-intent gates ----------
    n_open = len(held) + len(pending_buy)
    for intent in intents:
        action = intent["action"]

        # management / exits are allowed even under halts (they reduce risk)
        if action == "liquidate_all":
            approved.append(intent)
            continue
        if action == "close":
            if intent["symbol"] in held:
                approved.append(intent)
            else:
                reject(intent, "no such open position")
            continue
        if action == "raise_stop":
            approved.append(intent)  # executor resolves the child order id
            continue

        if action != "buy":
            reject(intent, f"unknown action {action!r}")
            continue

        # ---- new-entry gates ----
        if kill or (daily_breaker and daily_pl_pct is not None):
            reject(intent, f"entries halted: {'; '.join(halts)}")
            continue
        if not market_open:
            reject(intent, "market closed")
            continue
        if not data_fresh:
            reject(intent, f"scan data stale ({stale_min:.0f} min)")
            continue
        sym = intent["symbol"]
        if sym in held or sym in pending_buy:
            reject(intent, "duplicate: already held or pending order")
            continue
        if n_open >= r["max_positions"]:
            reject(intent, f"concurrency cap {r['max_positions']} reached")
            continue

        # price sanity vs live tape
        last = last_trades.get(sym)
        if last is None:
            reject(intent, "no live trade print for sanity check")
            continue
        tol = r["limit_price_tolerance_pct"] / 100
        if abs(intent["entry_limit"] - last) / last > tol:
            reject(intent, f"limit {intent['entry_limit']} drifted >"
                           f"{r['limit_price_tolerance_pct']}% from last {last}")
            continue
        if not (intent["stop"] < intent["entry_limit"] < intent["target"]):
            reject(intent, "stop/entry/target ordering invalid")
            continue

        # sizing: 1% equity risk / (entry - stop)
        risk_dollars = equity * r["risk_per_trade_pct"] / 100
        rps = intent["entry_limit"] - intent["stop"]
        qty = int(risk_dollars / rps)
        if qty < 1:
            reject(intent, f"qty rounds to 0 (risk ${risk_dollars:.0f}, {rps:.2f}/share)")
            continue
        notional = qty * intent["entry_limit"]
        # exposure + cash caps
        max_gross = equity * r["max_gross_exposure_pct"] / 100
        if gross + notional > max_gross:
            qty = int((max_gross - gross) / intent["entry_limit"])
        if notional > account["cash"]:
            qty = min(qty, int(account["cash"] / intent["entry_limit"]))
        if qty < 1:
            reject(intent, f"exposure/cash cap: gross {gross:.0f}, cash {account['cash']:.0f}")
            continue

        notional = qty * intent["entry_limit"]
        gross += notional
        n_open += 1
        approved.append({**intent, "qty": qty, "notional": round(notional, 2),
                         "risk_dollars": round(qty * rps, 2)})

    return {
        "approved": approved,
        "rejected": rejected,
        "halts": halts,
        "account_snapshot": {**account, "gross_after": round(gross, 2),
                             "drawdown_pct": round(dd_pct, 2),
                             "day_pl_pct": round(daily_pl_pct, 2)},
    }
