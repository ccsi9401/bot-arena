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
    return yaml.safe_load((ROOT / "config" / "steward.yaml").read_text(encoding="utf-8"))


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


def dollars(order):
    return (order["notional"] if order.get("notional") is not None
            else (order["qty"] or 0) * order["ref_price"])


def run():
    c = cfg()
    # steward config uses its own universe naming; patch stocks list to fakes
    c["universe"]["stocks"] = [f"S{i}" for i in range(8)]
    # The live config is INDEX-ONLY (stocks weighted 0), so this block has to supply
    # its own stock-sleeve profile or it would silently test nothing. The sleeve code
    # is still the revert path, so it stays covered here regardless of what ships.
    # index_only_targets() below covers the config that is actually live.
    c["strategy"]["weights"]["risk_on"] = {"stocks": 0.45, "index": 0.25,
                                           "defensive": 0.20, "cash": 0.10}
    c["risk"]["max_position_weight"] = 0.12

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
    frac = {s: True for s in list(a["targets"]) + ["X"]}
    p = plan(a["targets"], a, account(), [], prices, c, kill_tripped=False,
             fractionable=frac)
    check("planner emits buys from flat", len(p["orders"]) >= 7
          and all(o["side"] == "buy" for o in p["orders"]))
    check("planner respects position cap",
          all(w <= c["risk"]["max_position_weight"] + 1e-9
              for w in p["targets_final"].values()))

    # drift band: tiny deviation -> no trade
    positions = [{"symbol": s, "qty": w * 50000 / 100, "avg_entry": 100,
                  "market_value": w * 50000 * 1.005, "unrealized_pl": 0,
                  "current_price": 100.5} for s, w in a["targets"].items()]
    p2 = plan(a["targets"], a, account(), positions, prices, c, kill_tripped=False,
              fractionable=frac)
    check("drift band suppresses fidgeting", len(p2["orders"]) == 0)

    # kill switch: a drawdown past the inception floor forces risk-off.
    # Derived from config — hardcoding $39k silently stopped testing anything
    # the moment starting_equity changed.
    below_floor = c["starting_equity"] * (1 - c["risk"]["kill_switch_drawdown_pct"] / 100) * 0.975
    p3 = plan(a["targets"], a, account(equity=below_floor, cash=below_floor), [], prices, c,
              kill_tripped=False, fractionable=frac)
    check("kill switch forces defense", any("KILL" in h for h in p3["halts"])
          and all(s in c["universe"]["defensive_etfs"] for s in p3["targets_final"]))

    # sells come before buys
    positions2 = [{"symbol": "X", "qty": 100, "avg_entry": 100,
                   "market_value": 10000, "unrealized_pl": 0, "current_price": 100}]
    p4 = plan(a["targets"], a, account(equity=50000, cash=40000), positions2,
              prices, c, kill_tripped=False, fractionable=frac)
    sides = [o["side"] for o in p4["orders"]]
    check("sells ordered before buys", sides.index("sell") == 0 if "sell" in sides else False)
    xo = next(o for o in p4["orders"] if o["symbol"] == "X")
    check("full exit sells the exact position, no dust",
          xo["side"] == "sell" and abs(xo["qty"] - 100) < 1e-9
          and xo.get("notional") is None)

    # ---- sizing v2: the 2026-08-14 truncation bug ----
    # Real prices from journal/steward_20260814_1551 — expensive shares are where
    # whole-share flooring parked 5.3% of the book in cash.
    px_real = {"AMAT": 506.65, "AMD": 510.315, "CAT": 856.745, "GLD": 401.44,
               "IEF": 93.04, "INTC": 102.695, "LRCX": 332.82, "MU": 969.535,
               "QQQ": 730.55, "SHY": 82.015, "SPY": 776.39}
    t_real = {"AMAT": 0.075, "AMD": 0.075, "CAT": 0.075, "GLD": 0.0667,
              "IEF": 0.0667, "INTC": 0.075, "LRCX": 0.075, "MU": 0.075,
              "QQQ": 0.12, "SHY": 0.0667, "SPY": 0.12}
    eq = 49997.49
    acct_real = account(equity=eq, cash=eq)

    pf = plan(t_real, a, acct_real, [], px_real, c, kill_tripped=False,
              fractionable={s: True for s in t_real})
    invested = sum(dollars(o) for o in pf["orders"])
    target_dollars = sum(t_real.values()) * eq
    check("notional sizing invests to target within 0.1%",
          abs(invested - target_dollars) / eq < 0.001)
    check("notional sizing leaves planned cash, not 16%",
          abs(pf["projected_cash_weight"] - pf["cash_target"]) < 0.002)
    check("notional orders carry a dollar amount",
          all(o["notional"] is not None for o in pf["orders"] if o["side"] == "buy"))

    pw = plan(t_real, a, acct_real, [], px_real, c, kill_tripped=False)
    invested_w = sum(dollars(o) for o in pw["orders"])
    check("whole-share fallback recovers most of the truncation drag",
          invested_w / eq > 0.87)  # v1 floored to 83.7%
    check("whole-share sweep is logged",
          any("sweep" in n for n in pw["notes"]))

    # v1 could never top up a gap smaller than one share (int() -> 0 -> skipped)
    mu_short = [{"symbol": "MU", "qty": 3, "avg_entry": 969.535,
                 "market_value": 2908.61, "unrealized_pl": 0,
                 "current_price": 969.535}]
    p5 = plan({"MU": 0.075}, a, account(), mu_short, {"MU": 969.535}, c,
              kill_tripped=False, fractionable={"MU": True})
    check("sub-one-share top-up is no longer dropped",
          len(p5["orders"]) == 1
          and abs(dollars(p5["orders"][0]) - (0.075 * 50000 - 2908.61)) < 1.0)

    # cash guard: never plan more buying than the account can fund
    p6 = plan(t_real, a, account(equity=eq, cash=5000), [], px_real, c,
              kill_tripped=False, fractionable={s: True for s in t_real})
    check("cash guard keeps buys within available cash",
          sum(dollars(o) for o in p6["orders"] if o["side"] == "buy") <= 5000)

    # ---- cash-drag sweep: the 2026-08-21 stranded-cash bug ----
    # Every holding sat 0.0-1.0pp under target — each inside the 1.5% band, together
    # 4.5pp of the book stuck in cash, with no single position able to release it.
    # current_weights verbatim from journal/steward_20260821_1530/plan.json.
    w_drag = {"AMAT": 0.0707, "AMD": 0.0676, "CAT": 0.0681, "GLD": 0.0694,
              "IEF": 0.0665, "INTC": 0.0665, "LRCX": 0.0707, "MU": 0.0595,
              "QQQ": 0.1169, "SHY": 0.0672, "SPY": 0.1098}
    eq_d = 48838.73
    px_d = dict(px_real, MU=968.30)

    def held(weights, equity=eq_d):
        return [{"symbol": s, "qty": w * equity / px_d[s], "avg_entry": px_d[s],
                 "market_value": w * equity, "unrealized_pl": 0.0,
                 "current_price": px_d[s]} for s, w in weights.items()]

    def acct_for(weights, equity=eq_d):
        return account(equity=equity, cash=(1 - sum(weights.values())) * equity)

    pd_ = plan(t_real, a, acct_for(w_drag), held(w_drag), px_d, c,
               kill_tripped=False, fractionable={s: True for s in px_d})
    check("cash-drag sweep fires when cash is stranded above target",
          pd_["cash_drag_sweep"] is True)
    check("cash-drag sweep closes the gap to target",
          abs(pd_["projected_cash_weight"] - pd_["cash_target"]) < 0.005)
    check("cash-drag sweep buys the sub-band underweights, not just MU",
          len([o for o in pd_["orders"] if o["side"] == "buy"]) >= 7)
    check("cash-drag sweep never sells to raise cash it already has",
          not any(o["side"] == "sell" for o in pd_["orders"]))
    check("cash-drag sweep skips dust below the minimum notional",
          all(dollars(o) >= c["strategy"]["min_order_notional_pct"] * eq_d
              for o in pd_["orders"]))
    check("cash-drag sweep is logged", any("Cash drag" in n for n in pd_["notes"]))

    # ...and it must not become a new source of fidgeting: rerun on the post-sweep
    # book (cash back at target) and nothing should trade.
    pf2 = plan(t_real, a, acct_for(t_real), held(t_real), px_d, c,
               kill_tripped=False, fractionable={s: True for s in px_d})
    check("no sweep once cash is back at target", pf2["cash_drag_sweep"] is False)
    check("sweep is idempotent — no churn on the next cycle", len(pf2["orders"]) == 0)

    # cash slightly above target but inside cash_drag_band -> still no trading
    w_near = {s: w * 0.995 for s, w in t_real.items()}   # ~0.45pp of drag
    pn = plan(t_real, a, acct_for(w_near), held(w_near), px_d, c,
              kill_tripped=False, fractionable={s: True for s in px_d})
    check("small cash drag stays inside the band (no fidgeting)",
          pn["cash_drag_sweep"] is False and len(pn["orders"]) == 0)

    # a real sell signal still fires normally during a sweep
    w_over = dict(w_drag, GLD=0.10)                      # GLD 3.3pp over target
    po = plan(t_real, a, acct_for(w_over), held(w_over), px_d, c,
              kill_tripped=False, fractionable={s: True for s in px_d})
    check("out-of-band sells still fire during a sweep",
          any(o["side"] == "sell" and o["symbol"] == "GLD" for o in po["orders"]))

    # the sweep must still respect the cash floor
    poor = plan(t_real, a, account(equity=eq_d, cash=400), held(w_drag), px_d, c,
                kill_tripped=False, fractionable={s: True for s in px_d})
    check("cash-drag sweep respects available cash",
          sum(dollars(o) for o in poor["orders"] if o["side"] == "buy") <= 400)

    # ---- the dust floor must scale, or a small book silently strands cash ----
    # A fixed $25 floor is 0.05% of $50k but 0.50% of $5k. At $5k that would block
    # every sweep top-up under half a point of drift, which is the stranded-cash bug
    # the sweep exists to prevent, reintroduced by account size alone.
    small = plan(t_real, a, acct_for(w_drag, 5000), held(w_drag, 5000), px_d, c,
                 kill_tripped=False, fractionable={s: True for s in px_d})
    check("cash-drag sweep still fires on a small book", small["cash_drag_sweep"] is True)
    check("small book still closes the gap to target",
          abs(small["projected_cash_weight"] - small["cash_target"]) < 0.005)
    check("small book is not blocked by a dollar dust floor",
          len([o for o in small["orders"] if o["side"] == "buy"]) >= 7)
    floor_pct = c["strategy"]["min_order_notional_pct"]
    check("dust floor is proportional, not fixed", floor_pct < 0.001)
    check("dust floor at $5k is well under a sweep top-up",
          floor_pct * 5000 < 0.005 * 5000)

    # ---- kill switch: two measures, deliberately different thresholds ----
    peak_lim = c["risk"]["kill_switch_peak_drawdown_pct"]

    # a grown book, ordinary correction: must NOT halt. This is the case the old
    # single-threshold design got wrong in the other direction (never fired at all).
    grown = plan(t_real, a, account(equity=85000, cash=9000), held(w_drag, 85000),
                 px_d, c, kill_tripped=False, fractionable={s: True for s in px_d},
                 peak_equity=105000)                       # -19.0% off peak
    check("ordinary correction on a grown book does not halt",
          not grown["halts"] and abs(grown["peak_drawdown_pct"] + 0) > 0)
    check("peak drawdown is reported for the journal",
          abs(grown["peak_drawdown_pct"] - 19.05) < 0.1)

    # the historical worst (-19.4%) must sit safely inside the limit
    check("3y worst peak-to-trough is inside the peak limit", peak_lim > 19.4 + 5)

    # a genuine collapse off the peak: MUST halt, even though equity is still
    # far above the inception floor, which is exactly the hole being closed.
    crash = plan(t_real, a, account(equity=70000, cash=9000), held(w_drag, 70000),
                 px_d, c, kill_tripped=False, fractionable={s: True for s in px_d},
                 peak_equity=105000)                       # -33.3% off peak
    check("collapse off the peak halts even when above the inception floor",
          any("KILL" in h for h in crash["halts"])
          and all(s in c["universe"]["defensive_etfs"] for s in crash["targets_final"]))
    check("halt names which measure fired",
          any("off peak" in h for h in crash["halts"]))

    # inception floor still works on its own, with no peak supplied at all
    early = plan(t_real, a, account(equity=below_floor, cash=below_floor), [], px_d, c,
                 kill_tripped=False, fractionable={s: True for s in px_d})
    check("inception floor still fires with no peak reference",
          any("from inception" in h for h in early["halts"]))
    check("peak drawdown is None when no peak is supplied",
          early["peak_drawdown_pct"] is None)

    # ---- the LIVE scanner: not covered by the gate, so cover it here ----
    # scan.py never runs in CI (the backtest uses its own build_scan), so a mistake in
    # it would surface on a Monday with the market open. Exercise it against fake bars.
    import numpy as np
    import pandas as pd
    from bots.portfolio import scan as pscan

    def fake_bars(symbols, days):
        idx = pd.bdate_range("2024-01-01", periods=days)
        frames = []
        for i, sym in enumerate(symbols):
            # np.arange, not pd.Series(range(...)): a Series carries its own integer
            # index and pandas would REINDEX against the dates, silently yielding NaN.
            px = pd.Series(100.0 + i + np.arange(days) * 0.05, index=idx)
            frames.append(pd.DataFrame(
                {"close": px, "volume": 5e6, "symbol": sym}).set_index("symbol", append=True))
        return pd.concat(frames)

    class FakeData:
        requested = {}

        def daily_bars(self, symbols, days):
            FakeData.requested["days"] = days
            return fake_bars(symbols, days)

    c_scan = cfg()
    c_scan["universe"]["stocks"] = ["AAPL"]
    c_scan["universe"]["index_etfs"] = ["SPY"]
    c_scan["universe"]["defensive_etfs"] = ["SHY"]
    sc = pscan.scan(FakeData(), c_scan)
    check("live scanner returns a symbol snapshot", len(sc["symbols"]) == 3)
    check("live scanner computes the trend flag",
          all("above_200sma" in f for f in sc["symbols"].values()))
    check("rising series reads as above its trend SMA",
          all(f["above_200sma"] for f in sc["symbols"].values()))

    # the trap: a trend period longer than the fetch window would leave every symbol
    # with no SMA, hence permanent risk-off, with nothing logged anywhere.
    c_long = cfg()
    c_long["universe"]["stocks"] = ["AAPL"]
    c_long["universe"]["index_etfs"] = ["SPY"]
    c_long["universe"]["defensive_etfs"] = ["SHY"]
    c_long["strategy"]["trend_sma_days"] = 400
    sc2 = pscan.scan(FakeData(), c_long)
    check("scanner widens its fetch when the trend period grows",
          FakeData.requested["days"] >= 400)
    check("a long trend period still produces a usable flag",
          all(f["sma200"] is not None and f["sma200"] == f["sma200"]   # not NaN
              and f["above_200sma"] for f in sc2["symbols"].values()))

    # missing price is skipped, not guessed
    p7 = plan({"ZZZ": 0.12}, a, account(), [], {}, c, kill_tripped=False)
    check("missing price skipped safely",
          not p7["orders"] and any("no live price" in n for n in p7["notes"]))

    index_only_targets()
    index_residue_disposal()
    cycle_scheduling()

    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nALL STEWARD TESTS PASS")
    return 1 if FAILURES else 0


