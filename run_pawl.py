#!/usr/bin/env python3
"""
PAWL -- long-only crypto trend bot on Alpaca (paper).

    python run_pawl.py gate       # run the backtest gate; must pass before arming
    python run_pawl.py cycle      # daily: signals, rebalance, set floors
    python run_pawl.py ratchet    # every 4h: move resting floors up, never down
    python run_pawl.py pulse      # read-only status report
    python run_pawl.py flatten    # manual: cancel everything and go to cash

Three clocks, deliberately separated:
  cycle   -> daily, 00:10 UTC, after the daily bar closes
  ratchet -> every 4h; the floor itself rests AT THE BROKER between runs
  pulse   -> whenever you want to look
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from pawl import config as kcfg
from pawl import journal as J
from pawl import risk as R
from pawl import strategy as st
from pawl.broker import ALL_VENUES, BrokerError, capabilities, get_broker
from pawl.data import load_csvs
from pawl.floor import track_without_floor, FloorState, limit_for, open_floor, ratchet as ratchet_floor, breached
from pawl.floor import enabled as floor_enabled

GATE_PATH = "state/pawl_gate.json"


# ---------------------------------------------------------------- helpers
def _universe(cfg, state):
    u = list(cfg["universe"]["core"])
    if state.get("rotation_enabled", True):
        u += list(cfg["universe"]["rotation"])
    return u


def _gate_ok(cfg) -> tuple[bool, str]:
    if not os.path.exists(GATE_PATH):
        return False, "no gate artifact -- run `python run_pawl.py gate` first"
    with open(GATE_PATH, encoding="utf-8") as fh:
        g = json.load(fh)
    if not g.get("passed"):
        failed = [c["check"] for c in g.get("checks", []) if not c["pass"]]
        return False, f"gate did not pass: {failed}"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(g["generated_at"])
    if age > timedelta(days=cfg["gate"]["gate_max_age_days"]):
        return False, f"gate is {age.days} days old, limit is {cfg['gate']['gate_max_age_days']}"
    return True, f"gate passed {age.days}d ago, live arm = {g.get('live_arm')}"


def _connect(cfg):
    b = cfg.get("broker", {})
    venue = b.get("venue", "alpaca")
    kw = {}
    if venue == "sim":
        kw = {"target_venue": b.get("simulate", "alpaca"),
              "start_equity": float(b.get("sim_start_equity", 5000))}
    elif venue.startswith("kraken"):
        kw = {"max_notional_x_equity": float(b.get("max_notional_x_equity", 1.0))}
    else:
        kw = {"paper": cfg["meta"]["mode"] == "paper"}
    api = get_broker(venue, **kw)
    J.log("venue", name=api.caps.name, floor_mode=api.caps.floor_mode(),
          roundtrip_taker=f"{api.caps.roundtrip_taker:.2%}")
    if not api.caps.paper_supported and cfg["meta"]["mode"] == "paper":
        raise BrokerError(
            f"{api.caps.name} has no paper environment. Set broker.venue to 'sim' with "
            f"simulate: {venue} to rehearse against it, or accept that 'paper' here means live money.")
    return api


def _bars(api, symbols, days, completed_only=False):
    """completed_only drops today's still-forming bar so the daily cycle sees
    exactly what the backtest saw: signals on completed days only."""
    bars = api.daily_bars(symbols, days)
    if completed_only:
        today = datetime.now(timezone.utc).date()
        bars = {s: df[[t.date() < today for t in df.index]] for s, df in bars.items()}
    return bars


# ---------------------------------------------------------------- gate
def cmd_gate(cfg, args):
    from pawl import backtest as bt
    # The gate is costed at the CONFIGURED venue's real fee schedule, so
    # switching broker.venue automatically re-runs the economics. A backtest
    # priced at a venue you are not using is a backtest of a different bot.
    venue = cfg.get("broker", {}).get("venue", "alpaca")
    if venue == "sim":
        venue = cfg.get("broker", {}).get("simulate", "alpaca")
    caps = capabilities(venue)
    cfg["costs"]["taker_fee"] = caps.taker_fee
    cfg["costs"]["maker_fee"] = caps.maker_fee
    J.log("gate_costs", venue=caps.name, taker=caps.taker_fee,
          roundtrip=f"{caps.roundtrip_taker:.2%}")

    if args.csv_dir:
        bars = load_csvs(args.csv_dir)
        src = f"CSV folder {args.csv_dir}"
    else:
        api = _connect(cfg)
        bars = _bars(api, list(cfg["universe"]["core"]) + list(cfg["universe"]["rotation"]), args.days,
                     completed_only=True)
        src = f"Alpaca daily bars, {args.days}d"
    if not bars:
        J.log("gate_failed", reason="no bars returned", source=src)
        return 2

    J.log("gate_start", source=src, symbols=len(bars))
    result = bt.gate(bars, cfg)
    bt.write_gate(result, GATE_PATH)

    print("\n" + "=" * 74)
    print(f"  PAWL GATE -- {'PASSED' if result['passed'] else 'FAILED'}   [costed at {caps.name}, "
          f"{caps.roundtrip_taker:.2%} round trip]")
    print("=" * 74)
    for name, m in result["arms"].items():
        if "error" in m:
            print(f"  {name:<12} {m['error']}")
            continue
        print(f"  {name:<12} ret {m['total_return']:>8.1%}   CAGR {m['cagr']:>7.1%}   "
              f"DD {m['max_drawdown']:>7.1%}   Sharpe {m['sharpe']:>5.2f}   trades {m['trades']:>4}")
    print("-" * 74)
    for c in result["checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['check']:<24} {c['detail']}")
    print("-" * 74)
    for c in result["caveats"]:
        print(f"  ! {c}")
    print("=" * 74 + "\n")

    state = J.load_state()
    state["rotation_enabled"] = result["rotation_enabled"]
    state["floor_mode"] = result["floor_mode"]
    J.save_state(state)
    J.log("gate_done", passed=result["passed"], live_arm=result["live_arm"],
          rotation_enabled=result["rotation_enabled"], floor_mode=result["floor_mode"])
    return 0 if result["passed"] else 1


# ---------------------------------------------------------------- cycle
def cmd_cycle(cfg, args):
    ok, why = _gate_ok(cfg)
    if not ok:
        J.log("cycle_blocked", reason=why)
        return 1
    state = J.load_state()
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_cycle_date") == today and not getattr(args, "force", False):
        J.log("cycle_already_done", date=today, note="one daily cycle per UTC day; the retry clock found it done")
        return 0
    J.log("cycle_start", gate=why, mode=cfg["meta"]["mode"])

    api = _connect(cfg)
    equity = api.equity()
    positions = api.positions()

    for note in R.reconcile(state, positions):
        J.log("reconcile", note=note)
        if note.startswith("UNTRACKED"):
            state["manual_halt"] = True
            J.save_state(state)
            J.log("halted", reason="untracked position at broker; refusing to trade around a bug")
            return 1

    bars = _bars(api, _universe(cfg, state), max(400, cfg["regime"]["sma_days"] + 120),
                 completed_only=True)
    verdict = R.evaluate(state, equity, bars, cfg)
    for r in verdict.reasons:
        J.log("risk", note=r)

    if verdict.must_flatten:
        _flatten(api, state, cfg, reason="drawdown kill switch")
        state["manual_halt"] = True
        J.save_state(state)
        return 1
    if verdict.halted:
        J.save_state(state)
        return 1

    held = {s for s, p in positions.items() if abs(float(p.get("qty", 0))) > 0}
    dec = st.decide(bars, cfg, held=held, rotation_enabled=state.get("rotation_enabled", True))
    J.log("decision", regime_on=dec.regime_on, targets=dec.targets, exits=dec.exits)
    for s, why_ in dec.reasons.items():
        J.log("reason", symbol=s, why=why_)

    current_w = {s: float(p["market_value"]) / equity for s, p in positions.items() if equity > 0}
    deltas = st.orders_needed(dec.targets, current_w, equity, cfg)
    for s in dec.exits:
        deltas[s] = -current_w.get(s, 0.0) * equity
    if not verdict.may_enter:
        deltas = {s: d for s, d in deltas.items() if d < 0}      # exits only

    if not deltas:
        J.log("no_action", note="every position is inside its drift band; no round trips spent")
    failures = 0
    # sells first -- frees cash and clears floor orders holding quantity
    for s, usd in sorted(deltas.items(), key=lambda kv: kv[1]):
        try:
            price = float(bars[s]["close"].iloc[-1])
            if usd < 0:
                api.cancel_floor_for(s)          # a resting stop_limit holds the qty
                pos = positions.get(s)
                have = abs(float(pos["qty"])) if pos else 0.0
                q = min(have, abs(usd) / price)
                if q * price < 1.0:
                    continue
                api.market(s, "sell", q)
                J.log("sell", symbol=s, qty=q, approx_usd=round(q * price, 2))
                if q >= have * 0.999:
                    state["floors"].pop(s, None)
                    R.record_roundtrip(state)
            else:
                if usd < cfg["sizing"]["min_notional_usd"]:
                    continue
                q = usd / price
                api.market(s, "buy", q)
                J.log("buy", symbol=s, qty=q, approx_usd=round(usd, 2))
                a = float(st.atr(bars[s]["high"], bars[s]["low"], bars[s]["close"],
                                 cfg["floor"]["atr_days"]).iloc[-1])
                fs = open_floor(s, price, price, a, cfg)
                state["floors"][s] = fs.to_dict()
            state["consecutive_order_failures"] = 0
        except BrokerError as e:
            failures += 1
            state["consecutive_order_failures"] = int(state.get("consecutive_order_failures", 0)) + 1
            J.log("order_failed", symbol=s, error=str(e)[:300])

    _place_floors(api, state, bars, cfg)
    state["equity_at_last_cycle"] = equity
    state["last_cycle_date"] = today
    J.save_state(state)
    J.log("cycle_done", equity=equity, positions=len(state["floors"]), failures=failures)
    return 0


# ---------------------------------------------------------------- ratchet
def cmd_ratchet(cfg, args):
    api = _connect(cfg)
    state = J.load_state()
    if state.get("manual_halt"):
        J.log("ratchet_skipped", reason="manual halt set")
        return 0
    positions = api.positions()
    if not positions:
        J.log("ratchet_idle", note="no open positions")
        return 0
    if not floor_enabled(cfg):
        _place_floors(api, state, {}, cfg)       # clears any stale resting floors
        J.save_state(state)
        J.log("ratchet_idle", note="floor mode none (chosen by gate); exits come from the daily cycle")
        return 0

    bars = _bars(api, list(positions.keys()), 90)
    fresh, stale = R.bars_are_fresh(bars, cfg)
    if not fresh:
        J.log("ratchet_skipped", reason="stale bars: " + "; ".join(stale[:3]))
        return 0

    moved_any = False
    for s in positions:
        raw = state["floors"].get(s)
        df = bars.get(s)
        if df is None or df.empty:
            continue
        close = float(df["close"].iloc[-1])
        a = float(st.atr(df["high"], df["low"], df["close"], cfg["floor"]["atr_days"]).iloc[-1])
        if raw is None:
            # Position with no tracked floor -- adopt it rather than leave it naked.
            fs = open_floor(s, close, close, a, cfg)
            state["floors"][s] = fs.to_dict()
            J.log("floor_adopted", symbol=s, floor=round(fs.floor_price, 2))
            moved_any = True
            continue
        fs = FloorState.from_dict(raw)
        if breached(fs, close):
            api.cancel_floor_for(s)
            q = abs(float(positions[s]["qty"]))
            api.market(s, "sell", q)
            state["floors"].pop(s, None)
            R.record_roundtrip(state)
            J.log("floor_backstop_exit", symbol=s, close=close, floor=round(fs.floor_price, 2),
                  note="resting order did not fire; exited at market")
            moved_any = True
            continue
        fs, moved = ratchet_floor(fs, close, a, cfg)
        state["floors"][s] = fs.to_dict()
        if moved:
            moved_any = True
            J.log("floor_raised", symbol=s, floor=round(fs.floor_price, 2),
                  high_water=round(fs.high_water, 2), distance_pct=round((close / fs.floor_price - 1) * 100, 2))

    _place_floors(api, state, bars, cfg)
    J.save_state(state)
    J.log("ratchet_done", moved=moved_any, positions=len(positions))
    return 0


def _place_floors(api: Alpaca, state: dict, bars: dict, cfg: dict) -> None:
    """Make the broker's resting orders match our intended floors."""
    positions = api.positions()
    if not floor_enabled(cfg):
        # Gate chose no floor: make sure nothing is left resting from an
        # earlier mode, or it would keep holding quantity and could still fire.
        for s in positions:
            try:
                api.cancel_floor_for(s)
            except BrokerError as e:
                J.log("floor_cancel_failed", symbol=s, error=str(e)[:300])
        # state["floors"] is ALSO the position table that reconcile() checks. Wiping it here
        # (as this code did until 2026-09-26) made the bot's own buys look UNTRACKED on the
        # next cycle and halted it. Keep one tracking entry per held position, order_id None.
        state["floors"] = track_without_floor(state.get("floors") or {}, positions)
        return
    for s, raw in list(state.get("floors", {}).items()):
        pos = positions.get(s)
        if not pos:
            state["floors"].pop(s, None)
            continue
        fs = FloorState.from_dict(raw)
        qty = abs(float(pos["qty"]))
        if qty <= 0:
            state["floors"].pop(s, None)
            continue
        try:
            api.cancel_floor_for(s)
            o = api.protective_stop(s, qty, fs.floor_price, limit_for(fs.floor_price, cfg))
            if o is None:
                fs.order_id = None
                state["floors"][s] = fs.to_dict()
                J.log("floor_bot_side", symbol=s, stop=round(fs.floor_price, 2),
                      note=f"{api.caps.name} cannot hold a resting protective order; this floor "
                           "is enforced only while the ratchet loop runs")
                continue
            fs.order_id = o.get("id")
            state["floors"][s] = fs.to_dict()
            J.log("floor_resting", symbol=s, stop=round(fs.floor_price, 2),
                  limit=round(limit_for(fs.floor_price, cfg), 2), order_id=fs.order_id)
        except BrokerError as e:
            J.log("floor_place_failed", symbol=s, error=str(e)[:300],
                  note="position is running WITHOUT a resting floor; ratchet loop is the only protection")


