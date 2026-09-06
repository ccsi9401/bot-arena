#!/usr/bin/env python3
"""Run one full pipeline cycle for a bot: scan → analyze → validate → execute.

Usage:  python run_cycle.py --bot scalpel|glider [--dry-run]

Every stage's artifact is journaled; commit the repo afterwards and the run is
reproducible. --dry-run does everything except place orders.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime

from core.common import Journal, State, load_config, now_et
from core.broker import make_broker
from core.data import MarketData
from core import scanner, validator, executor as executor_mod, core_sleeve


def reconcile_ledger(state: State, positions: list[dict], journal: Journal) -> list[dict]:
    """State ledger vs broker truth. Broker wins; drift is journaled loudly."""
    ledger = state.read("ledger", {})
    held = {p["symbol"]: p for p in positions}
    drift = []
    for sym in list(ledger):
        if sym not in held:
            drift.append(f"ledger had {sym} but broker shows flat — removed")
            del ledger[sym]
    for sym, p in held.items():
        if sym not in ledger:
            drift.append(f"broker holds {sym} unknown to ledger — adopted at avg entry")
            ledger[sym] = {"entry": p["avg_entry"], "stop": None,
                           "opened": f"{now_et():%Y-%m-%d}"}
    state.write("ledger", ledger)
    if drift:
        journal.write("reconcile_drift", {"drift": drift})
    out = []
    for sym, rec in ledger.items():
        age = (date.fromisoformat(f"{now_et():%Y-%m-%d}") -
               date.fromisoformat(rec["opened"])).days
        out.append({"symbol": sym, "entry": rec["entry"],
                    "stop": rec["stop"] if rec["stop"] is not None else rec["entry"] * 0.9,
                    "age_days": age})
    return out


def update_ledger_after_execution(state: State, execution: dict, validation: dict) -> None:
    ledger = state.read("ledger", {})
    approved_by_symbol = {o.get("symbol"): o for o in validation["approved"]}
    for res in execution["results"]:
        if not res.get("ok"):
            continue
        if res["action"] == "buy":
            o = approved_by_symbol.get(res["symbol"], {})
            ledger[res["symbol"]] = {"entry": o.get("entry_limit"),
                                     "stop": o.get("stop"),
                                     "opened": f"{now_et():%Y-%m-%d}"}
        elif res["action"] == "close":
            ledger.pop(res.get("symbol"), None)
        elif res["action"] == "liquidate_all":
            ledger.clear()
        elif res["action"] == "raise_stop":
            sym = res.get("symbol")
            o = next((a for a in validation["approved"]
                      if a["action"] == "raise_stop" and a["symbol"] == sym), None)
            if sym in ledger and o:
                ledger[sym]["stop"] = o["new_stop"]
    state.write("ledger", ledger)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True, choices=["scalpel", "glider"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.bot)
    journal = Journal.start(args.bot, cfg)
    state = State(args.bot)

    try:
        data = MarketData()
        broker = make_broker(cfg)
        market_open = data.market_open()
        if not market_open:
            journal.write("skipped", {"reason": "market closed (holiday/half-day)"})
            print(json.dumps({"run_id": journal.run_id, "skipped": "market closed"}))
            return 0
        account = broker.account()
        # the core sleeve (idle cash parked in SPY) is not a strategy position:
        # strip it before reconcile/validate, expose its value as spendable cash
        core_pos, positions = core_sleeve.split_positions(broker.positions(), cfg)
        account["core_value"] = float(core_pos["market_value"]) if core_pos else 0.0
        open_orders = broker.open_orders()
        open_ctx = reconcile_ledger(state, positions, journal)

        # ---- 1. scan ----
        mode = "intraday" if args.bot == "scalpel" else "daily"
        scan = scanner.scan(data, cfg, mode)
        journal.write("scan", scan)

        # ---- 2. analyze ----
        if args.bot == "scalpel":
            from bots.intraday.analyzer import analyze
            analysis = analyze(scan, cfg, f"{now_et():%H:%M}")
        else:
            from bots.swing.analyzer import analyze
            analysis = analyze(scan, cfg, open_ctx)
        journal.write("analysis", analysis)

        # ---- 3. validate ----
        syms = [i["symbol"] for i in analysis["intents"] if i.get("symbol")]
        last_trades = data.last_trades(syms) if syms else {}
        validation = validator.validate(
            analysis["intents"], account, positions, open_orders, cfg, state,
            scan["asof_et"], market_open, last_trades)

        # ---- 3b. core sleeve: sell SPY BEFORE entries (never margin), buy AFTER ----
        core_log = None
        if core_sleeve.enabled(cfg):
            kill = state.kill_switch_tripped() or any("kill" in h.lower() for h in validation["halts"])
            core_plan = core_sleeve.plan(account, core_pos, positions, open_orders,
                                         validation["approved"], cfg,
                                         regime_ok=bool(analysis.get("regime_ok", True)), kill=kill)
            core_log = {"plan": core_plan, "orders": [], "dry_run": args.dry_run}
            if core_plan["action"] == "sell" and not args.dry_run:
                res = core_sleeve.execute(core_plan, broker)
                core_log["orders"].append(res)
                if not res.get("filled"):
                    dropped = [o for o in validation["approved"] if o["action"] == "buy"]
                    validation["approved"] = [o for o in validation["approved"] if o["action"] != "buy"]
                    validation["rejected"] += [{**o, "reject_reason": "core sleeve sell did not fill - entry skipped rather than borrow"}
                                               for o in dropped]
                    core_log["note"] = f"sell unfilled - {len(dropped)} entries skipped"
        journal.write("validation", validation)

        # ---- 4. execute ----
        if args.dry_run:
            execution = {"results": [], "placed": 0, "failed": 0, "dry_run": True}
        else:
            tif = "gtc" if args.bot == "glider" else "day"
            execution = executor_mod.execute(validation["approved"], broker, tif=tif)
            update_ledger_after_execution(state, execution, validation)
            if core_log and core_log["plan"]["action"] == "buy":
                core_log["orders"].append(core_sleeve.execute(core_log["plan"], broker))
        journal.write("execution", execution)
        if core_log:
            journal.write("core", core_log)

        state.append_equity_point(account["equity"], account["cash"],
                                  note=f"cycle {journal.run_id}")
        print(json.dumps({
            "run_id": journal.run_id,
            "equity": account["equity"],
            "intents": len(analysis["intents"]),
            "approved": len(validation["approved"]),
            "rejected": len(validation["rejected"]),
            "halts": validation["halts"],
            "placed": execution["placed"],
            "failed": execution["failed"],
        }, indent=2))
        return 0
    except Exception:
        err = traceback.format_exc()
        journal.write("error", {"traceback": err})
        print(err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
