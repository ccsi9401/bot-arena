#!/usr/bin/env python3
"""STEWARD orchestrator — Claude's portfolio manager on the 3rd paper account.

Usage:
  python run_steward.py --mode auto     # cycle if this week's rebalance is still due
                                        # and the market is open, else pulse
  python run_steward.py --mode catchup  # cycle ONLY if a past-due rebalance was missed,
                                        # otherwise exit without touching anything
  python run_steward.py --mode cycle    # force the full scan→analyze→plan→execute
  python run_steward.py --mode pulse    # snapshot equity + benchmark, refresh report
  add --dry-run to plan without placing orders

Scheduling (rewritten 2026-08-29). The workflow used to decide cycle-vs-pulse from
the UTC clock: Friday, hour 19 or 20. Hour 20 UTC is 16:00 ET — after the close — so
market_open() rejected it, leaving a real window of 59 minutes once a week. GitHub's
cron drifts 20-40 minutes routinely and drifted 3.5 and 8 hours in the week of Aug 24,
so runs landed outside the window, silently downgraded themselves to a pulse, and
committed green. Two of roughly five scheduled cycles ever fired, and the $5,000
restart sat in 100% cash for five days with nothing anywhere saying so.

The clock is no longer the authority. State records the date of the last completed
cycle and the bot asks the only question that matters — has this week's rebalance
happened yet? A drifted run still works, a missed Friday is picked up by the next
weekday-morning catchup, and the pulse reports an overdue cycle instead of hiding it.

Self-contained: shares only read-only imports with the traders (data layer,
broker adapter, chart helper). SCALPEL's frozen code path is untouched.

Launch gate: refuses to trade until state/steward/gate.json exists with
{"passed": true} — written by the steward-backtest workflow on a green run.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from core.common import ROOT, Journal, State, now_et
from core.broker import AlpacaBroker
from core.data import MarketData
from bots.portfolio import scan as pscan
from bots.portfolio.analyzer import analyze
from bots.portfolio.planner import plan
import yaml


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "steward.yaml").read_text(encoding="utf-8"))


def gate_passed(state: State) -> bool:
    return bool(state.read("gate", {}).get("passed", False))


# ---------------------------------------------------------------------------
# Cycle scheduling — date-based, deliberately not clock-based.
# ---------------------------------------------------------------------------

def week_anchor(cfg: dict, now: datetime | None = None) -> datetime:
    """Start of the current rebalance week: the most recent cycle weekday, 00:00 ET.

    With cycle_weekday=4 (Friday): on a Friday this is today; on the following
    Monday it is still that same Friday. So one anchor covers Friday through
    Thursday, and 'has a cycle run since the anchor?' is a stable question no
    matter which day or hour the runner happens to wake up on.
    """
    now = now or now_et()
    target = int(cfg["strategy"].get("cycle_weekday", 4))
    days_since = (now.weekday() - target) % 7
    return (now - timedelta(days=days_since)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def cycle_status(state: State, cfg: dict, now: datetime | None = None) -> dict:
    """Everything the runner, the report and the health check need to know.

    due      — no cycle has completed since this week's anchor.
    overdue  — due AND the anchor day itself has passed, i.e. the scheduled slot
               came and went without a rebalance. On the anchor day this is False:
               the cycle is due but its slot has not arrived yet, which is normal
               and must not read as a fault.
    """
    now = now or now_et()
    anchor = week_anchor(cfg, now)
    last = state.read("last_cycle", {})
    last_date = last.get("date_et")
    days_since_anchor = (now.date() - anchor.date()).days
    due = (not last_date) or last_date < f"{anchor:%Y-%m-%d}"
    return {
        "due": due,
        "overdue": bool(due and days_since_anchor >= 1),
        "week_anchor_et": f"{anchor:%Y-%m-%d}",
        "days_since_anchor": days_since_anchor,
        "last_cycle_date_et": last_date,
        "last_cycle_run_id": last.get("run_id"),
        "checked_et": now.isoformat(),
    }


def record_cycle(state: State, run_id: str) -> None:
    """Mark this week's rebalance done. Written only on a cycle that actually
    reached the market — a gate skip or a closed market leaves the week still due,
    so the next catchup retries instead of silently swallowing the week."""
    state.write("last_cycle", {"date_et": f"{now_et():%Y-%m-%d}",
                               "run_id": run_id, "ts_et": now_et().isoformat()})


def resolve_mode(requested: str, state: State, cfg: dict, data: MarketData) -> tuple[str, dict]:
    """Turn auto/catchup into a concrete cycle-or-pulse decision.

    auto     — the Friday slot and manual dispatch: rebalance if one is due and the
               market is open, otherwise just take the pulse.
    catchup  — the weekday-morning safety net: acts ONLY on an overdue cycle, so a
               Friday-morning run never pre-empts the Friday-afternoon slot, and a
               week that is already done costs one no-op run.
    """
    status = cycle_status(state, cfg)
    if requested in ("cycle", "pulse"):
        return requested, status
    want = status["overdue"] if requested == "catchup" else status["due"]
    if want and data.market_open():
        return "cycle", status
    if requested == "catchup":
        return "noop", status
    return "pulse", status


def ensure_benchmark(state: State, data: MarketData, cfg: dict) -> dict:
    bm = state.read("benchmark", {})
    if not bm:
        px = data.last_trades([cfg["benchmark"]]).get(cfg["benchmark"])
        if px:
            bm = {"start_date": f"{now_et():%Y-%m-%d}", "spy_start_price": px,
                  "start_equity": cfg["starting_equity"]}
            state.write("benchmark", bm)
    return bm


def benchmark_value(bm: dict, data: MarketData, cfg: dict):
    if not bm:
        return None
    px = data.last_trades([cfg["benchmark"]]).get(cfg["benchmark"])
    if not px:
        return None
    return bm["start_equity"] * px / bm["spy_start_price"]


def snapshot(state: State, broker: AlpacaBroker, data: MarketData, cfg: dict,
             note: str) -> dict:
    acct = broker.account()
    state.append_equity_point(acct["equity"], acct["cash"], note=note)
    bm = ensure_benchmark(state, data, cfg)
    bv = benchmark_value(bm, data, cfg)
    if bv:
        curve = state.read("benchmark_curve", [])
        curve.append({"ts_et": now_et().isoformat(), "equity": round(bv, 2),
                      "cash": 0, "note": "spy shadow"})
        state.write("benchmark_curve", curve)
    return acct


def execute(orders: list[dict], broker: AlpacaBroker) -> dict:
    """Place the plan. Notional (dollar) orders where the planner asked for them,
    share quantities otherwise — notional wins so fills land exactly on target
    regardless of price drift between planning and execution."""
    broker.cancel_all_orders()
    results = []
    for o in orders:
        notional = o.get("notional")
        sizing = ({"notional": round(float(notional), 2)} if notional
                  else {"qty": o["qty"]})
        try:
            ack = broker.trading.submit_order(MarketOrderRequest(
                symbol=o["symbol"],
                side=OrderSide.BUY if o["side"] == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY, **sizing))
            results.append({"symbol": o["symbol"], "side": o["side"],
                            "qty": o.get("qty"), "notional": notional,
                            "id": str(ack.id), "status": str(ack.status), "ok": True})
        except Exception as e:
            results.append({"symbol": o["symbol"], "side": o["side"],
                            "qty": o.get("qty"), "notional": notional,
                            "ok": False, "error": f"{type(e).__name__}: {e}"})
    return {"results": results,
            "placed": sum(1 for x in results if x.get("ok")),
            "failed": sum(1 for x in results if not x.get("ok"))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "catchup", "cycle", "pulse"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    state = State("steward")
    try:
        data = MarketData()
        broker = AlpacaBroker(cfg["account_env_prefix"])

        mode, status = resolve_mode(args.mode, state, cfg, data)
        # Persist it every run, whatever the outcome: this is what makes a missed
        # cycle visible to the report and the weekly health check instead of being
        # something you can only infer from an absent journal folder.
        state.write("cycle_status", {**status, "requested_mode": args.mode,
                                     "resolved_mode": mode})

        if mode == "noop":
            print(json.dumps({"mode": "catchup", "action": "noop",
                              "reason": ("no overdue cycle" if not status["overdue"]
                                         else "market closed — will retry"),
                              **status}, indent=2))
            return 0

        if mode == "pulse":
            acct = snapshot(state, broker, data, cfg, "nightly pulse")
            import steward_report
            steward_report.main()
            out = {"mode": "pulse", "equity": acct["equity"], **status}
            if status["overdue"]:
                print(f"::warning::STEWARD weekly rebalance OVERDUE — none since "
                      f"{status['last_cycle_date_et'] or 'inception'}, week of "
                      f"{status['week_anchor_et']} ({status['days_since_anchor']}d).")
            print(json.dumps(out, indent=2))
            return 0

        # ---- full weekly cycle ----
        journal = Journal.start("steward", cfg)
        if not gate_passed(state):
            journal.write("skipped", {"reason": "backtest gate not passed yet"})
            print(json.dumps({"run_id": journal.run_id,
                              "skipped": "gate not passed — run steward-backtest first"}))
            return 0
        if not data.market_open():
            journal.write("skipped", {"reason": "market closed"})
            print(json.dumps({"run_id": journal.run_id, "skipped": "market closed"}))
            return 0

        scan = pscan.scan(data, cfg)
        journal.write("scan", scan)

        analysis = analyze(scan, cfg)
        journal.write("analysis", analysis)

        acct = broker.account()
        positions = broker.positions()
        syms = sorted(set(list(analysis["targets"]) + [p["symbol"] for p in positions]))
        prices = data.last_trades(syms)
        fractionable = broker.fractionable(syms)
        kill = state.kill_switch_tripped()
        # High-water mark from the recorded curve, so the peak-drawdown gate has a
        # reference that grows with the book rather than being pinned to inception.
        curve = state.read("equity_curve", [])
        peak_equity = max([float(pt.get("equity") or 0) for pt in curve]
                          + [float(acct["equity"])])
        p = plan(analysis["targets"], analysis, acct, positions, prices, cfg, kill,
                 fractionable=fractionable, peak_equity=peak_equity)
        if any("KILL" in h for h in p["halts"]) and not kill:
            state.trip_kill_switch(p["halts"][0])
        journal.write("plan", p)

        if args.dry_run:
            execution = {"results": [], "placed": 0, "failed": 0, "dry_run": True}
        else:
            execution = execute(p["orders"], broker)
        journal.write("execution", execution)

        # The week is done once the plan has reached the market. A dry run is a
        # rehearsal, not a rebalance, so it deliberately leaves the week open.
        if not args.dry_run:
            record_cycle(state, journal.run_id)
            state.write("cycle_status", {**cycle_status(state, cfg),
                                         "requested_mode": args.mode,
                                         "resolved_mode": mode})

        snapshot(state, broker, data, cfg, f"cycle {journal.run_id}")
        import steward_report
        steward_report.main()
        print(json.dumps({"run_id": journal.run_id, "equity": acct["equity"],
                          "regime_on": analysis["regime_on"],
                          "index_residue_pp": analysis.get("index_residue_pp"),
                          "orders": len(p["orders"]), "placed": execution["placed"],
                          "failed": execution["failed"],
                          "cash_target": p.get("cash_target"),
                          "projected_cash_weight": p.get("projected_cash_weight"),
                          "cash_drag_sweep": p.get("cash_drag_sweep"),
                          "halts": p["halts"]}, indent=2))
        return 0
    except Exception:
        err = traceback.format_exc()
        (ROOT / "journal").mkdir(exist_ok=True)
        (ROOT / "journal" / f"steward_error_{now_et():%Y%m%d_%H%M}.json").write_text(
            json.dumps({"traceback": err}), encoding="utf-8")
        print(err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
