#!/usr/bin/env python3
"""GLIDER reflection — learn from LIVE fills without chasing noise.

Weekly. Pulls GLIDER's filled orders from Alpaca, pairs buys with sells into
round-trips, and compares the live per-trade return distribution against the
backtest reference (reports/glider_learn/reference_trades.json, falling back to
reports/backtest/glider.json).

Statistically honest rule:
  - do nothing until learning.min_live_trades closed trades exist
  - bootstrap the backtest reference at the live sample size; if the live mean
    return sits below learning.underperform_pctile, set risk_per_trade_pct to
    half of learning.base_risk_pct  (the bot is not doing what the backtest said)
  - if live is back inside the distribution, restore base_risk_pct
Every run writes state/glider/reflection.json; the config edit is journaled there.

Usage:  python glider_reflect.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

from core.common import ROOT, load_config, now_et
from core.broker import make_broker

CFG_PATH = ROOT / "config" / "glider.yaml"
STATE = ROOT / "state" / "glider"
REF_LEARN = ROOT / "reports" / "glider_learn" / "reference_trades.json"
REF_GATE = ROOT / "reports" / "backtest" / "glider.json"


def round_trips(fills: list[dict]) -> list[dict]:
    """FIFO-match buy fills to sell fills per symbol → [{symbol, entry, exit, qty, ret_pct}]."""
    fills = sorted(fills, key=lambda f: f["filled_at"])
    lots: dict[str, deque] = defaultdict(deque)
    out = []
    for f in fills:
        if f["side"] == "buy":
            lots[f["symbol"]].append([f["filled_qty"], f["price"], f["filled_at"]])
            continue
        qty = f["filled_qty"]
        while qty > 1e-9 and lots[f["symbol"]]:
            lot = lots[f["symbol"]][0]
            take = min(qty, lot[0])
            out.append({"symbol": f["symbol"], "entry": lot[1], "exit": f["price"],
                        "qty": take, "opened": lot[2], "closed": f["filled_at"],
                        "ret_pct": (f["price"] / lot[1] - 1) * 100})
            lot[0] -= take; qty -= take
            if lot[0] <= 1e-9:
                lots[f["symbol"]].popleft()
    return out


def reference_returns() -> tuple[list[float], str]:
    for path in (REF_LEARN, REF_GATE):
        if path.exists():
            d = json.loads(path.read_text())
            trades = d["trades"]
            rets = [(t["exit"] / t["entry"] - 1) * 100 for t in trades if t.get("entry")]
            if len(rets) >= 30:
                return rets, path.name
    return [], "none"


def set_yaml_key(text: str, key: str, value) -> str:
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*)([^#\n]*?)(\s*#.*)?$", re.M)
    return pat.sub(lambda m: f"{m.group(1)}{value}{(m.group(3) or '')}", text, count=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config("glider")
    L = cfg.get("learning", {})
    STATE.mkdir(parents=True, exist_ok=True)
    out = {"ts_et": now_et().isoformat(), "action": "none"}

    def done(msg: str) -> int:
        out["summary"] = msg
        (STATE / "reflection.json").write_text(json.dumps(out, indent=2, default=str))
        print(json.dumps(out, indent=2, default=str))
        return 0

    if not L.get("enabled", False):
        return done("learning disabled — no action")

    broker = make_broker(cfg)
    fills = broker.filled_orders()
    trips = round_trips(fills)
    out["live_closed_trades"] = len(trips)
    need = L.get("min_live_trades", 30)
    if len(trips) < need:
        return done(f"{len(trips)} closed live trades < {need} needed — nothing to learn from yet")

    live = np.array([t["ret_pct"] for t in trips])
    ref, ref_name = reference_returns()
    out["reference"] = ref_name
    if not ref:
        return done("no backtest reference trades found — run the gate or learner first")

    rng = np.random.default_rng(0)
    boot = np.array([rng.choice(ref, len(live)).mean() for _ in range(2000)])
    pct = L.get("underperform_pctile", 5)
    p_low = float(np.percentile(boot, pct))
    live_mean = float(live.mean())
    out.update({"live_mean_ret_pct": round(live_mean, 3),
                "live_win_rate": round(float((live > 0).mean() * 100), 1),
                "backtest_mean_ret_pct": round(float(np.mean(ref)), 3),
                f"backtest_p{pct}_at_n{len(live)}": round(p_low, 3),
                "live_percentile_in_backtest": round(float((boot < live_mean).mean() * 100), 1)})

    base = float(L.get("base_risk_pct", cfg["risk"]["risk_per_trade_pct"]))
    current = float(cfg["risk"]["risk_per_trade_pct"])
    target = round(base / 2, 2) if live_mean < p_low else base
    if abs(target - current) < 1e-9:
        return done(f"risk_per_trade_pct stays {current} (live mean {live_mean:.2f}% vs p{pct} {p_low:.2f}%)")

    out["action"] = f"risk_per_trade_pct {current} → {target}"
    if not args.dry_run:
        text = CFG_PATH.read_text(encoding="utf-8")
        CFG_PATH.write_text(set_yaml_key(text, "risk_per_trade_pct", target), encoding="utf-8")
    return done(("HALVED risk: live underperforms backtest beyond chance"
                 if target < base else "RESTORED base risk: live back inside backtest distribution")
                + (" (dry run, not written)" if args.dry_run else ""))


if __name__ == "__main__":
    sys.exit(main())