def index_only_targets():
    """The SHIPPING config, unpatched — index-only + 200-day gate.

    Guards a silent failure rather than a crash: max_position_weight is applied
    per symbol and the overflow goes to CASH once every ETF is capped. With a 70%
    index sleeve over two ETFs and the old 0.12 cap, the book quietly holds 24%
    equity and 56% cash — the right-looking notes, an entirely different strategy.
    Nothing raises, so only an assertion on the resulting weights catches it.
    """
    c = cfg()
    if c["strategy"]["weights"]["risk_on"]["stocks"] > 0:
        return  # stock sleeve was restored; this check no longer applies

    idx = c["universe"]["index_etfs"]
    dfn = c["universe"]["defensive_etfs"]
    syms = {}
    for s, m in zip(idx, (0.10, 0.15)):
        syms[s] = {"sleeve": "index", "above_200sma": True, "mom_6m": m,
                   "mom_12_1": m, "close": 500.0, "avg_dollar_vol_20d": 9e9}
    for s in dfn:
        syms[s] = {"sleeve": "defensive", "above_200sma": True, "mom_6m": 0.01,
                   "mom_12_1": 0.01, "close": 100.0, "avg_dollar_vol_20d": 9e9}

    a = analyze({"symbols": syms, "benchmark": c["benchmark"],
                 "universe_size": len(syms)}, c)
    t = a["targets"]
    w = c["strategy"]["weights"]["risk_on"]
    eq = sum(v for k, v in t.items() if k in idx)
    de = sum(v for k, v in t.items() if k in dfn)

    check("index-only: no stock positions", not any(k not in idx + dfn for k in t))
    check("index-only: index sleeve reaches its full target weight",
          abs(eq - w["index"]) < 0.005)
    check("index-only: defensive ballast intact", abs(de - w["defensive"]) < 0.005)
    check("index-only: cash near target, not starved by position caps",
          abs((1 - eq - de) - w["cash"]) < 0.005)
    check("index-only: position cap leaves room for the sleeve",
          c["risk"]["max_position_weight"] * len(idx) >= w["index"] - 1e-9)


