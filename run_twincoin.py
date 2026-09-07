#!/usr/bin/env python3
"""TWINCOIN runner — Twin-Coin Trend Bot on Alpaca PAPER.

Usage:
  python run_twincoin.py --mode signal     # no keys needed: fetch bars, print today's six-vote per coin
  python run_twincoin.py --mode preflight  # keys: auth, account, assets, data; prints a checklist, writes nothing
  python run_twincoin.py --mode arm        # keys: record account equity as the $500 ledger's zero point; enables cycle
  python run_twincoin.py --mode pulse      # keys: full dry run, prints what a cycle would do, writes nothing
  python run_twincoin.py --mode cycle      # keys: trade (exits before entries), writes state + journal
  python run_twincoin.py --mode flatten    # keys: cancel every order and close every position; keeps state file
  python run_twincoin.py --mode clear-halt # lift a reconcile halt after you have looked at the account

Exit codes: 0 ok · 1 error · 2 not armed (cycle refused) · 3 credentials missing.
Credentials: TWINCOIN_API_KEY / TWINCOIN_API_SECRET in the environment (a local .env in the
repo root is read for local runs). They are never printed or written.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from twincoin import ROOT, load_config, load_dotenv  # noqa: E402
from twincoin.broker import Alpaca, CredentialsError  # noqa: E402
from twincoin.data import load_market  # noqa: E402
from twincoin.engine import Engine, new_state  # noqa: E402
from twincoin.strategy import indicators_4h, indicators_daily, vote  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_NOT_ARMED, EXIT_NO_CREDS = 0, 1, 2, 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Journal:
    """JSONL per UTC day + trades.csv; echoes to stdout. enabled=False for dry modes."""

    def __init__(self, cfg: dict, mode: str, enabled: bool):
        self.dir = ROOT / cfg["paths"]["journal_dir"]
        self.trades_csv = ROOT / cfg["paths"]["trades_csv"]
        self.run_id, self.mode, self.enabled = uuid.uuid4().hex[:8], mode, enabled

    def write(self, event: str, **payload) -> None:
        print(f"[{event}] " + json.dumps(payload, default=str, sort_keys=True))
        if not self.enabled:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        row = {"ts": utcnow().isoformat(timespec="seconds"), "run_id": self.run_id, "mode": self.mode, "event": event, **payload}
        with open(self.dir / f"{utcnow():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def trade(self, row: dict) -> None:
        self.write("TRADE_CLOSED", **row)
        if not self.enabled:
            return
        new = not self.trades_csv.exists()
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)


def load_state(cfg: dict) -> dict | None:
    p = ROOT / cfg["paths"]["state"]
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_state(cfg: dict, st: dict) -> None:
    p = ROOT / cfg["paths"]["state"]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def armed(cfg: dict) -> bool:
    p = ROOT / cfg["paths"]["armed"]
    if not p.exists():
        return False
    return bool(json.loads(p.read_text(encoding="utf-8")).get("armed"))


# ------------------------------------------------------------------- modes
def mode_signal(cfg: dict) -> int:
    now = utcnow()
    daily, h4, notes = load_market(cfg, now)
    print(f"as of {now:%Y-%m-%d %H:%M} UTC  data: {json.dumps(notes)}")
    for sym in cfg["universe"]["symbols"]:
        d = indicators_daily(daily[sym], int(cfg["signal"]["ema50_slope_bars"]), int(cfg["signal"]["obv_lookback"]))
        h = indicators_4h(h4[sym])
        r, hr = d.iloc[-1], h.iloc[-1]
        b, s, det = vote(r, hr, tuple(cfg["signal"]["rsi_bull"]), float(cfg["signal"]["rsi_bear"]))
        verdict = "BUY" if b >= int(cfg["signal"]["vote_min"]) else ("SELL" if s >= int(cfg["signal"]["sell_vote_min"]) else "NO TRADE")
        print(f"{sym}: daily bar {d.index[-1]:%Y-%m-%d} close {r.close:,.2f}  ATR {r.atr:,.2f} ({r.atr / r.close:.1%})  "
              f"EMA20/50/200 {r.ema20:,.0f}/{r.ema50:,.0f}/{r.ema200:,.0f}  RSI {r.rsi:.1f}")
        print(f"   vote {b} bull / {s} bear  {det}  -> {verdict}"
              + ("" if r.close > r.ema200 else "  [below 200 EMA: no entries]"))
    return EXIT_OK


def mode_preflight(cfg: dict) -> int:
    b = Alpaca(cfg)
    a = b.account()
    ok = True
    print(f"account       {a.get('account_number')}  status={a.get('status')}  crypto_status={a.get('crypto_status')}")
    print(f"equity        ${float(a.get('equity', 0)):,.2f}  cash ${float(a.get('cash', 0)):,.2f}  (paper endpoint: {b.base})")
    if a.get("crypto_status") != "ACTIVE":
        print("!! crypto_status is not ACTIVE: enable crypto on this paper account"); ok = False
    for sym in cfg["universe"]["symbols"]:
        try:
            asset = b.asset(sym)
            print(f"asset {sym}   tradable={asset.get('tradable')} fractionable={asset.get('fractionable')} min_order_size={asset.get('min_order_size')}")
            ok &= bool(asset.get("tradable"))
        except Exception as e:
            print(f"!! asset {sym}: {e}"); ok = False
    pos = b.positions()
    print(f"positions     {pos if pos else 'none'}")
    oo = b.open_orders()
    print(f"open orders   {len(oo)}")
    daily, h4, notes = load_market(cfg)
    print(f"data          {json.dumps(notes)}")
    st = load_state(cfg)
    print(f"state         {'present, armed ' + str(st.get('armed_at')) if st else 'absent (run --mode arm)'}  armed.json={armed(cfg)}")
    print("PREFLIGHT " + ("OK" if ok else "FAILED"))
    return EXIT_OK if ok else EXIT_ERROR


def mode_arm(cfg: dict) -> int:
    b = Alpaca(cfg)
    a = b.account()
    if load_state(cfg):
        print("state already exists; refusing to re-arm (delete state/twincoin/twincoin_state.json by hand to start over)")
        return EXIT_ERROR
    if b.positions():
        print("account has open positions; flatten or adopt them consciously before arming")
        return EXIT_ERROR
    now = utcnow()
    st = new_state(cfg, float(a["equity"]), now)
    save_state(cfg, st)
    p = ROOT / cfg["paths"]["armed"]
    p.write_text(json.dumps({"armed": True, "armed_at": now.isoformat(timespec="seconds"), "account_equity_at_arm": float(a["equity"]),
                             "basis_equity": cfg["ledger"]["basis_equity"], "note": "written by run_twincoin.py --mode arm"}, indent=2), encoding="utf-8")
    print(f"armed: ledger basis ${cfg['ledger']['basis_equity']:.2f} against account equity ${float(a['equity']):,.2f} at {now:%Y-%m-%d %H:%M} UTC")
    return EXIT_OK


def mode_cycle(cfg: dict, dry: bool) -> int:
    mode = "pulse" if dry else "cycle"
    if not dry and not armed(cfg):
        print("not armed: run --mode arm first (state/twincoin/armed.json)")
        return EXIT_NOT_ARMED
    j = Journal(cfg, mode, enabled=not dry)
    b = Alpaca(cfg)
    st = load_state(cfg)
    if st is None:
        if dry:
            st = new_state(cfg, float(b.account()["equity"]), utcnow())
            j.write("pulse_note", note="no state file; using a fresh ledger for this dry run")
        else:
            print("no state file: run --mode arm first")
            return EXIT_NOT_ARMED
    now = utcnow()
    daily, h4, notes = load_market(cfg, now)
    j.write("data", **notes)
    eng = Engine(cfg, b, st, j, dry_run=dry)
    try:
        result = eng.cycle(daily, h4, now)
    except Exception as e:
        j.write("cycle_error", error=str(e), trace=traceback.format_exc()[-1500:])
        if not dry:
            save_state(cfg, st)
        return EXIT_ERROR
    if not dry:
        save_state(cfg, st)
    return EXIT_OK


def mode_flatten(cfg: dict) -> int:
    j = Journal(cfg, "flatten", enabled=True)
    b = Alpaca(cfg)
    for sym in cfg["universe"]["symbols"]:
        n = b.cancel_symbol_orders(sym)
        j.write("orders_canceled", symbol=sym, n=n, why="flatten")
    for sym, p in b.positions().items():
        if sym not in cfg["universe"]["symbols"]:
            continue
        o = b.submit(sym, "sell", p["qty_available"], "market")
        o = b.wait_fill(o["id"])
        j.write("flatten_exit", symbol=sym, status=o.get("status"), price=o.get("filled_avg_price"), qty=o.get("filled_qty"))
    st = load_state(cfg)
    if st:
        st["positions"] = {}
        save_state(cfg, st)
    return EXIT_OK


def mode_clear_halt(cfg: dict) -> int:
    st = load_state(cfg)
    if not st:
        print("no state"); return EXIT_ERROR
    st["reconcile_halt"] = False
    save_state(cfg, st)
    print("reconcile_halt cleared")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["signal", "preflight", "arm", "pulse", "cycle", "flatten", "clear-halt"], default="pulse")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    load_dotenv()
    cfg = load_config(args.config)
    try:
        if args.mode == "signal":
            return mode_signal(cfg)
        if args.mode == "preflight":
            return mode_preflight(cfg)
        if args.mode == "arm":
            return mode_arm(cfg)
        if args.mode == "pulse":
            return mode_cycle(cfg, dry=True)
        if args.mode == "cycle":
            return mode_cycle(cfg, dry=False)
        if args.mode == "flatten":
            return mode_flatten(cfg)
        if args.mode == "clear-halt":
            return mode_clear_halt(cfg)
    except CredentialsError as e:
        print(f"credentials: {e}")
        return EXIT_NO_CREDS
    except Exception as e:
        print(f"error: {e}")
        traceback.print_exc()
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
