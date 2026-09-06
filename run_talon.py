#!/usr/bin/env python3
"""TALON live cycle — long-only crypto ensemble with a rising floor, Alpaca paper.

Usage:
  python run_talon.py --mode cycle     # trade: exits, then entries; writes state + journal
  python run_talon.py --mode pulse     # read-only: prints what a cycle WOULD do, touches nothing
  python run_talon.py --mode flatten   # close every position at the broker; floor state is kept

Gate: `cycle` refuses to place any order unless state/talon/gate.json exists and says
{"passed": true} — written only by backtest/backtest.py on a green run against real
bars. Exit code 2 without it. `flatten` is a safety action and is NOT gated.

Cycle order (exits before entries, always):
  1. load state, equity, positions
  2. daily bookkeeping — a new UTC day resets the loss-breaker basis and clears the halt
  3. AccountFloor.update -> journal any sweep -> tradeable = equity - locked
  4. kill switches (drawdown from high-water, daily loss breaker); a fresh halt is journaled
  5. fetch bars (completed bars for signals and, by default, for floor exits)
  6. exits: update every PositionFloor on the last completed bar's close and close anything
     breached (rising_floor.position.evaluate_on; the backtest fills on the same convention);
     adopt orphans; a kill-switch halt flattens; a daily-breaker halt follows
     risk.daily_breaker_action
  7. entries via planner.plan(), sized on TRADEABLE equity
  8. save state, journal a snapshot row

Every order attempt is wrapped: a failure on one symbol is journaled and the cycle
continues. Credentials come from TALON_API_KEY / TALON_API_SECRET and are never
written or printed. To release the locked reserve or reset the kill switch's
high-water mark, edit state/talon/talon_state.json by hand — the bot cannot.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from talon import ROOT, load_config  # noqa: E402
from talon.broker import Alpaca, CredentialsError, LiveGuardError  # noqa: E402
from talon.data import align, drop_incomplete_bar, fetch_bars  # noqa: E402
from talon.floor import EVAL_COMPLETED_BAR, EVAL_CURRENT_PRICE, AccountFloor, PositionFloor  # noqa: E402
from talon.planner import HALT_FLATTEN, halt_action, kill_switch_state, plan  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_GATE = 2
STATE_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# journal + state
# ---------------------------------------------------------------------------

class Journal:
    """One JSONL row per event, one file per UTC day. Also echoes to stdout."""

    def __init__(self, cfg: dict, run_id: str, mode: str, enabled: bool = True):
        self.dir = ROOT / cfg["paths"]["journal_dir"]
        self.run_id, self.mode, self.enabled = run_id, mode, enabled

    def write(self, event: str, **payload) -> None:
        row = {"ts": now_iso(), "run_id": self.run_id, "mode": self.mode, "event": event, **payload}
        print(f"[{event}] " + json.dumps(payload, default=str, sort_keys=True))
        if not self.enabled:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{utcnow():%Y-%m-%d}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def state_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["state"]


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def gate_status(cfg: dict) -> tuple[bool, str]:
    p = ROOT / cfg["paths"]["gate"]
    if not p.exists():
        return False, f"{_rel(p)} does not exist — run backtest/backtest.py (without --synthetic) first"
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"{_rel(p)} unreadable: {e}"
    if not g.get("passed", False):
        return False, f"{_rel(p)} says passed=false: {g.get('reason', '')}"
    return True, f"gate passed ({g.get('generated_utc', '?')})"


def new_state(equity: float) -> dict:
    return {
        "version": STATE_VERSION,
        "created_utc": now_iso(),
        "last_run_utc": None,
        "start_equity": equity,
        "equity_high_water": equity,
        "day": {"date": f"{utcnow():%Y-%m-%d}", "start_equity": equity},
        "halted": False,
        "halt_reason": "",
        "account_floor": AccountFloor(basis=equity).to_dict(),
        "positions": {},
        "last_snapshot": {},
    }


def load_state(cfg: dict, equity: float) -> dict:
    p = state_path(cfg)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return new_state(equity)


def save_state(cfg: dict, state: dict) -> None:
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["last_run_utc"] = now_iso()
    p.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_bars(cfg: dict, journal: Journal) -> tuple[dict, dict]:
    """(bars_closed, bars_full): completed bars for signals, full bars for current prices.
    Symbols with fewer than min_bars are dropped BEFORE align() so they cannot trim the panel."""
    dcfg = cfg["data"]
    symbols = list(dict.fromkeys(list(cfg["universe"]["symbols"]) + [cfg["universe"]["benchmark"]]))
    full = fetch_bars(symbols, dcfg["timeframe"], dcfg["lookback_bars"], cfg=cfg)
    closed = full if dcfg.get("use_incomplete_bar", False) else drop_incomplete_bar(full, dcfg["timeframe"])
    short = [s for s, df in closed.items() if len(df) < int(dcfg["min_bars"])]
    if short:
        journal.write("data_short", symbols=short, min_bars=dcfg["min_bars"])
    closed = align({s: df for s, df in closed.items() if s not in short}, mode=dcfg.get("alignment", "intersection"))
    if cfg["universe"]["benchmark"] not in closed:
        raise RuntimeError("benchmark has too little history to run the regime gate")
    return closed, full


def current_price(sym: str, positions: dict, full: dict) -> float | None:
    p = positions.get(sym, {})
    if p.get("current_price", 0) > 0:
        return float(p["current_price"])
    df = full.get(sym)
    if df is not None and len(df):
        return float(df["close"].iloc[-1])
    return None


def exit_price(sym: str, positions: dict, closed: dict, full: dict, cfg: dict) -> float | None:
    """The price the position floor is updated with and checked against.
    completed_bar: the close of the last COMPLETED bar (a close-based stop; the same
    convention the backtest fills on). current_price: the live mark, every cycle."""
    mode = str(cfg["rising_floor"]["position"].get("evaluate_on", EVAL_COMPLETED_BAR))
    if mode == EVAL_CURRENT_PRICE:
        return current_price(sym, positions, full)
    if mode != EVAL_COMPLETED_BAR:
        raise ValueError(f"rising_floor.position.evaluate_on must be {EVAL_COMPLETED_BAR} or {EVAL_CURRENT_PRICE}")
    df = closed.get(sym)
    if df is None or not len(df):
        return None
    v = df["close"].dropna()
    return float(v.iloc[-1]) if len(v) else None


def last_atr(sym: str, closed: dict, cfg: dict) -> float:
    from talon.strategies import atr  # local import keeps module import light
    df = closed.get(sym)
    if df is None or not len(df):
        return 0.0
    v = atr(df, cfg["rising_floor"]["position"]["atr_period"]).iloc[-1]
    return float(v) if v == v else 0.0


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------

def run(mode: str, cfg: dict) -> int:
    run_id = f"{utcnow():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    trade = mode == "cycle"
    journal = Journal(cfg, run_id, mode, enabled=(mode != "pulse"))

    if trade:
        ok, why = gate_status(cfg)
        if not ok:
            print(f"REFUSING TO TRADE: {why}")
            return EXIT_NO_GATE
        print(f"gate: {why}")

    broker = Alpaca(cfg)
    print(f"{cfg['meta']['name']} v{cfg['meta']['version']} mode={mode} endpoint={'PAPER' if broker.is_paper else 'LIVE'} run={run_id}")
    acct = broker.account()
    equity = float(acct["equity"])
    positions = broker.position_map()

    if mode == "flatten":
        state = load_state(cfg, equity)
        for sym in list(positions):
            try:
                res = broker.close_position(sym)
                journal.write("flatten_close", symbol=sym, order_id=res.get("id") if isinstance(res, dict) else None)
            except Exception as e:  # noqa: BLE001
                journal.write("order_failed", symbol=sym, action="flatten", error=str(e))
        journal.write("flatten", closed=list(positions), equity=equity, note="floor state kept")
        save_state(cfg, state)
        return EXIT_OK

    # 1. state -------------------------------------------------------------
    state = load_state(cfg, equity)
    if not state.get("start_equity"):
        state["start_equity"] = equity

    # 2. daily bookkeeping -------------------------------------------------
    today = f"{utcnow():%Y-%m-%d}"
    if state["day"].get("date") != today:
        state["day"] = {"date": today, "start_equity": equity}
        if state.get("halted"):
            journal.write("halt_cleared", reason="new UTC day", previous=state.get("halt_reason", ""))
        state["halted"], state["halt_reason"] = False, ""
        journal.write("new_day", date=today, start_equity=equity)

    # 3. account floor -----------------------------------------------------
    af = AccountFloor.from_dict(state["account_floor"])
    swept = af.update(equity, cfg, now_iso())
    if swept > 0:
        journal.write("sweep", swept=round(swept, 2), locked=round(af.locked, 2), basis=round(af.basis, 2), equity=equity)
    tradeable = af.tradeable(equity)
    state["account_floor"] = af.to_dict()

    # 4. kill switches -----------------------------------------------------
    state["equity_high_water"] = max(float(state.get("equity_high_water", equity)), equity)
    halted, reason = kill_switch_state(equity, state["equity_high_water"], state["day"]["start_equity"], cfg)
    if halted and not state.get("halted"):
        journal.write("halt", reason=reason, equity=equity, high_water=state["equity_high_water"],
                      day_start=state["day"]["start_equity"])
    if halted:
        state["halted"], state["halt_reason"] = True, reason
    halted = bool(state.get("halted"))
    flatten = halted and halt_action(state.get("halt_reason", ""), cfg) == HALT_FLATTEN

    # 5. bars --------------------------------------------------------------
    closed, full = load_bars(cfg, journal)
    bench = closed[cfg["universe"]["benchmark"]]

    # 6. exits before entries ---------------------------------------------
    floors: dict[str, PositionFloor] = {s: PositionFloor.from_dict(d) for s, d in state["positions"].items()}
    for sym in list(floors):
        if sym not in positions:
            journal.write("position_gone", symbol=sym, note="in state but not at broker; floor dropped")
            floors.pop(sym)
    for sym, p in positions.items():
        if sym not in floors:
            entry = p["avg_entry_price"] or current_price(sym, positions, full) or 0.0
            floors[sym] = PositionFloor.open(sym, entry, last_atr(sym, closed, cfg), cfg)
            journal.write("adopt", symbol=sym, entry=entry, floor=floors[sym].floor, r_unit=floors[sym].r_unit)

    exits: list[str] = []
    for sym, fl in floors.items():
        px = exit_price(sym, positions, closed, full, cfg)
        if px is None:
            continue
        before, stage_before = fl.floor, fl.stage
        fl.update(px, cfg)
        if fl.floor > before:
            journal.write("floor_up", symbol=sym, floor=round(fl.floor, 6), was=round(before, 6),
                          stage=fl.stage, r=round(fl.r_multiple(px), 2), high_water=fl.high_water)
        if flatten or fl.breached(px):
            exits.append(sym)
            journal.write("exit_signal", symbol=sym, price=px, mark=current_price(sym, positions, full),
                          floor=fl.floor, stage=fl.stage, r=round(fl.r_multiple(px), 2),
                          reason=("halt: " + state["halt_reason"]) if flatten else "floor breached on completed bar"
                          if cfg["rising_floor"]["position"].get("evaluate_on", EVAL_COMPLETED_BAR) == EVAL_COMPLETED_BAR
                          else "floor breached")

    for sym in exits:
        if not trade:
            continue
        try:
            res = broker.close_position(sym)
            journal.write("exit", symbol=sym, order_id=res.get("id") if isinstance(res, dict) else None)
            floors.pop(sym, None)
            positions.pop(sym, None)
        except Exception as e:  # noqa: BLE001
            journal.write("order_failed", symbol=sym, action="exit", error=str(e))

    # 7. entries -----------------------------------------------------------
    open_map = {s: float(positions[s]["market_value"]) for s in positions if s in floors}
    if halted:
        result = {"regime_on": None, "targets": {}, "candidates": [],
                  "notes": [f"halted ({'flattened' if flatten else 'entries blocked, floors manage exits'}): " + state["halt_reason"]]}
    else:
        result = plan(closed, bench, tradeable, cfg, open_map, i=None, exclude=exits)
    journal.write("plan", regime_on=result["regime_on"], tradeable=round(tradeable, 2),
                  targets={s: t["notional"] for s, t in result["targets"].items()}, notes=result["notes"],
                  candidates=[{"symbol": c["symbol"], "score": round(c["score"], 3),
                               "momentum": (round(c["momentum"], 4) if c["momentum"] == c["momentum"] else None),
                               "reasons": c["reasons"]} for c in result["candidates"]])

    bcfg = cfg["broker"]
    for sym, t in result["targets"].items():
        if not trade:
            continue
        try:
            order = broker.buy_notional(sym, t["notional"])
            fill = broker.fill_price(order.get("id", ""), bcfg["fill_wait_s"], bcfg["fill_polls"]) if order.get("id") else None
            entry = fill or float(t["price"])
            fl = PositionFloor.open(sym, entry, t["atr"], cfg)
            floors[sym] = fl
            journal.write("entry", symbol=sym, notional=t["notional"], entry=entry, fill_confirmed=bool(fill),
                          floor=fl.floor, r_unit=fl.r_unit, score=t["score"], reasons=t["reasons"], order_id=order.get("id"))
        except Exception as e:  # noqa: BLE001
            journal.write("order_failed", symbol=sym, action="entry", notional=t["notional"], error=str(e))

    # 8. save + snapshot ---------------------------------------------------
    snapshot = {
        "equity": equity,
        "locked": round(af.locked, 2),
        "tradeable": round(tradeable, 2),
        "pnl_total": round(equity - float(state["start_equity"]), 2),
        "pnl_day": round(equity - float(state["day"]["start_equity"]), 2),
        "regime_on": result["regime_on"],
        "open_count": len(floors),
        "open": sorted(floors),
        "halted": halted,
        "high_water": state["equity_high_water"],
    }
    state["positions"] = {s: f.to_dict() for s, f in floors.items()}
    state["last_snapshot"] = snapshot
    if mode == "pulse":
        print("pulse: read-only — nothing written. Intended exits:", exits or "none",
              "| intended entries:", {s: t["notional"] for s, t in result["targets"].items()} or "none")
        return EXIT_OK
    save_state(cfg, state)
    journal.write("snapshot", **snapshot)
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["cycle", "pulse", "flatten"], default="pulse")
    ap.add_argument("--config", default=None, help="path to talon.yaml (default config/talon.yaml)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    try:
        return run(args.mode, cfg)
    except CredentialsError as e:
        print(f"credentials error: {e}")
        return EXIT_ERROR
    except LiveGuardError as e:
        print(f"live-money guard: {e}")
        return EXIT_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        if args.mode != "pulse":
            try:
                Journal(cfg, f"{utcnow():%Y%m%d_%H%M%S}_err", args.mode).write("error", error=str(e))
            except Exception:  # noqa: BLE001
                pass
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
