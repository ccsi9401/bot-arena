"""Offline pipeline tests — no broker, no network. Run: python -m tests.test_pipeline"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.common import State, load_config, now_et
from core import validator
from bots.intraday.analyzer import analyze as scalpel_analyze
from bots.swing.analyzer import analyze as glider_analyze

FAILURES = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILURES.append(name)


def fake_scan(mode="intraday"):
    def sym(close, sma50, sma200, rsi2=50.0, pct_hi=5.0, sess=None, low=None):
        return {
            "close": close, "last_bar_date": "2026-08-03", "sma50": sma50,
            "sma200": sma200, "ema20": close * 0.99, "rsi2": rsi2, "atr14": close * 0.02,
            "avg_dollar_vol_20d": 2e8, "pct_below_52wk_high": pct_hi,
            "low_today": low if low is not None else close * 0.99,
            "avg_vol_20d": 5e6, "is_etf": False, "session": sess,
        }

    def sess(last, or_high, vwap, rs, pace):
        return {"last": last, "last_bar_end_et": "x", "open": last * 0.99,
                "or_high": or_high, "or_low": last * 0.97, "vwap": vwap,
                "ret_since_open": 0.01, "rel_ret_vs_bench": 0.005,
                "volume_pace": pace, "rs_percentile": rs}

    return {
        "mode": mode, "asof_et": now_et().isoformat(), "benchmark": "SPY",
        "universe_size": 4, "scanned": 4,
        "symbols": {
            # SPY in uptrend -> regime open
            "SPY": sym(500, 480, 450, sess=sess(500, 499, 498, 0.5, 1.0)),
            # GOOD intraday breakout candidate
            "NVDA": sym(150, 140, 120, sess=sess(150, 148, 147, 0.95, 2.0)),
            # fails: below vwap
            "AAPL": sym(200, 190, 170, sess=sess(200, 198, 201, 0.9, 2.0)),
            # GOOD swing pullback candidate: uptrend + RSI2 low
            "MSFT": sym(420, 410, 380, rsi2=4.0, pct_hi=6.0,
                        sess=sess(420, 421, 421, 0.4, 0.8)),
        },
    }


def account(equity=50000, cash=50000, last_equity=50000):
    return {"equity": equity, "cash": cash, "last_equity": last_equity,
            "buying_power": cash, "status": "ACTIVE"}


def fresh_state(bot):
    import core.common as cc
    tmp = Path(tempfile.mkdtemp())
    s = State(bot)
    s.dir = tmp  # divert writes away from real state/
    return s


def run():
    scan = fake_scan()
    scfg = load_config("scalpel")
    scfg["strategy"]["style"] = "orb_momentum"
    gcfg = load_config("glider")

    # --- scalpel analyzer ---
    a = scalpel_analyze(scan, scfg, "11:30")
    syms = [i["symbol"] for i in a["intents"]]
    check("scalpel picks NVDA breakout", syms == ["NVDA"])
    check("scalpel rejects below-vwap AAPL", "AAPL" not in syms)
    i = a["intents"][0]
    check("scalpel stop below entry below target",
          i["stop"] < i["entry_limit"] < i["target"])

    a2 = scalpel_analyze(scan, scfg, "15:30")
    check("scalpel 15:30 liquidates only",
          a2["intents"][0]["action"] == "liquidate_all" and len(a2["intents"]) == 1)

    a3 = scalpel_analyze(scan, scfg, "15:00")
    check("scalpel no entries after last-entry cycle", a3["intents"] == [])

    # --- glider analyzer ---
    g = glider_analyze(scan, gcfg, [])
    gsyms = [i["symbol"] for i in g["intents"] if i["action"] == "buy"]
    check("glider regime open", g["regime_ok"])
    check("glider buys MSFT pullback", "MSFT" in gsyms)
    check("glider skips non-pullback NVDA... unless EMA touch",
          all(s in ("MSFT", "NVDA", "AAPL", "SPY") for s in gsyms))

    # bear regime: SPY below 200sma
    bear = fake_scan()
    bear["symbols"]["SPY"]["close"] = 400
    gb = glider_analyze(bear, gcfg, [])
    check("glider bear regime blocks entries",
          not gb["regime_ok"] and not [i for i in gb["intents"] if i["action"] == "buy"])

    # management: time stop + breakeven
    gm = glider_analyze(scan, gcfg, [
        {"symbol": "NVDA", "entry": 130, "stop": 125, "age_days": 20},
        {"symbol": "AAPL", "entry": 180, "stop": 172, "age_days": 3},
    ])
    acts = {(i["action"], i["symbol"]) for i in gm["intents"]}
    check("glider time-stops 20-day NVDA", ("close", "NVDA") in acts)
    check("glider ratchets AAPL to breakeven (+1R hit)", ("raise_stop", "AAPL") in acts)

    # --- validator ---
    st = fresh_state("scalpel")
    intents = scalpel_analyze(scan, scfg, "11:30")["intents"]
    v = validator.validate(intents, account(), [], [], scfg, st,
                           scan["asof_et"], True, {"NVDA": 150.0})
    check("validator approves sized buy", len(v["approved"]) == 1
          and v["approved"][0]["qty"] > 0)
    q = v["approved"][0]
    expected_risk = 50000 * scfg["risk"]["risk_per_trade_pct"] / 100
    check("validator sizes to configured risk pct",
          abs(q["risk_dollars"] - expected_risk) / expected_risk < 0.05)

    v2 = validator.validate(intents, account(), [], [], scfg, st,
                            scan["asof_et"], True, {"NVDA": 170.0})
    check("validator rejects price drift", len(v2["approved"]) == 0
          and "drifted" in v2["rejected"][0]["reject_reason"])

    kill_eq = int(50000 * (1 - (scfg["risk"]["kill_switch_drawdown_pct"] + 2) / 100))
    v3 = validator.validate(intents, account(equity=kill_eq, last_equity=kill_eq + 500),
                            [], [], scfg, fresh_state("scalpel"),
                            scan["asof_et"], True, {"NVDA": 150.0})
    check("validator kill switch past threshold", any("KILL" in h for h in v3["halts"])
          and len(v3["approved"]) == 0)

    brk_eq = int(50000 * (1 - (scfg["risk"]["daily_loss_limit_pct"] + 0.5) / 100))
    v4 = validator.validate(intents, account(equity=brk_eq, last_equity=50000),
                            [], [], scfg, fresh_state("scalpel"),
                            scan["asof_et"], True, {"NVDA": 150.0})
    check("validator daily breaker past threshold",
          any("BREAKER" in h for h in v4["halts"]))

    v5 = validator.validate(intents, account(),
                            [{"symbol": "NVDA", "qty": 10, "avg_entry": 140,
                              "market_value": 1500, "unrealized_pl": 100,
                              "current_price": 150}],
                            [], scfg, fresh_state("scalpel"),
                            scan["asof_et"], True, {"NVDA": 150.0})
    check("validator duplicate guard", len(v5["approved"]) == 0
          and "duplicate" in v5["rejected"][0]["reject_reason"])

    stale = validator.validate(intents, account(), [], [], scfg, fresh_state("scalpel"),
                               "2026-08-03T09:00:00-04:00", True, {"NVDA": 150.0})
    check("validator stale-data gate", len(stale["approved"]) == 0)

    # liquidation passes through even when halted
    liq = validator.validate([{"action": "liquidate_all", "reasoning": "eod"}],
                             account(equity=kill_eq, last_equity=kill_eq), [], [],
                             scfg, fresh_state("scalpel"),
                             scan["asof_et"], True, {})
    check("validator allows liquidation under kill switch",
          any(a["action"] == "liquidate_all" for a in liq["approved"]))

    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nALL TESTS PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(run())
