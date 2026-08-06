"""STEWARD offline tests — no network. Run: python -m tests.test_steward"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from core.common import ROOT
from bots.portfolio.analyzer import analyze
from bots.portfolio.planner import plan

FAILURES = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILURES.append(name)


def cfg():
    return yaml.safe_load((ROOT / "config" / "steward.yaml").read_text())


def fake_scan(c, spy_up=True):
    syms = {}

    def add(sym, sleeve, mom12, mom6, up):
        syms[sym] = {"close": 100.0, "last_bar_date": "2026-08-05",
                     "mom_12_1": mom12, "mom_6m": mom6, "sma200": 90.0,
                     "above_200sma": up, "vol_63d_ann": 0.2,
                     "avg_dollar_vol_20d": 2e8, "sleeve": sleeve}

    # 8 stocks, varied momentum; two disqualified (downtrend / negative momentum)
    moms = [0.80, 0.60, 0.50, 0.40, 0.30, 0.25, 0.20, 0.10]
    for i, m in enumerate(moms):
        add(f"S{i}", "stock", m, m / 2, up=(i != 6))
    syms["S7"]["mom_12_1"] = -0.10
    add("SPY", "index", 0.20, 0.10, spy_up)
    add("QQQ", "index", 0.30, 0.20, spy_up)
    for d in ("IEF", "GLD", "SHY"):
        add(d, "defensive", 0.02, 0.01, True)
    return {"mode": "portfolio", "asof_et": "2026-08-05T15:45:00-04:00",
            "benchmark": "SPY", "universe_size": len(syms),
            "scanned": len(syms), "symbols": syms}


def account(equity=50000, cash=50000):
    return {"equity": equity, "cash": cash, "last_equity": equity,
            "buying_power": cash, "status": "ACTIVE"}


def run():
    c = cfg()
    # steward config uses its own universe naming; patch stocks list to fakes
    c["universe"]["stocks"] = [f"S{i}" for i in range(8)]

    # ---- risk-on ----
    a = analyze(fake_scan(c, spy_up=True), c)
    check("regime on when SPY above 200sma", a["regime_on"])
    stock_targets = {s: w for s, w in a["targets"].items() if s.startswith("S") and s not in ("SPY", "SHY")}
    check("picks top-6 qualified stocks", len(stock_targets) == 6)
    check("skips downtrend stock S6", "S6" not in a["targets"])
    check("skips negative-momentum S7", "S7" not in a["targets"])
    check("defensive ballast present", all(d in a["targets"] for d in ("IEF", "GLD", "SHY")))
    check("weights sum <= 95%", sum(a["targets"].values()) <= 0.951)
    check("cash weight >= 5%", a["cash_weight"] >= 0.049)

    a2 = analyze(fake_scan(c, spy_up=True), c)
    check("deterministic (same scan -> same targets)", a == a2)

    # ---- risk-off ----
    b = analyze(fake_scan(c, spy_up=False), c)
    check("regime off when SPY below 200sma", not b["regime_on"])
    check("risk-off: no stocks", not any(s.startswith("S") and s[1].isdigit()
                                         for s in b["targets"]))
    check("risk-off: heavy defense", abs(sum(b["targets"].values()) - 0.60) < 0.01)

    # ---- planner ----
    prices = {s: 100.0 for s in list(a["targets"]) + ["X"]}
    p = plan(a["targets"], a, account(), [], prices, c, kill_tripped=False)
    check("planner emits buys from flat", len(p["orders"]) >= 7
          and all(o["side"] == "buy" for o in p["orders"]))
    check("planner respects position cap",
          all(w <= c["risk"]["max_position_weight"] + 1e-9
              for w in p["targets_final"].values()))

    # drift band: tiny deviation -> no trade
    positions = [{"symbol": s, "qty": w * 50000 / 100, "avg_entry": 100,
                  "market_value": w * 50000 * 1.005, "unrealized_pl": 0,
                  "current_price": 100.5} for s, w in a["targets"].items()]
    p2 = plan(a["targets"], a, account(), positions, prices, c, kill_tripped=False)
    check("drift band suppresses fidgeting", len(p2["orders"]) == 0)

    # kill switch: -22% drawdown forces risk-off
    p3 = plan(a["targets"], a, account(equity=39000, cash=39000), [], prices, c,
              kill_tripped=False)
    check("kill switch forces defense", any("KILL" in h for h in p3["halts"])
          and all(s in c["universe"]["defensive_etfs"] for s in p3["targets_final"]))

    # sells come before buys
    positions2 = [{"symbol": "X", "qty": 100, "avg_entry": 100,
                   "market_value": 10000, "unrealized_pl": 0, "current_price": 100}]
    p4 = plan(a["targets"], a, account(equity=50000, cash=40000), positions2,
              prices, c, kill_tripped=False)
    sides = [o["side"] for o in p4["orders"]]
    check("sells ordered before buys", sides.index("sell") == 0 if "sell" in sides else False)

    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nALL STEWARD TESTS PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(run())
