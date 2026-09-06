"""Wiring test for the live cycle with the core sleeve: fake broker, fake data, no network.

Checks the one property that keeps the account off margin: when an entry is approved
while the cash is parked in SPY, the SPY sell is placed and filled BEFORE the bracket
buy, the sleeve position never reaches reconcile/validate, and the journal records it.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.common as cc
import run_cycle


class FakeBroker:
    def __init__(self):
        self.calls = []
        self.core_mv = 4940.0

    def account(self):
        return {"equity": 5000.0, "cash": 60.0, "last_equity": 5000.0,
                "buying_power": 60.0, "status": "ACTIVE"}

    def positions(self):
        return [{"symbol": "SPY", "qty": 6.4, "avg_entry": 770.0, "market_value": self.core_mv,
                 "unrealized_pl": 0.0, "current_price": 771.0}]

    def open_orders(self):
        return []

    def submit_notional_market(self, symbol, notional, side):
        self.calls.append(("notional", symbol, side, round(notional, 2)))
        if side == "sell":
            self.core_mv -= notional
        return {"id": f"o{len(self.calls)}", "symbol": symbol, "side": side,
                "notional": round(notional, 2), "status": "OrderStatus.ACCEPTED"}

    def order_status(self, order_id):
        return {"id": order_id, "status": "OrderStatus.FILLED", "filled_qty": 1.0,
                "filled_avg_price": 771.0}

    def submit_bracket_buy(self, symbol, qty, limit_price, stop_price, target_price, tif="day"):
        self.calls.append(("bracket_buy", symbol, qty))
        return {"id": f"b{len(self.calls)}", "symbol": symbol, "qty": qty, "status": "accepted"}

    def close_position(self, symbol):
        self.calls.append(("close", symbol))
        return {"symbol": symbol, "closed": True}

    def replace_stop(self, symbol, new_stop):
        return {"ok": True}


class FakeData:
    def market_open(self):
        return True

    def last_trades(self, syms):
        return {s: 420.0 if s == "MSFT" else 500.0 for s in syms}


def _scan():
    def sym(close, sma50, sma200, rsi2=50.0, pct_hi=5.0):
        return {"close": close, "last_bar_date": "2026-09-04", "sma50": sma50, "sma200": sma200,
                "ema20": close * 0.99, "rsi2": rsi2, "atr14": close * 0.02,
                "avg_dollar_vol_20d": 2e8, "pct_below_52wk_high": pct_hi,
                "low_today": close * 0.99, "avg_vol_20d": 5e6, "is_etf": False, "session": None}
    return {"mode": "daily", "asof_et": cc.now_et().isoformat(), "benchmark": "SPY",
            "universe_size": 2, "scanned": 2,
            "symbols": {"SPY": sym(500, 480, 450, rsi2=3.0),      # regime open; also a "setup"
                        "MSFT": sym(420, 410, 380, rsi2=4.0)}}    # pullback setup


def test_cycle_sells_core_before_bracket_buy(tmp_path, monkeypatch):
    real_root = cc.ROOT
    (tmp_path / "config").mkdir()
    for f in ("glider.yaml", "universe.yaml"):
        shutil.copy(real_root / "config" / f, tmp_path / "config" / f)
    monkeypatch.setattr(cc, "ROOT", tmp_path)          # journal/, state/ and config/ go to tmp
    broker = FakeBroker()
    monkeypatch.setattr(run_cycle, "make_broker", lambda cfg: broker)
    monkeypatch.setattr(run_cycle, "MarketData", lambda: FakeData())
    monkeypatch.setattr(run_cycle.scanner, "scan", lambda data, cfg, mode: _scan())
    monkeypatch.setattr(sys, "argv", ["run_cycle.py", "--bot", "glider"])

    assert run_cycle.main() == 0

    kinds = [c[0] for c in broker.calls]
    assert "bracket_buy" in kinds, broker.calls
    first_sell = next(i for i, c in enumerate(broker.calls) if c[0] == "notional" and c[2] == "sell")
    first_buy = kinds.index("bracket_buy")
    assert first_sell < first_buy, broker.calls                 # cash freed before the entry
    assert all(c[1] != "SPY" for c in broker.calls if c[0] == "bracket_buy")  # core never a setup

    run_dir = next(p for p in (tmp_path / "journal").iterdir() if p.name.startswith("glider_"))
    import json
    core = json.loads((run_dir / "core.json").read_text(encoding="utf-8"))
    assert core["plan"]["action"] == "sell" and core["orders"][0]["filled"]
    drift = run_dir / "reconcile_drift.json"
    assert not drift.exists()                                   # the sleeve was stripped, not "adopted"
    ledger = json.loads((tmp_path / "state" / "glider" / "ledger.json").read_text(encoding="utf-8"))
    assert "SPY" not in ledger and "MSFT" in ledger