# ---------------------------------------------------------------- pulse / flatten
def cmd_pulse(cfg, args):
    api = _connect(cfg)
    state = J.load_state()
    acct, positions = api.account(), api.positions()
    equity = float(acct["equity"])
    hwm = float(state.get("high_water_equity") or equity)
    gate_ok, gate_why = _gate_ok(cfg)

    print("\n" + "=" * 66)
    print(f"  PAWL pulse -- {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}  [{cfg['meta']['mode']}]")
    print("=" * 66)
    print(f"  venue         {api.caps.name}  ({api.caps.roundtrip_taker:.2%} round trip, floor: {api.caps.floor_mode()})")
    print(f"  equity        ${equity:,.2f}")
    print(f"  cash          ${float(acct['cash']):,.2f}")
    print(f"  high-water    ${hwm:,.2f}   (drawdown {0 if hwm<=0 else (hwm-equity)/hwm:.2%})")
    print(f"  gate          {'OK' if gate_ok else 'BLOCKED'} -- {gate_why}")
    print(f"  rotation      {'enabled' if state.get('rotation_enabled') else 'disabled (core-only)'}")
    print(f"  halt          {state.get('manual_halt')}")
    rt = state.get("roundtrips", {}).get(datetime.now(timezone.utc).strftime("%Y-%m"), 0)
    print(f"  round trips   {rt}/{cfg['risk']['monthly_roundtrip_budget']} this month")
    print("-" * 66)
    if not positions:
        print("  flat")
    for s, p in positions.items():
        fl = state.get("floors", {}).get(s)
        last = float(p["current_price"])
        line = f"  {s:<10} ${float(p['market_value']):>10,.2f}  P/L {float(p['unrealized_plpc']):>7.2%}"
        if fl:
            room = (last / fl["floor_price"] - 1) * 100
            line += f"   floor ${fl['floor_price']:>10,.2f} ({room:+.1f}% away)"
        else:
            line += "   NO FLOOR"
        print(line)
    print("=" * 66 + "\n")
    return 0