def index_residue_disposal():
    """The index sleeve only fills when EVERY index ETF is above its own trend.

    Regression for the 2026-08-29 finding: risk-on requires SPY above its 200-day,
    but QQQ is under no such obligation. One leg below trend left SPY capped at 40%,
    defensive at 20% and 40% of the book in cash — during RISK-ON — and no check
    could see it, because the planner derives cash_target from these very targets,
    so target and actual agreed at 40% and the cash-drag sweep stayed silent.
    """
    c = cfg()
    if c["strategy"]["weights"]["risk_on"]["stocks"] > 0:
        return
    idx = c["universe"]["index_etfs"]
    dfn = c["universe"]["defensive_etfs"]
    w = c["strategy"]["weights"]["risk_on"]

    def book(up_flags, residue_to):
        cc = cfg()
        cc["strategy"]["index_residue_to"] = residue_to
        syms = {}
        for s, m, up in zip(idx, (0.10, 0.15), up_flags):
            syms[s] = {"sleeve": "index", "above_200sma": up, "mom_6m": m,
                       "mom_12_1": m, "close": 500.0, "avg_dollar_vol_20d": 9e9}
        for s in dfn:
            syms[s] = {"sleeve": "defensive", "above_200sma": True, "mom_6m": 0.01,
                       "mom_12_1": 0.01, "close": 100.0, "avg_dollar_vol_20d": 9e9}
        # SPY defines the regime, so it must be the one held above trend.
        syms[c["benchmark"]]["above_200sma"] = True
        return analyze({"symbols": syms, "benchmark": cc["benchmark"],
                        "universe_size": len(syms)}, cc)

    a = book((True, False), "defensive")
    t = a["targets"]
    eq = sum(v for k, v in t.items() if k in idx)
    de = sum(v for k, v in t.items() if k in dfn)
    cash = 1 - eq - de

    check("residue: regime still reads risk-on with one index leg down",
          a["regime_on"] is True)
    check("residue: the lagging ETF is not held", idx[1] not in t)
    check("residue: cash stays at target instead of absorbing the sleeve",
          abs(cash - w["cash"]) < 0.005)
    check("residue: ballast absorbs the unfilled sleeve",
          de > w["defensive"] + 0.05)
    check("residue: book stays fully invested",
          abs((eq + de) - (1 - w["cash"])) < 0.005)
    check("residue: disposal is reported in the notes",
          any("residue routed" in n for n in a["notes"]))
    check("residue: nothing left stranded once routed",
          a["index_residue_pp"] == 0)

    # the old behaviour, kept available and now explicit rather than accidental
    b = book((True, False), "cash")
    cash_b = 1 - sum(b["targets"].values())
    check("residue: opting back into cash still works",
          cash_b > w["cash"] + 0.05 and b["index_residue_pp"] > 0)
    check("residue: the cash variant says so out loud",
          any("held as cash" in n for n in b["notes"]))

    # both legs healthy — unchanged from the shipping behaviour
    d = book((True, True), "defensive")
    check("residue: no residue when every index leg is above trend",
          d["index_residue_pp"] == 0
          and abs(sum(v for k, v in d["targets"].items() if k in dfn)
                  - w["defensive"]) < 0.005)


