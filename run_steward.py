#!/usr/bin/env python3
"""STEWARD orchestrator — Claude's portfolio manager on the 3rd paper account.

Usage:
  python run_steward.py --mode cycle   # full scan→analyze→plan→execute (Fridays)
  python run_steward.py --mode pulse   # snapshot equity + benchmark, refresh report
  add --dry-run to plan without placing orders

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
    ap.add_argument("--mode", required=True, choices=["cycle", "pulse"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    state = State("steward")
    try:
        data = MarketData()
        broker = AlpacaBroker(cfg["account_env_prefix"])

        if args.mode == "pulse":
            acct = snapshot(state, broker, data, cfg, "nightly pulse")
            import steward_report
            steward_report.main()
            print(json.dumps({"mode": "pulse", "equity": acct["equity"]}))
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

        snapshot(state, broker, data, cfg, f"cycle {journal.run_id}")
        import steward_report
        steward_report.main()
        print(json.dumps({"run_id": journal.run_id, "equity": acct["equity"],
                          "regime_on": analysis["regime_on"],
                          "orders": len(p["orders"]), "placed": execution["placed"],
                          "failed": execution["failed"],
                          "projected_cash_weight": p.get("projected_cash_weight"),
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