def _flatten(api, state, cfg, reason: str):
    for s, p in api.positions().items():
        try:
            api.cancel_floor_for(s)
            api.market(s, "sell", abs(float(p["qty"])))
            J.log("flatten_sell", symbol=s, reason=reason)
        except BrokerError as e:
            J.log("flatten_failed", symbol=s, error=str(e)[:300])
    state["floors"] = {}


def cmd_flatten(cfg, args):
    api = _connect(cfg)
    state = J.load_state()
    _flatten(api, state, cfg, reason="manual flatten")
    state["manual_halt"] = True
    J.save_state(state)
    J.log("flattened", note="manual_halt set; clear it in state to resume")
    return 0


def cmd_venues(cfg, args):
    """Print the capability sheet. The venue question is not a fee question --
    it is a question about what the venue lets the STRATEGY be."""
    print(f"\n{'venue':<42}{'RT taker':>9}{'floor mode':>18}{'short':>7}{'perps':>7}{'paper':>7}")
    print("-" * 92)
    for v in ALL_VENUES:
        c = capabilities(v)
        print(f"{c.name[:41]:<42}{c.roundtrip_taker:>8.2%}{c.floor_mode():>18}"
              f"{'yes' if c.shorting else 'no':>7}{'yes' if c.perpetuals else 'no':>7}"
              f"{'yes' if c.paper_supported else 'no':>7}")
    print("-" * 92)
    for v in ALL_VENUES:
        c = capabilities(v)
        print(f"\n  {c.name}\n    {c.notes}")
    cur = cfg.get("broker", {}).get("venue", "alpaca")
    print(f"\n  configured: {cur}\n")
    return 0