def cycle_scheduling():
    """The rebalance must be decided by state, not by what time the runner woke up.

    The old workflow asked 'is it Friday, UTC hour 19 or 20?'. Hour 20 UTC is past
    the 16:00 ET close, so market_open() rejected it: a 59-minute window per week,
    against cron drift measured at 3.5 and 8 hours. Cycles were skipped in silence.
    """
    import run_steward as rs
    from datetime import datetime
    from core.common import ET

    c = cfg()

    class FakeState:
        def __init__(self, last=None):
            self.d = {"last_cycle": {"date_et": last} if last else {}}
        def read(self, name, default):
            return self.d.get(name, default)
        def write(self, name, payload):
            self.d[name] = payload

    def at(y, m, d, hh=12):
        return datetime(y, m, d, hh, tzinfo=ET)

    # Aug 2026: 28th is a Friday, 31st the following Monday.
    fri, sat, mon, thu = at(2026, 8, 28), at(2026, 8, 29), at(2026, 8, 31), at(2026, 9, 3)

    check("anchor: Friday anchors to itself",
          f"{rs.week_anchor(c, fri):%Y-%m-%d}" == "2026-08-28")
    check("anchor: the weekend still belongs to Friday's week",
          f"{rs.week_anchor(c, sat):%Y-%m-%d}" == "2026-08-28")
    check("anchor: Monday still belongs to Friday's week",
          f"{rs.week_anchor(c, mon):%Y-%m-%d}" == "2026-08-28")
    check("anchor: the next Thursday is still that same week",
          f"{rs.week_anchor(c, thu):%Y-%m-%d}" == "2026-08-28")

    fresh = FakeState()
    check("due: a bot that has never cycled is due",
          rs.cycle_status(fresh, c, fri)["due"] is True)
    check("due: but not OVERDUE on the day of its slot",
          rs.cycle_status(fresh, c, fri)["overdue"] is False)
    check("due: a missed Friday is overdue by Monday",
          rs.cycle_status(fresh, c, mon)["overdue"] is True)
    check("due: overdue reports how far past the slot it is",
          rs.cycle_status(fresh, c, mon)["days_since_anchor"] == 3)

    done = FakeState("2026-08-28")
    check("due: a completed Friday closes the week",
          rs.cycle_status(done, c, mon)["due"] is False)
    check("due: and stays closed to the end of the week",
          rs.cycle_status(done, c, thu)["due"] is False)
    check("due: the next Friday opens a new week",
          rs.cycle_status(done, c, at(2026, 9, 4))["due"] is True)

    late = FakeState("2026-08-31")   # Friday missed, caught up on the Monday
    check("due: a catchup run satisfies the week it was owed",
          rs.cycle_status(late, c, thu)["due"] is False)
    check("due: and does not suppress the following Friday",
          rs.cycle_status(late, c, at(2026, 9, 4))["due"] is True)

    class Clock:
        def __init__(self, is_open): self.is_open = is_open
        def market_open(self): return self.is_open

    OPEN, SHUT = Clock(True), Clock(False)
    check("mode: auto rebalances when one is due and the market is open",
          rs.resolve_mode("auto", FakeState(), c, OPEN)[0] == "cycle")
    check("mode: auto falls back to a pulse when the market is shut",
          rs.resolve_mode("auto", FakeState(), c, SHUT)[0] == "pulse")
    check("mode: auto pulses once the week is already done",
          rs.resolve_mode("auto", FakeState(f"{rs.week_anchor(c):%Y-%m-%d}"),
                          c, OPEN)[0] == "pulse")
    check("mode: catchup does nothing when nothing was missed",
          rs.resolve_mode("catchup", FakeState(f"{rs.week_anchor(c):%Y-%m-%d}"),
                          c, OPEN)[0] == "noop")
    check("mode: an explicit cycle is still forced through",
          rs.resolve_mode("cycle", FakeState(f"{rs.week_anchor(c):%Y-%m-%d}"),
                          c, OPEN)[0] == "cycle")
    check("mode: an explicit pulse never rebalances",
          rs.resolve_mode("pulse", FakeState(), c, OPEN)[0] == "pulse")



if __name__ == "__main__":
    sys.exit(run())
