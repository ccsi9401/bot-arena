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
from core import scanner, validator, executor as executor_mod


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
        positions = broker.positions()
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
        journal.write("validation", validation)

        # ---- 4. execute ----
        if args.dry_run:
            execution = {"results": [], "placed": 0, "failed": 0, "dry_run": True}
        else:
            tif = "gtc" if args.bot == "glider" else "day"
            execution = executor_mod.execute(validation["approved"], broker, tif=tif)
            update_ledger_after_execution(state, execution, validation)
        journal.write("execution", execution)

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
