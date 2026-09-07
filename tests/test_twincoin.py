"""TWINCOIN engine tests with a fake broker and synthetic bars. No network."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twincoin import load_config  # noqa: E402
from twincoin.engine import Engine, new_state  # noqa: E402
from twincoin.strategy import indicators_4h, indicators_daily, vote  # noqa: E402

SYMS = ["BTC/USD", "ETH/USD"]


# ----------------------------------------------------------------- reference copy of the backtest's vote
def _ref_vote(r, h):
    b = s = 0
    if r.close > r.ema20 and h.e20 > h.e50: b += 1
    elif r.close < r.ema20 and h.e20 < h.e50: s += 1
    if 55 <= r.rsi <= 75: b += 1
    elif r.rsi < 45: s += 1
    if r.macd > r.sig: b += 1
    else: s += 1
    if r.close > r.ema50 and r.ema50 > r.ema50_5: b += 1
    elif r.close < r.ema50 and r.ema50 < r.ema50_5: s += 1
    if r.close > r.ema200 and r.ema50 > r.ema200: b += 1
    elif r.close < r.ema200 and r.ema50 < r.ema200: s += 1
    if r.obv - r.obv20 > 0: b += 1
    else: s += 1
    return b, s


def synth_daily(n=400, seed=1, drift=0.002, start="2025-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    ret = rng.normal(drift, 0.02, n)
    close = 30000 * np.exp(np.cumsum(ret))
    op = np.roll(close, 1); op[0] = close[0]
    hi = np.maximum(op, close) * (1 + rng.uniform(0, 0.01, n)); lo = np.minimum(op, close) * (1 - rng.uniform(0, 0.01, n))
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close, "volume": vol}, index=idx)


def synth_h4(daily: pd.DataFrame, seed=2):
    rng = np.random.default_rng(seed)
    rows = []
    for day, r in daily.iterrows():
        path = np.linspace(r.open, r.close, 7)[1:]
        for k in range(6):
            c = path[k] * (1 + rng.normal(0, 0.002))
            o = path[k - 1] if k else r.open
            rows.append({"ts": day + timedelta(hours=4 * k), "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c, "volume": r.volume / 6})
    return pd.DataFrame(rows).set_index("ts")


def test_vote_matches_backtest_reference():
    d = indicators_daily(synth_daily())
    h = indicators_4h(synth_h4(synth_daily()))
    for day in d.index[250:]:
        t = day + timedelta(hours=20)
        b, s, _ = vote(d.loc[day], h.loc[t])
        assert (b, s) == _ref_vote(d.loc[day], h.loc[t])


# ----------------------------------------------------------------- fake broker
class FakeBroker:
    def __init__(self, prices: dict[str, float], equity=100000.0):
        self.prices, self.equity = dict(prices), equity
        self.pos: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.n = 0

    def account(self):
        return {"equity": self.equity + sum(p["qty"] * self.prices[s] - p["qty"] * p["avg_entry_price"] for s, p in self.pos.items())}

    def positions(self):
        return {s: {"qty": p["qty"], "qty_available": p["qty"], "avg_entry_price": p["avg_entry_price"], "current_price": self.prices[s],
                    "market_value": p["qty"] * self.prices[s]} for s, p in self.pos.items() if p["qty"] > 0}

    def submit(self, symbol, side, qty, order_type, limit_price=None, stop_price=None, client_order_id=None):
        self.n += 1
        oid = f"o{self.n}"
        o = {"id": oid, "symbol": symbol, "side": side, "qty": qty, "type": order_type, "limit_price": limit_price, "stop_price": stop_price, "status": "new"}
        if order_type == "market":
            px = self.prices[symbol]
            o.update(status="filled", filled_avg_price=px, filled_qty=qty, filled_at="2025-06-01T00:00:00+00:00")
            if side == "buy":
                p = self.pos.setdefault(symbol, {"qty": 0.0, "avg_entry_price": px})
                p["avg_entry_price"] = (p["avg_entry_price"] * p["qty"] + px * qty) / (p["qty"] + qty) if p["qty"] else px
                p["qty"] += qty
            else:
                self.pos[symbol]["qty"] -= qty
                self.equity += qty * (px - self.pos[symbol]["avg_entry_price"])
                if self.pos[symbol]["qty"] <= 1e-12:
                    del self.pos[symbol]
        self.orders[oid] = o
        return o

    def fill_resting(self, oid, px):
        o = self.orders[oid]
        o.update(status="filled", filled_avg_price=px, filled_qty=o["qty"], filled_at="2025-06-02T00:00:00+00:00")
        self.pos[o["symbol"]]["qty"] -= o["qty"]
        self.equity += o["qty"] * (px - self.pos[o["symbol"]]["avg_entry_price"])
        if self.pos[o["symbol"]]["qty"] <= 1e-12:
            del self.pos[o["symbol"]]

    def get_order(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        if self.orders[oid]["status"] == "new":
            self.orders[oid]["status"] = "canceled"

    def cancel_symbol_orders(self, symbol):
        n = 0
        for o in self.orders.values():
            if o["symbol"] == symbol and o["status"] == "new":
                o["status"] = "canceled"; n += 1
        return n

    def open_orders(self, symbol=None):
        return [o for o in self.orders.values() if o["status"] == "new" and (symbol is None or o["symbol"] == symbol)]

    def wait_fill(self, oid):
        return self.orders[oid]


class MemJournal:
    def __init__(self):
        self.rows, self.trades = [], []

    def write(self, event, **p):
        self.rows.append((event, p))

    def trade(self, row):
        self.trades.append(row)

    def events(self, name):
        return [p for e, p in self.rows if e == name]


def make_market(n=400, drift=0.004, seed=3):
    """Strong uptrend so the vote reaches 5 of 6 by the end."""
    daily = {s: synth_daily(n, seed + i, drift) for i, s in enumerate(SYMS)}
    h4 = {s: synth_h4(daily[s], seed + 10 + i) for i, s in enumerate(SYMS)}
    return daily, h4


def cut(daily, h4, day_idx):
    """Bars visible at the close of daily bar day_idx (t = last 4H bar of that day)."""
    d = {s: daily[s].iloc[: day_idx + 1] for s in SYMS}
    t = daily[SYMS[0]].index[day_idx] + timedelta(hours=20)
    h = {s: h4[s][h4[s].index <= t] for s in SYMS}
    return d, h, t


@pytest.fixture
def cfg():
    c = load_config()
    c["risk"]["weekend_filter"] = False   # synthetic calendar; weekend tested separately
    return c


def _find_signal_day(cfg, daily, h4, want=5):
    for i in range(260, len(daily[SYMS[0]])):
        d, h, t = cut(daily, h4, i)
        D = indicators_daily(d[SYMS[0]]); H = indicators_4h(h[SYMS[0]])
        b, s, _ = vote(D.iloc[-1], H.loc[t])
        r = D.iloc[-1]
        if b >= want and r.close > r.ema200 and r.atr / r.close <= cfg["universe"]["vol_gate"][SYMS[0]]:
            return i
    pytest.skip("no 5-of-6 day in synthetic data")


def test_entry_sizing_and_resting_orders(cfg):
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    d, h, t = cut(daily, h4, i)
    px = {s: float(h[s]["close"].iloc[-1]) for s in SYMS}
    b = FakeBroker(px)
    st = new_state(cfg, b.account()["equity"], t)
    j = MemJournal()
    Engine(cfg, b, st, j).cycle(d, h, t + timedelta(hours=4))
    assert SYMS[0] in st["positions"]
    p = st["positions"][SYMS[0]]
    atr = float(indicators_daily(d[SYMS[0]])["atr"].iloc[-1])
    assert abs(p["risk_px"] - 2 * atr) < 1e-9
    # 2% of the $500 ledger, capped by the 42% sleeve
    assert p["risk_usd"] <= 500 * 0.02 + 1e-6
    assert p["cost"] <= 500 * 0.42 + 1e-6
    assert p["floor"] == pytest.approx(p["entry"] - p["risk_px"])
    resting = b.open_orders(SYMS[0])
    kinds = sorted(o["type"] for o in resting)
    assert kinds == ["limit", "stop_limit"]
    lim = [o for o in resting if o["type"] == "limit"][0]; back = [o for o in resting if o["type"] == "stop_limit"][0]
    assert lim["limit_price"] == pytest.approx(p["entry"] + 2 * p["risk_px"])
    assert back["stop_price"] == pytest.approx(p["floor"] * 0.97)
    assert lim["qty"] + back["qty"] <= p["qty"] + 1e-9
    assert st["last_bar"] == t.isoformat()


def test_idempotent_same_bar(cfg):
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    d, h, t = cut(daily, h4, i)
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS})
    st = new_state(cfg, b.account()["equity"], t); j = MemJournal()
    e = Engine(cfg, b, st, j)
    e.cycle(d, h, t + timedelta(hours=4)); n = b.n
    assert e.cycle(d, h, t + timedelta(hours=4)) == "noop"
    assert b.n == n


def test_weekend_blocks_entry(cfg):
    cfg["risk"]["weekend_filter"] = True
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    # shift the calendar so the daily close lands on a Saturday 00:00 UTC
    day = daily[SYMS[0]].index[i]
    shift = (5 - (day + timedelta(days=1)).weekday()) % 7
    daily = {s: daily[s].shift(shift, freq="D") for s in SYMS}; h4 = {s: h4[s].shift(shift, freq="D") for s in SYMS}
    d, h, t = cut(daily, h4, i)
    assert (t + timedelta(hours=4)).weekday() == 5
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS}); st = new_state(cfg, 100000, t); j = MemJournal()
    Engine(cfg, b, st, j).cycle(d, h, t + timedelta(hours=4))
    assert not st["positions"]
    assert any(p.get("why") == "weekend" for p in j.events("entry_blocked"))


def test_floor_exit_and_cooldown(cfg):
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    d, h, t = cut(daily, h4, i)
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS}); st = new_state(cfg, 100000, t); j = MemJournal()
    Engine(cfg, b, st, j).cycle(d, h, t + timedelta(hours=4))
    p = st["positions"][SYMS[0]]
    # next 4H bar closes below the floor
    t2 = t + timedelta(hours=4)
    crash = p["floor"] * 0.99
    for s in SYMS:
        c = crash if s == SYMS[0] else float(h[s]["close"].iloc[-1])
        h[s] = pd.concat([h[s], pd.DataFrame([{"open": c * 1.01, "high": c * 1.02, "low": c * 0.99, "close": c, "volume": 1}], index=[t2])])
    b.prices[SYMS[0]] = crash
    Engine(cfg, b, st, j).cycle(d, h, t2 + timedelta(hours=4))
    assert SYMS[0] not in st["positions"]
    assert j.trades and j.trades[-1]["reason"] == "floor" and j.trades[-1]["pnl"] < 0
    assert st["cooldown_until"].get(SYMS[0])
    assert not b.open_orders(SYMS[0])


def test_partial_fill_ratchets_floor_and_replaces_backstop(cfg):
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    d, h, t = cut(daily, h4, i)
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS}); st = new_state(cfg, 100000, t); j = MemJournal()
    Engine(cfg, b, st, j).cycle(d, h, t + timedelta(hours=4))
    p = st["positions"][SYMS[0]]
    old_back = p["backstop_order_id"]
    b.fill_resting(p["partial_order_id"], p["partial_px"])
    b.prices[SYMS[0]] = p["partial_px"]
    t2 = t + timedelta(hours=4)
    for s in SYMS:
        c = float(h[s]["close"].iloc[-1]) if s != SYMS[0] else p["partial_px"]
        h[s] = pd.concat([h[s], pd.DataFrame([{"open": c, "high": c, "low": c, "close": c, "volume": 1}], index=[t2])])
    Engine(cfg, b, st, j).cycle(d, h, t2 + timedelta(hours=4))
    p = st["positions"][SYMS[0]]
    assert p["partial_done"] and p["floor"] >= p["entry"] + p["risk_px"] - 1e-9
    assert p["backstop_order_id"] != old_back and b.orders[old_back]["status"] == "canceled"
    assert b.orders[p["backstop_order_id"]]["qty"] == pytest.approx(p["qty"], rel=1e-4)


def test_kill_switch_flattens_and_halts(cfg):
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    d, h, t = cut(daily, h4, i)
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS}); st = new_state(cfg, 100000, t); j = MemJournal()
    Engine(cfg, b, st, j).cycle(d, h, t + timedelta(hours=4))
    assert st["positions"]
    b.equity -= 500 * 0.30   # ledger drops 30% (account loses $150)
    t2 = t + timedelta(hours=4)
    for s in SYMS:
        c = float(h[s]["close"].iloc[-1])
        h[s] = pd.concat([h[s], pd.DataFrame([{"open": c, "high": c, "low": c, "close": c, "volume": 1}], index=[t2])])
    Engine(cfg, b, st, j).cycle(d, h, t2 + timedelta(hours=4))
    assert not st["positions"] and st["halt_until"] and st["kill_mode_left"] == 10
    assert j.events("KILL_SWITCH")


def test_orphan_adopted(cfg):
    daily, h4 = make_market()
    d, h, t = cut(daily, h4, 300)
    px = {s: float(h[s]["close"].iloc[-1]) for s in SYMS}
    b = FakeBroker(px)
    b.pos[SYMS[1]] = {"qty": 0.05, "avg_entry_price": px[SYMS[1]] * 0.98}
    st = new_state(cfg, 100000, t); j = MemJournal()
    Engine(cfg, b, st, j).cycle(d, h, t + timedelta(hours=4))
    assert SYMS[1] in st["positions"] and st["positions"][SYMS[1]].get("adopted")
    assert len(b.open_orders(SYMS[1])) == 2


def test_dry_run_touches_nothing(cfg):
    daily, h4 = make_market()
    i = _find_signal_day(cfg, daily, h4)
    d, h, t = cut(daily, h4, i)
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS}); st = new_state(cfg, 100000, t); j = MemJournal()
    Engine(cfg, b, st, j, dry_run=True).cycle(d, h, t + timedelta(hours=4))
    assert b.n == 0 and not st["positions"] and j.events("would_enter")


def test_order_sanity_rejects_oversize(cfg):
    daily, h4 = make_market()
    d, h, t = cut(daily, h4, 300)
    b = FakeBroker({s: float(h[s]["close"].iloc[-1]) for s in SYMS}); st = new_state(cfg, 100000, t); j = MemJournal()
    e = Engine(cfg, b, st, j)
    with pytest.raises(RuntimeError):
        e._submit(symbol=SYMS[0], side="buy", qty=1.0, order_type="market", ref_price=50000.0)
    with pytest.raises(RuntimeError):
        e._submit(symbol="DOGE/USD", side="buy", qty=1.0, order_type="market", ref_price=1.0)