def _apply_gate_floor_mode(cfg) -> None:
    """Live runs the floor mode the gate measured, not the config default.
    Otherwise the gate can report the no-floor arm while live places a floor
    (on Alpaca bars the catastrophe floor fires on phantom wicks: Sharpe 0.62
    with it vs 0.80 without, 2021-2026)."""
    if not os.path.exists(GATE_PATH):
        return
    with open(GATE_PATH, encoding="utf-8") as fh:
        mode = json.load(fh).get("floor_mode")
    if mode in ("tight", "catastrophe", "none"):
        cfg["floor"]["mode"] = mode


def main():
    ap = argparse.ArgumentParser(prog="run_pawl.py")
    ap.add_argument("command", choices=["gate", "cycle", "ratchet", "pulse", "flatten", "venues"])
    ap.add_argument("--config", default="config/pawl.yaml")
    ap.add_argument("--days", type=int, default=1800, help="history window for the gate")
    ap.add_argument("--csv-dir", default=None, help="backtest from local CSVs instead of the API")
    args = ap.parse_args()

    cfg = kcfg.load(args.config)
    if args.command in ("cycle", "ratchet", "pulse"):
        _apply_gate_floor_mode(cfg)
    fn = {"gate": cmd_gate, "cycle": cmd_cycle, "ratchet": cmd_ratchet,
          "pulse": cmd_pulse, "flatten": cmd_flatten, "venues": cmd_venues}[args.command]
    try:
        return fn(cfg, args)
    except BrokerError as e:
        J.log("fatal_broker_error", error=str(e)[:400])
        return 2


if __name__ == "__main__":
    sys.exit(main())
