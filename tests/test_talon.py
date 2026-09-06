"""TALON offline test suite — no network anywhere in here.

Run:  pytest tests/test_talon.py -q
"""
from __future__ import annotations

import collections
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from talon import load_config  # noqa: E402
from talon import strategies as S  # noqa: E402
from talon.floor import AccountFloor, PositionFloor  # noqa: E402


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def rising_frame(n: int = 400, step: float = 1.0, start: float = 100.0) -> pd.DataFrame:
    """Strictly rising bars: every close is a new high."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = start + step * np.arange(n, dtype=float)
    return pd.DataFrame(
        {"open": close - step / 2, "high": close, "low": close - step, "close": close, "volume": 1.0},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Step 3 — strategies
# ---------------------------------------------------------------------------

def test_indicator_shapes_and_bounds():
    df = rising_frame(300)
    assert len(S.sma(df["close"], 20)) == len(df)
    assert S.sma(df["close"], 20).isna().sum() == 19
    assert S.ema(df["close"], 21).isna().sum() == 20
    a = S.atr(df, 14)
    assert a.isna().sum() == 13  # TR is defined from bar 0 (high-low); Wilder needs 14 of them
    r = S.rsi(df["close"], 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert r.iloc[-1] == pytest.approx(100.0)  # no losses ever -> RSI 100
    z = S.zscore(df["close"], 20).dropna()
    assert np.isfinite(z).all()
    v = S.realised_vol(df["close"], 30, 365).dropna()
    assert (v >= 0).all()


def test_every_strategy_returns_aligned_01_series(cfg):
    df = rising_frame(400)
    for name, fn in S.STRATEGIES.items():
        sig = fn(df, cfg["strategies"][name])
        assert isinstance(sig, pd.Series)
        assert sig.index.equals(df.index)
        assert set(sig.unique()) <= {0, 1}


def test_checkpoint_rising_series_no_signal_before_slowest_sma(cfg):
    """CHECKPOINT (step 3): a strictly rising series produces zero signals before the
    slowest SMA is defined — for every strategy individually and for the ensemble."""
    scfg = cfg["strategies"]
    df = rising_frame(400)
    slowest_sma = max(scfg["donchian_breakout"]["trend_sma"], scfg["dip_reversion"]["trend_sma"])

    # a 100-bar SMA is first defined on index 99 -> nothing may fire on indices 0..98
    don = S.donchian_breakout(df, scfg["donchian_breakout"])
    assert don.iloc[:slowest_sma - 1].sum() == 0
    assert don.iloc[slowest_sma - 1:].sum() > 0  # ...and it DOES fire once defined

    ema_warm = scfg["ema_trend"]["slow"] + scfg["ema_trend"]["slope_bars"]
    et = S.ema_trend(df, scfg["ema_trend"])
    assert et.iloc[:ema_warm - 1].sum() == 0
    assert et.iloc[ema_warm - 1:].sum() > 0

    frame = S.signal_frame(df, scfg)
    warm = S.warmup_bars(scfg)
    assert warm >= slowest_sma
    assert frame.iloc[:slowest_sma - 1].values.sum() == 0  # the checkpoint itself
    assert frame.iloc[:warm - 1].values.sum() == 0         # signal_frame masks until every window is full
    score = S.ensemble_score(frame, scfg)
    assert (score.iloc[:warm - 1] == 0).all()
    assert ((score >= 0) & (score <= 1)).all()


def test_donchian_uses_shifted_level(cfg):
    """With shift(1) a bar whose close == high and is a new high breaks out. Without
    the shift, close > max(highs including itself) could never be true."""
    scfg = cfg["strategies"]["donchian_breakout"]
    df = rising_frame(400)
    sig = S.donchian_breakout(df, scfg)
    warm = max(scfg["entry_lookback"] + 1, scfg["trend_sma"])
    assert (sig.iloc[warm:] == 1).all()


def test_dip_reversion_requires_uptrend(cfg):
    scfg = cfg["strategies"]["dip_reversion"]
    # a steady downtrend with a sharp dip: RSI/z-score would fire, the trend filter must veto
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = 200.0 - 0.3 * np.arange(n)
    close[-5:] -= 15.0
    df = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1.0}, index=idx)
    assert S.dip_reversion(df, scfg).sum() == 0


def test_weight_zero_disables_strategy(cfg):
    scfg = copy.deepcopy(cfg["strategies"])
    scfg["dip_reversion"]["weight"] = 0
    frame = S.signal_frame(rising_frame(300), scfg)
    assert "dip_reversion" not in frame.columns
    assert "ema_trend" in frame.columns


def test_regime_on_series(cfg):
    df = rising_frame(400)
    reg = S.regime_on(df, cfg)
    n = cfg["regime"]["sma_days"]
    assert reg.dtype == bool
    assert not reg.iloc[:n - 1].any()
    assert reg.iloc[n:].all()


# ---------------------------------------------------------------------------
# Step 4 — the rising floor
# ---------------------------------------------------------------------------

ENTRY = 100.0
ATR = 2.5  # r_unit = 2 * 2.5 = 5.0 (> 0.5% floor)


def _pf(cfg) -> PositionFloor:
    return PositionFloor.open("BTC/USD", ENTRY, ATR, cfg)


def test_floor_starts_at_entry_minus_atr_mult(cfg):
    p = cfg["rising_floor"]["position"]
    f = _pf(cfg)
    assert f.r_unit == pytest.approx(ATR * p["initial_atr_mult"])
    assert f.floor == pytest.approx(ENTRY - p["initial_atr_mult"] * ATR)
    assert f.high_water == ENTRY
    assert f.stage == "basement"
    assert f.cost_pad == pytest.approx(ENTRY * (cfg["costs"]["fee_bps"] + cfg["costs"]["slippage_bps"]) / 10_000)


def test_floor_min_r_pct_guards_tiny_atr(cfg):
    p = cfg["rising_floor"]["position"]
    f = PositionFloor.open("X/USD", ENTRY, 0.0001, cfg)
    assert f.r_unit == pytest.approx(ENTRY * p["min_r_pct"])


def test_floor_never_lowers_on_descending_prices(cfg):
    f = _pf(cfg)
    ru = f.r_unit
    for r in np.linspace(0, 8, 41):
        f.update(ENTRY + r * ru, cfg)
    peak_floor = f.floor
    assert peak_floor > ENTRY
    for px in np.linspace(ENTRY + 8 * ru, ENTRY - 3 * ru, 60):
        before = f.floor
        f.update(px, cfg)
        assert f.floor >= before
        assert f.floor == peak_floor


def test_floor_breakeven_at_one_r(cfg):
    f = _pf(cfg)
    f.update(ENTRY + 1.0 * f.r_unit, cfg)
    assert f.floor >= ENTRY
    assert f.stage == "breakeven"
    assert f.r_multiple(ENTRY + f.r_unit) == pytest.approx(1.0)


def test_floor_ratchet_steps(cfg):
    f = _pf(cfg)
    ru = f.r_unit
    f.update(ENTRY + 2 * ru, cfg)
    assert f.floor >= ENTRY + 1 * ru
    assert f.stage == "locked_1R"
    f.update(ENTRY + 3 * ru, cfg)
    assert f.floor >= ENTRY + 2 * ru
    assert f.stage == "locked_2R"
    f.update(ENTRY + 5 * ru, cfg)
    assert f.floor >= ENTRY + 3.5 * ru
    assert f.stage == "locked_3.5R"


def test_floor_trail_far_past_last_step(cfg):
    p = cfg["rising_floor"]["position"]
    f = _pf(cfg)
    hw = ENTRY + 40 * f.r_unit  # far beyond the 5R step: trail dominates the ratchet
    f.update(hw, cfg)
    assert f.floor == pytest.approx(hw * (1 - p["trail_pct"]))
    assert f.stage == "trailing"


def test_floor_trail_not_active_in_basement(cfg):
    f = _pf(cfg)
    f.update(ENTRY + 0.5 * f.r_unit, cfg)  # below breakeven: still basement, trail must not apply
    assert f.stage == "basement"
    assert f.floor == pytest.approx(ENTRY - f.r_unit)


def test_floor_breached_and_roundtrip(cfg):
    f = _pf(cfg)
    f.update(ENTRY + 2.2 * f.r_unit, cfg)
    assert f.breached(f.floor)
    assert f.breached(f.floor - 0.01)
    assert not f.breached(f.floor + 0.01)
    g = PositionFloor.from_dict(json.loads(json.dumps(f.to_dict())))
    assert g.floor == f.floor and g.stage == f.stage and g.high_water == f.high_water
    assert g.entry == f.entry and g.r_unit == f.r_unit and g.cost_pad == f.cost_pad


def test_account_floor_nothing_at_nine_percent(cfg):
    af = AccountFloor(basis=10_000)
    assert af.update(10_900, cfg, "t0") == 0.0
    assert af.locked == 0.0
    assert af.basis == 10_000


def test_account_floor_sweeps_forty_percent_at_ten_percent(cfg):
    af = AccountFloor(basis=10_000)
    swept = af.update(11_000, cfg, "t0")
    assert swept == pytest.approx(400.0)  # 40% of the $1,000 gain
    assert af.locked == pytest.approx(400.0)
    assert af.basis == pytest.approx(10_600.0)
    assert af.tradeable(11_000) == pytest.approx(10_600.0)
    assert len(af.history) == 1


def test_account_floor_whole_steps_only(cfg):
    af = AccountFloor(basis=10_000)
    swept = af.update(11_240, cfg, "t0")  # +12.4% -> only the 10% step is eligible
    assert swept == pytest.approx(400.0)
    assert af.basis == pytest.approx(11_240 - 400)
    assert af.update(11_300, cfg, "t1") == 0.0  # no nibbling on the next tick up


def test_account_floor_locked_never_decreases(cfg):
    af = AccountFloor(basis=10_000)
    rng = np.random.default_rng(1)
    eq = 10_000.0
    prev = 0.0
    for i in range(400):
        eq *= float(np.exp(rng.normal(0.0005, 0.03)))
        af.update(eq, cfg, f"t{i}")
        assert af.locked >= prev
        prev = af.locked
    # a drawdown sequence after a big run
    for eq2 in np.linspace(eq, eq * 0.5, 50):
        af.update(eq2, cfg, "dd")
        assert af.locked >= prev
    assert af.tradeable(eq * 0.5) == max(eq * 0.5 - af.locked, 0.0)


def test_account_floor_has_no_release_method_and_refuses_decrease(cfg):
    af = AccountFloor(basis=10_000)
    af.update(11_000, cfg, "t0")
    with pytest.raises(ValueError):
        af.locked = af.locked - 1.0
    names = {n.lower() for n in dir(AccountFloor) if not n.startswith("_")}
    for forbidden in ("release", "unlock", "reset", "withdraw", "decrease"):
        assert not any(forbidden in n for n in names)
    g = AccountFloor.from_dict(json.loads(json.dumps(af.to_dict())))
    assert g.locked == af.locked and g.basis == af.basis and g.high_water == af.high_water


# ---------------------------------------------------------------------------
# Step 5 — planner
# ---------------------------------------------------------------------------

from talon.planner import plan, kill_switch_state, compute_features  # noqa: E402
from talon import broker as B  # noqa: E402
from talon import data as D  # noqa: E402


def panel(n: int = 400, syms=("A/USD", "B/USD", "C/USD"), steps=(1.0, 0.8, 0.5)) -> dict:
    return {s: rising_frame(n, step=st, start=100.0) for s, st in zip(syms, steps)}


def test_planner_regime_off_returns_nothing(cfg):
    bars = panel()
    bench = rising_frame(400)
    bench_off = bench.copy()
    bench_off["close"] = bench_off["close"].values[::-1]  # falling benchmark
    res = plan(bars, bench_off, 10_000, cfg, [], i=None)
    assert res["regime_on"] is False
    assert res["targets"] == {}
    assert res["candidates"] == []
    assert any("Regime OFF" in n for n in res["notes"])


def test_planner_sizing_and_caps(cfg):
    bars = panel()
    bench = rising_frame(400)
    equity = 10_000.0
    res = plan(bars, bench, equity, cfg, [], i=None)
    assert res["regime_on"] is True
    assert 0 < len(res["targets"]) <= cfg["ensemble"]["max_positions"]
    rcfg = cfg["risk"]
    for sym, t in res["targets"].items():
        assert t["notional"] <= equity * rcfg["max_position_weight"] + 1e-6
        assert t["notional"] >= rcfg["min_order_notional"]
        assert set(t["reasons"]) <= set(cfg["strategies"])
        assert t["r_unit"] >= t["price"] * cfg["rising_floor"]["position"]["min_r_pct"]
    gross = sum(t["notional"] for t in res["targets"].values())
    assert gross <= equity * rcfg["max_gross_exposure"] + 1e-6


def test_planner_qty_is_risk_over_r_unit_when_uncapped(cfg):
    c = copy.deepcopy(cfg)
    c["risk"]["inverse_vol_sizing"] = False
    c["risk"]["max_position_weight"] = 10.0   # no cap
    c["risk"]["max_gross_exposure"] = 100.0   # no cap
    bars = {"A/USD": rising_frame(400)}
    res = plan(bars, bars["A/USD"], 10_000.0, c, [], i=None)
    t = res["targets"]["A/USD"]
    assert t["qty"] == pytest.approx(10_000.0 * c["risk"]["risk_per_trade_pct"] / t["r_unit"])


def test_planner_gross_cap_haircuts_pro_rata(cfg):
    c = copy.deepcopy(cfg)
    c["risk"]["max_position_weight"] = 0.5
    c["risk"]["max_gross_exposure"] = 0.30
    c["risk"]["risk_per_trade_pct"] = 0.20  # oversize on purpose
    bars = panel()
    res = plan(bars, rising_frame(400), 10_000.0, c, {}, i=None)
    assert len(res["targets"]) >= 2  # haircut, not drop-the-tail
    gross = sum(t["notional"] for t in res["targets"].values())
    assert gross == pytest.approx(3_000.0, rel=1e-3)
    # existing exposure counts against the cap
    res2 = plan(bars, rising_frame(400), 10_000.0, c, {"Z/USD": 2_500.0}, i=None)
    gross2 = sum(t["notional"] for t in res2["targets"].values())
    assert gross2 <= 500.0 + 0.01 * len(res2["targets"])  # notionals are rounded to cents


def test_planner_skips_open_symbols_and_respects_slots(cfg):
    bars = panel()
    bench = rising_frame(400)
    opened = {"A/USD": 1_000.0, "B/USD": 1_000.0, "C/USD": 1_000.0}
    res = plan(bars, bench, 10_000.0, cfg, opened, i=None)
    assert res["targets"] == {}
    c = copy.deepcopy(cfg)
    c["ensemble"]["max_positions"] = 2
    res = plan(bars, bench, 10_000.0, c, {"A/USD": 1_000.0}, i=None)
    assert "A/USD" not in res["targets"]
    assert len(res["targets"]) <= 1


def test_planner_backtest_index_matches_live_last_bar(cfg):
    bars = panel()
    bench = rising_frame(400)
    cache = {}
    live = plan(bars, bench, 10_000.0, cfg, [], i=None)
    bt = plan(bars, bench, 10_000.0, cfg, [], i=len(bench) - 1, feature_cache=cache)
    assert live["targets"].keys() == bt["targets"].keys()
    for s in live["targets"]:
        assert live["targets"][s]["notional"] == pytest.approx(bt["targets"][s]["notional"])
    assert set(cache) == set(bars)


def test_planner_inverse_vol_scale_mean_one_and_clipped(cfg):
    bars = panel(steps=(3.0, 1.0, 0.2))
    res = plan(bars, rising_frame(400), 10_000.0, cfg, [], i=None)
    scales = [t["vol_scale"] for t in res["targets"].values()]
    lo, hi = cfg["risk"]["inverse_vol_clip"]
    assert all(lo - 1e-9 <= s <= hi + 1e-9 for s in scales)


def test_kill_switch(cfg):
    assert kill_switch_state(10_000, 10_000, 10_000, cfg) == (False, "")
    halted, why = kill_switch_state(8_100, 10_000, 8_400, cfg)
    assert halted and "kill switch" in why
    halted, why = kill_switch_state(9_500, 10_000, 9_950, cfg)
    assert halted and "daily loss" in why
    halted, _ = kill_switch_state(9_700, 10_000, 9_900, cfg)
    assert not halted


# ---------------------------------------------------------------------------
# Step 6 — data + broker
# ---------------------------------------------------------------------------

def test_align_trims_to_intersection():
    a = rising_frame(10)
    b = rising_frame(10).iloc[3:]
    out = D.align({"A": a, "B": b})
    assert len(out["A"]) == len(out["B"]) == 7
    assert out["A"].index.equals(out["B"].index)


def test_drop_incomplete_bar():
    df = rising_frame(5)
    now = df.index[-1] + pd.Timedelta(hours=3)  # last daily bar still in progress
    out = D.drop_incomplete_bar({"A": df}, "1D", now=now.to_pydatetime())
    assert len(out["A"]) == 4
    later = df.index[-1] + pd.Timedelta(days=1, minutes=1)
    out2 = D.drop_incomplete_bar({"A": df}, "1D", now=later.to_pydatetime())
    assert len(out2["A"]) == 5


def test_timeframe_delta():
    assert D.timeframe_delta("1D") == pd.Timedelta(days=1)
    assert D.timeframe_delta("4H") == pd.Timedelta(hours=4)
    assert D.timeframe_delta("15Min") == pd.Timedelta(minutes=15)


def test_fetch_bars_paginates(monkeypatch, cfg):
    pages = [
        {"bars": {"BTC/USD": [{"t": "2024-01-01T06:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
         "next_page_token": "p2"},
        {"bars": {"BTC/USD": [{"t": "2024-01-02T06:00:00Z", "o": 1.5, "h": 2.5, "l": 1, "c": 2, "v": 11}]},
         "next_page_token": None},
    ]
    calls = []

    def fake_get(session, url, params, headers, dcfg):
        calls.append(dict(params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(D, "_get_with_retry", fake_get)
    out = D.fetch_bars(["BTC/USD"], "1D", 900, cfg=cfg)
    assert len(calls) == 2 and calls[1]["page_token"] == "p2"
    df = out["BTC/USD"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2 and df.index.tz is not None and df.index.is_monotonic_increasing


def test_broker_two_switch_guard(cfg):
    paper = copy.deepcopy(cfg); paper["meta"]["mode"] = "paper"
    live = copy.deepcopy(cfg); live["meta"]["mode"] = "live"
    on = {B.FORCE_LIVE_ENV: B.FORCE_LIVE_VALUE}
    assert B.resolve_base_url(paper, env={}) == B.PAPER_URL
    assert B.resolve_base_url(live, env=on) == B.LIVE_URL
    with pytest.raises(B.LiveGuardError):
        B.resolve_base_url(live, env={})          # config says live, env silent
    with pytest.raises(B.LiveGuardError):
        B.resolve_base_url(paper, env=on)         # env says live, config silent
    with pytest.raises(B.LiveGuardError):
        B.resolve_base_url(live, env={B.FORCE_LIVE_ENV: "yes"})  # wrong magic word


def test_broker_requires_credentials(monkeypatch, cfg):
    monkeypatch.delenv("TALON_API_KEY", raising=False)
    monkeypatch.delenv("TALON_API_SECRET", raising=False)
    monkeypatch.delenv(B.FORCE_LIVE_ENV, raising=False)
    with pytest.raises(B.CredentialsError):
        B.Alpaca(cfg)


def test_broker_position_map_reinserts_slash(monkeypatch, cfg):
    monkeypatch.setenv("TALON_API_KEY", "k")
    monkeypatch.setenv("TALON_API_SECRET", "s")
    monkeypatch.delenv(B.FORCE_LIVE_ENV, raising=False)
    br = B.Alpaca(cfg)
    assert br.is_paper
    monkeypatch.setattr(br, "_get", lambda path, params=None: [
        {"symbol": "BTCUSD", "qty": "0.1", "market_value": "5000", "avg_entry_price": "50000",
         "current_price": "50000", "unrealized_pl": "0"},
        {"symbol": "AAVEUSD", "qty": "1", "market_value": "100", "avg_entry_price": "100",
         "current_price": "100", "unrealized_pl": "0"},
    ])
    pm = br.position_map()
    assert set(pm) == {"BTC/USD", "AAVE/USD"}
    assert pm["BTC/USD"]["qty"] == 0.1
    assert B.with_slash("BTCUSD") == "BTC/USD"
    assert B.without_slash("BTC/USD") == "BTCUSD"


def test_broker_order_bodies(monkeypatch, cfg):
    monkeypatch.setenv("TALON_API_KEY", "k")
    monkeypatch.setenv("TALON_API_SECRET", "s")
    monkeypatch.delenv(B.FORCE_LIVE_ENV, raising=False)
    br = B.Alpaca(cfg)
    sent = {}
    monkeypatch.setattr(br, "_post", lambda path, body: sent.update({"path": path, **body}) or {"id": "x"})
    br.buy_notional("BTCUSD", 123.456)
    assert sent["symbol"] == "BTC/USD" and sent["notional"] == "123.46"
    assert sent["type"] == "market" and sent["time_in_force"] == "gtc" and sent["side"] == "buy"


# ---------------------------------------------------------------------------
# Step 7 — live cycle, driven with a fake broker and synthetic bars (no network)
# ---------------------------------------------------------------------------

import importlib.util  # noqa: E402

import run_talon as RT  # noqa: E402
import talon.planner as planner_mod  # noqa: E402
import talon.floor as floor_mod  # noqa: E402


def _load_backtest():
    spec = importlib.util.spec_from_file_location("talon_backtest", ROOT / "backtest" / "backtest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BT = _load_backtest()


class FakeBroker:
    is_paper = True

    def __init__(self, equity=10_000.0, positions=None, fail_on=()):
        self._equity = equity
        self._positions = dict(positions or {})
        self.fail_on = set(fail_on)
        self.orders: list = []
        self.closed: list = []

    def account(self):
        return {"equity": str(self._equity)}

    def position_map(self):
        return dict(self._positions)

    def buy_notional(self, sym, notional):
        if sym in self.fail_on:
            raise RuntimeError("simulated broker rejection")
        self.orders.append((sym, float(notional)))
        return {"id": f"o{len(self.orders)}"}

    def fill_price(self, order_id, wait_s, polls):
        return None

    def close_position(self, sym):
        self.closed.append(sym)
        self._positions.pop(sym, None)
        return {"id": f"c{len(self.closed)}"}


@pytest.fixture
def live(tmp_path, monkeypatch, cfg):
    """A config whose paths live in tmp, creds in env, bars + broker faked. Yields (cfg, install)."""
    c = copy.deepcopy(cfg)
    c["paths"] = {"state": str(tmp_path / "talon_state.json"), "gate": str(tmp_path / "gate.json"),
                  "journal_dir": str(tmp_path / "journal")}
    monkeypatch.setenv("TALON_API_KEY", "k")
    monkeypatch.setenv("TALON_API_SECRET", "s")
    monkeypatch.delenv(B.FORCE_LIVE_ENV, raising=False)
    syms = c["universe"]["symbols"]
    frames = {s: rising_frame(400, step=1.0 + 0.1 * k, start=100.0 * (k + 1)) for k, s in enumerate(syms)}
    monkeypatch.setattr(RT, "fetch_bars", lambda symbols, tf, lb, cfg=None: {s: frames[s] for s in symbols})

    def install(broker: FakeBroker):
        monkeypatch.setattr(RT, "Alpaca", lambda cfg: broker)
        return broker

    def grant():
        (tmp_path / "gate.json").write_text(json.dumps({"passed": True, "generated_utc": "t"}), encoding="utf-8")

    def journal_events():
        rows = []
        for p in sorted((tmp_path / "journal").glob("*.jsonl")):
            rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]
        return rows

    return c, install, grant, journal_events, tmp_path


def test_cycle_refuses_without_gate(live):
    c, install, grant, events, tmp = live
    install(FakeBroker())
    assert RT.run("cycle", c) == RT.EXIT_NO_GATE
    assert not (tmp / "talon_state.json").exists()
    (tmp / "gate.json").write_text(json.dumps({"passed": False, "reason": "nope"}), encoding="utf-8")
    assert RT.run("cycle", c) == RT.EXIT_NO_GATE


def test_cycle_places_entries_and_records_state(live):
    c, install, grant, events, tmp = live
    grant()
    br = install(FakeBroker())
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert 0 < len(br.orders) <= c["ensemble"]["max_positions"]
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert set(state["positions"]) == {s for s, _ in br.orders}
    for s, notional in br.orders:
        fl = PositionFloor.from_dict(state["positions"][s])
        assert fl.floor < fl.entry and fl.stage == "basement"
    kinds = [e["event"] for e in events()]
    assert "entry" in kinds and "snapshot" in kinds and "plan" in kinds
    snap = [e for e in events() if e["event"] == "snapshot"][-1]
    assert snap["open_count"] == len(br.orders) and snap["locked"] == 0.0


def test_cycle_survives_one_failed_order(live):
    c, install, grant, events, tmp = live
    grant()
    probe = install(FakeBroker())
    RT.run("cycle", c)
    victim = probe.orders[0][0]
    (tmp / "talon_state.json").unlink()
    br = install(FakeBroker(fail_on={victim}))
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert victim not in {s for s, _ in br.orders}
    assert len(br.orders) == len(probe.orders) - 1
    failed = [e for e in events() if e["event"] == "order_failed"]
    assert failed and failed[-1]["symbol"] == victim
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert victim not in state["positions"]


def test_cycle_adopts_orphan_and_exits_on_breach(live):
    c, install, grant, events, tmp = live
    grant()
    # entry far above the completed-bar close (~499): the adopted floor sits above the close
    br = install(FakeBroker(positions={"BTC/USD": {"qty": 1.0, "market_value": 5000.0, "avg_entry_price": 5000.0,
                                                    "current_price": 5000.0, "unrealized_pl": 0.0}}))
    assert RT.run("cycle", c) == RT.EXIT_OK
    kinds = [e["event"] for e in events()]
    assert "adopt" in kinds and "exit_signal" in kinds and "exit" in kinds
    sig = [e for e in events() if e["event"] == "exit_signal"][0]
    assert sig["price"] < 600 and sig["mark"] == 5000.0    # judged on the completed bar, not the live mark
    assert "BTC/USD" in br.closed
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert "BTC/USD" not in state["positions"]


def test_cycle_halts_and_flattens_on_kill_switch(live):
    c, install, grant, events, tmp = live
    grant()
    br = install(FakeBroker(positions={"ETH/USD": {"qty": 1.0, "market_value": 3000.0, "avg_entry_price": 2900.0,
                                                    "current_price": 3000.0, "unrealized_pl": 100.0}}))
    st = RT.new_state(10_000.0)
    st["equity_high_water"] = 20_000.0  # 50% under high-water -> kill switch
    (tmp / "talon_state.json").write_text(json.dumps(st), encoding="utf-8")
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert br.orders == []
    assert "ETH/USD" in br.closed
    halts = [e for e in events() if e["event"] == "halt"]
    assert halts and "kill switch" in halts[0]["reason"]
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert state["halted"] is True and state["positions"] == {}


def test_daily_loss_breaker_uses_day_start(live):
    c, install, grant, events, tmp = live
    grant()
    br = install(FakeBroker(equity=9_500.0))
    st = RT.new_state(10_000.0)
    st["day"]["start_equity"] = 10_000.0  # -5% on the day -> breaker
    (tmp / "talon_state.json").write_text(json.dumps(st), encoding="utf-8")
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert br.orders == []
    assert any("daily loss" in e.get("reason", "") for e in events() if e["event"] == "halt")


def test_account_floor_sweep_is_journaled(live):
    c, install, grant, events, tmp = live
    grant()
    install(FakeBroker(equity=11_000.0))
    st = RT.new_state(10_000.0)
    (tmp / "talon_state.json").write_text(json.dumps(st), encoding="utf-8")
    assert RT.run("cycle", c) == RT.EXIT_OK
    sweeps = [e for e in events() if e["event"] == "sweep"]
    assert sweeps and sweeps[0]["swept"] == pytest.approx(400.0)
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert state["account_floor"]["locked"] == pytest.approx(400.0)
    assert state["account_floor"]["basis"] == pytest.approx(10_600.0)
    plan_row = [e for e in events() if e["event"] == "plan"][-1]
    assert plan_row["tradeable"] == pytest.approx(10_600.0)  # sized on tradeable, not total


def test_pulse_writes_nothing(live):
    c, install, grant, events, tmp = live
    br = install(FakeBroker())
    assert RT.run("pulse", c) == RT.EXIT_OK
    assert br.orders == [] and br.closed == []
    assert not (tmp / "talon_state.json").exists()
    assert not (tmp / "journal").exists()


def test_flatten_closes_all_and_keeps_floor_state(live):
    c, install, grant, events, tmp = live
    br = install(FakeBroker(positions={"SOL/USD": {"qty": 2.0, "market_value": 300.0, "avg_entry_price": 140.0,
                                                    "current_price": 150.0, "unrealized_pl": 20.0}}))
    st = RT.new_state(10_000.0)
    st["account_floor"]["locked"] = 250.0
    (tmp / "talon_state.json").write_text(json.dumps(st), encoding="utf-8")
    assert RT.run("flatten", c) == RT.EXIT_OK
    assert br.closed == ["SOL/USD"]
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert state["account_floor"]["locked"] == 250.0


def test_run_talon_cli_exit_codes(tmp_path, monkeypatch):
    """CHECKPOINT (step 10): --mode cycle exits 2 with no gate.json (subprocess, real CLI)."""
    import yaml
    c = load_config()
    c["paths"] = {"state": str(tmp_path / "s.json"), "gate": str(tmp_path / "gate.json"),
                  "journal_dir": str(tmp_path / "journal")}
    cfg_path = tmp_path / "talon.yaml"
    cfg_path.write_text(yaml.safe_dump(c), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TALON_")}
    r = subprocess.run([sys.executable, str(ROOT / "run_talon.py"), "--mode", "cycle", "--config", str(cfg_path)],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING TO TRADE" in r.stdout
    r = subprocess.run([sys.executable, str(ROOT / "run_talon.py"), "--mode", "pulse", "--config", str(cfg_path)],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 1 and "credentials error" in r.stdout


# ---------------------------------------------------------------------------
# Step 8 — backtest: same planner, same floor, no look-ahead, gate logic
# ---------------------------------------------------------------------------

def test_backtest_imports_planner_and_floor_not_copies():
    assert BT.plan is planner_mod.plan
    assert BT.PositionFloor is floor_mod.PositionFloor
    assert BT.kill_switch_state is planner_mod.kill_switch_state
    src = (ROOT / "backtest" / "backtest.py").read_text(encoding="utf-8")
    for token in ("risk_per_trade_pct", "max_position_weight", "max_gross_exposure", "inverse_vol", "min_score", "top_k"):
        assert token not in src, f"backtest.py mentions {token}: sizing logic is leaking out of the planner"


def test_synthetic_engine_trades_and_sweeps(cfg):
    bars = D.align(BT.synthetic_bars(cfg))
    res = BT.run_strategy(bars, cfg)
    m = res["metrics"]
    assert m["trades"] > 0
    assert m["locked_reserve"] > 0
    assert "sharpe" in m and "max_drawdown_pct" in m and "avg_r" in m
    cost = (cfg["costs"]["fee_bps"] + cfg["costs"]["slippage_bps"]) / 10_000
    for t in res["trades"]:
        df = bars[t["symbol"]]
        # entries fill at the OPEN of the bar after the signal, cost applied adversely;
        # exit fills are covered per exit_check mode in test_backtest_intrabar_exit_fills
        assert t["entry"] == pytest.approx(df.loc[t["entry_ts"], "open"] * (1 + cost))
        assert pd.Timestamp(t["exit_ts"]) >= pd.Timestamp(t["entry_ts"])


def test_no_look_ahead_future_bars_do_not_change_plan(cfg):
    bars = D.align(BT.synthetic_bars(cfg, bars=500, seed=11))
    bench = bars[cfg["universe"]["benchmark"]]
    i = 320
    base = plan(bars, bench, 10_000.0, cfg, [], i=i)
    rng = np.random.default_rng(0)
    tampered = {}
    for s, df in bars.items():
        d = df.copy()
        mult = rng.uniform(0.5, 1.5, size=len(d) - i - 1)
        for col in ("open", "high", "low", "close"):
            d.iloc[i + 1:, d.columns.get_loc(col)] = d[col].iloc[i + 1:].to_numpy() * mult
        tampered[s] = d
    alt = plan(tampered, tampered[cfg["universe"]["benchmark"]], 10_000.0, cfg, [], i=i)
    assert base["regime_on"] == alt["regime_on"]
    assert base["targets"].keys() == alt["targets"].keys()
    for s in base["targets"]:
        assert base["targets"][s]["notional"] == pytest.approx(alt["targets"][s]["notional"])
        assert base["targets"][s]["score"] == pytest.approx(alt["targets"][s]["score"])


def test_backtest_controls_and_gate_logic(cfg):
    bars = D.align(BT.synthetic_bars(cfg, bars=500, seed=5))
    ew = BT.buy_and_hold(bars, cfg, {s: 1 / len(bars) for s in bars})
    btc = BT.buy_and_hold(bars, cfg, {cfg["universe"]["benchmark"]: 1.0})
    assert ew["curve"].iloc[0] == cfg["risk"]["starting_equity"]
    assert len(ew["curve"]) == len(bars[cfg["universe"]["benchmark"]]) - cfg["data"]["min_bars"] + 1

    def arm(**m):
        base = {"sharpe": 1.0, "max_drawdown_pct": 0.2, "total_return_pct": 0.5, "trades": 40, "years": 3.5}
        base.update(m)
        return {"metrics": base}

    controls = {"equal_weight": arm(sharpe=0.5), "btc_hold": arm(sharpe=0.7)}
    ok, reason, checks = BT.evaluate_gate(arm(), controls, cfg)
    assert ok and reason == "all checks passed"
    ok, reason, _ = BT.evaluate_gate(arm(sharpe=0.9), {"equal_weight": arm(sharpe=1.2), "btc_hold": arm()}, cfg)
    assert not ok and "beat equal_weight on sharpe" in reason  # 20% while the market makes 60% = lost
    for bad in (dict(sharpe=0.5), dict(max_drawdown_pct=0.5), dict(total_return_pct=-0.1), dict(trades=10), dict(years=2.0)):
        ok, _, _ = BT.evaluate_gate(arm(**bad), controls, cfg)
        assert not ok, bad


def test_synthetic_cli_never_writes_gate(tmp_path):
    import yaml
    c = load_config()
    c["paths"] = {"state": str(tmp_path / "s.json"), "gate": str(tmp_path / "gate.json"),
                  "journal_dir": str(tmp_path / "journal")}
    cfg_path = tmp_path / "talon.yaml"
    cfg_path.write_text(yaml.safe_dump(c), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "backtest" / "backtest.py"), "--synthetic", "--bars", "450",
                        "--config", str(cfg_path)], capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MEANINGLESS" in r.stdout
    assert not (tmp_path / "gate.json").exists()


# ---------------------------------------------------------------------------
# Improvements 1-5: sub-window gate, point-in-time universe, breaker action, intrabar exits
# ---------------------------------------------------------------------------

from talon.planner import halt_action, HALT_FLATTEN, HALT_BLOCK_ENTRIES  # noqa: E402


def test_align_point_in_time_keeps_union_calendar():
    a = rising_frame(10)
    b = rising_frame(10).iloc[3:]
    out = D.align({"A": a, "B": b}, mode="point_in_time")
    assert len(out["A"]) == len(out["B"]) == 10
    assert out["B"]["close"].isna().sum() == 3
    assert list(D.valid_bars(out["B"]).iloc[[2, 3, 9]]) == [0, 1, 7]
    with pytest.raises(ValueError):
        D.align({"A": a}, mode="bogus")


def test_planner_requires_own_history_under_pit(cfg):
    c = copy.deepcopy(cfg)
    c["data"]["min_bars"] = 120
    full = rising_frame(400)
    late = rising_frame(400).iloc[300:]            # listed late: only 100 valid bars
    bars = D.align({"A/USD": full, "B/USD": late}, mode="point_in_time")
    res = plan(bars, bars["A/USD"], 10_000.0, c, [], i=None)
    assert "A/USD" in res["targets"]
    assert "B/USD" not in [x["symbol"] for x in res["candidates"]]
    # and once it has min_bars of its own bars it is eligible
    late2 = rising_frame(400).iloc[200:]
    bars2 = D.align({"A/USD": full, "B/USD": late2}, mode="point_in_time")
    res2 = plan(bars2, bars2["A/USD"], 10_000.0, c, [], i=None)
    assert "B/USD" in [x["symbol"] for x in res2["candidates"]]


def test_gap_does_not_leak_stale_signals(cfg):
    """A name with a 400-bar hole must not be scored on the first bars after the hole."""
    c = copy.deepcopy(cfg)
    df = rising_frame(900)
    holed = df.copy()
    holed.iloc[300:700] = np.nan
    bars = D.align({"BTC/USD": df, "X/USD": holed}, mode="point_in_time")
    res = plan(bars, bars["BTC/USD"], 10_000.0, c, [], i=705)
    assert "X/USD" not in res["targets"]
    feats = compute_features(bars["X/USD"], c)
    assert feats["score"].iloc[700:760].fillna(0).sum() == 0


def test_halt_action(cfg):
    c = copy.deepcopy(cfg)
    c["risk"]["daily_breaker_action"] = HALT_BLOCK_ENTRIES
    assert halt_action("daily loss breaker: down 5%", c) == HALT_BLOCK_ENTRIES
    assert halt_action("kill switch: drawdown 20%", c) == HALT_FLATTEN
    c["risk"]["daily_breaker_action"] = HALT_FLATTEN
    assert halt_action("daily loss breaker: down 5%", c) == HALT_FLATTEN
    c["risk"]["daily_breaker_action"] = "nonsense"
    with pytest.raises(ValueError):
        halt_action("daily loss breaker: x", c)


def test_daily_breaker_block_entries_keeps_positions(live):
    c, install, grant, events, tmp = live
    c["risk"]["daily_breaker_action"] = HALT_BLOCK_ENTRIES
    grant()
    # entry just under the completed-bar close (~639) so the floor is NOT breached; only the breaker acts
    br = install(FakeBroker(equity=9_500.0, positions={"ETH/USD": {"qty": 1.0, "market_value": 640.0, "avg_entry_price": 600.0,
                                                                     "current_price": 640.0, "unrealized_pl": 40.0}}))
    st = RT.new_state(10_000.0)
    st["day"]["start_equity"] = 10_000.0
    (tmp / "talon_state.json").write_text(json.dumps(st), encoding="utf-8")
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert br.orders == [] and br.closed == []           # no new risk, no flatten
    state = json.loads((tmp / "talon_state.json").read_text(encoding="utf-8"))
    assert state["halted"] and "ETH/USD" in state["positions"]
    c["risk"]["daily_breaker_action"] = HALT_FLATTEN
    (tmp / "talon_state.json").write_text(json.dumps(st), encoding="utf-8")
    br2 = install(FakeBroker(equity=9_500.0, positions={"ETH/USD": {"qty": 1.0, "market_value": 640.0, "avg_entry_price": 600.0,
                                                                      "current_price": 640.0, "unrealized_pl": 40.0}}))
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert br2.closed == ["ETH/USD"]


def test_backtest_intrabar_exit_fills(cfg):
    """Floor set at the previous close; breached by the LOW -> fill at the floor, or at the
    open if it gapped through. Entries still fill at the next open."""
    c = copy.deepcopy(cfg)
    c["rising_floor"]["position"]["evaluate_on"] = "current_price"
    assert BT.exit_check_for(c) == "intrabar_low"
    bars = D.align(BT.synthetic_bars(c), mode="point_in_time")
    res = BT.run_strategy(bars, c)
    cost = (c["costs"]["fee_bps"] + c["costs"]["slippage_bps"]) / 10_000
    kinds = collections.Counter(t["reason"] for t in res["trades"])
    assert kinds["floor"] > 0
    for t in res["trades"]:
        df = bars[t["symbol"]]
        assert t["entry"] == pytest.approx(df.loc[t["entry_ts"], "open"] * (1 + cost))
        if t["reason"] == "floor":
            assert t["exit"] == pytest.approx(t["floor"] * (1 - cost))
            assert df.loc[t["exit_ts"], "low"] <= t["floor"] + 1e-9
        elif t["reason"] == "gap":
            assert t["exit"] == pytest.approx(df.loc[t["exit_ts"], "open"] * (1 - cost))
            assert df.loc[t["exit_ts"], "open"] <= t["floor"] + 1e-9
        elif t["reason"] == "halt":
            assert t["exit"] == pytest.approx(df.loc[t["exit_ts"], "open"] * (1 - cost))
        assert t["bars_held"] >= 0
    assert res["metrics"]["exit_reasons"]["floor"] == kinds["floor"]


def test_backtest_close_exit_mode_still_available(cfg):
    c = copy.deepcopy(cfg)
    c["rising_floor"]["position"]["evaluate_on"] = "completed_bar"
    assert BT.exit_check_for(c) == "close"
    c["rising_floor"]["position"]["evaluate_on"] = "bogus"
    with pytest.raises(ValueError):
        BT.exit_check_for(c)
    c["rising_floor"]["position"]["evaluate_on"] = "completed_bar"
    bars = D.align(BT.synthetic_bars(c, bars=500, seed=2), mode="point_in_time")
    res = BT.run_strategy(bars, c)
    cost = (c["costs"]["fee_bps"] + c["costs"]["slippage_bps"]) / 10_000
    for t in res["trades"]:
        assert t["exit"] == pytest.approx(bars[t["symbol"]].loc[t["exit_ts"], "open"] * (1 - cost))
        assert t["bars_held"] >= 1


def test_backtest_force_closes_delisted_holding(cfg):
    c = copy.deepcopy(cfg)
    raw = BT.synthetic_bars(c, bars=600, seed=4)
    victim = c["universe"]["symbols"][1]
    raw[victim] = raw[victim].iloc[:420]           # delisted at bar 420, never returns
    bars = D.align(raw, mode="point_in_time")
    assert bars[victim]["close"].isna().sum() == 180
    res = BT.run_strategy(bars, c)
    held_through = [t for t in res["trades"] if t["symbol"] == victim and t["reason"] == "delisted"]
    assert all(o["symbol"] != victim for o in res["open_at_end"])
    for t in held_through:
        assert pd.Timestamp(t["exit_ts"]) <= bars[victim].index[420]
    assert np.isfinite(res["curve"]).all()


def test_sub_window_checks(cfg):
    c = copy.deepcopy(cfg)
    c["backtest_gate"]["sub_windows"] = 2
    idx = pd.date_range("2024-01-01", periods=401, freq="D", tz="UTC")
    # strategy: up in the first half, flat in the second; control: flat then up
    strat = pd.Series(np.concatenate([np.linspace(100, 150, 201), np.full(200, 150.0)]), index=idx)
    ctrl = pd.Series(np.concatenate([np.full(201, 100.0), np.linspace(100, 150, 200)]), index=idx)
    checks = BT.sub_window_checks({"curve": strat}, {"equal_weight": {"curve": ctrl}}, c)
    assert len(checks) == 4
    beat = [x for x in checks if "beat" in x["check"]]
    assert beat[0]["ok"] is True and beat[1]["ok"] is False   # second half loses to the control
    ok, reason, allc = BT.evaluate_gate(
        {"curve": strat, "metrics": {"sharpe": 2.0, "max_drawdown_pct": 0.0, "total_return_pct": 0.5, "trades": 50, "years": 3.5}},
        {"equal_weight": {"curve": ctrl, "metrics": {"sharpe": 0.1}}}, c)
    assert not ok and "sub-window 2/2 beat equal_weight" in reason
    c["backtest_gate"]["sub_windows"] = 1
    assert BT.sub_window_checks({"curve": strat}, {"equal_weight": {"curve": ctrl}}, c) == []


def test_buy_and_hold_drops_names_absent_at_start(cfg):
    c = copy.deepcopy(cfg)
    raw = BT.synthetic_bars(c, bars=600, seed=9)
    late = c["universe"]["symbols"][2]
    raw[late] = raw[late].iloc[400:]                 # listed after the window start
    bars = D.align(raw, mode="point_in_time")
    ew = BT.buy_and_hold(bars, c, {s: 1.0 / len(bars) for s in bars})
    assert ew["metrics"]["names"] == len(bars) - 1
    assert np.isfinite(ew["curve"]).all()


def test_live_exit_uses_completed_bar_not_live_mark(live):
    """A live mark below the floor must NOT trigger an exit when the completed bar closed above it."""
    c, install, grant, events, tmp = live
    grant()
    br = install(FakeBroker(positions={"BTC/USD": {"qty": 1.0, "market_value": 100.0, "avg_entry_price": 480.0,
                                                    "current_price": 100.0, "unrealized_pl": -380.0}}))
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert "BTC/USD" not in br.closed
    assert not any(e["event"] == "exit_signal" for e in events())
    # ...and with evaluate_on: current_price the same wick DOES trigger it
    c["rising_floor"]["position"]["evaluate_on"] = "current_price"
    (tmp / "talon_state.json").unlink()
    br2 = install(FakeBroker(positions={"BTC/USD": {"qty": 1.0, "market_value": 100.0, "avg_entry_price": 480.0,
                                                     "current_price": 100.0, "unrealized_pl": -380.0}}))
    assert RT.run("cycle", c) == RT.EXIT_OK
    assert "BTC/USD" in br2.closed


# ---------------------------------------------------------------------------
# Phone page — talon_board.render() is pure; exercise it offline
# ---------------------------------------------------------------------------

import talon_board as TB  # noqa: E402
from datetime import datetime as _dt  # noqa: E402


def _gate(passed: bool) -> dict:
    return {
        "passed": passed,
        "reason": "all checks passed" if passed else "FAILED: sub-window 1/2 beat equal_weight on sharpe",
        "generated_utc": "2026-09-06T06:56:53+00:00",
        "universe": ["BTC/USD", "ETH/USD"],
        "window": {"start": "2023-04-05 00:00:00+00:00", "end": "2026-09-06 00:00:00+00:00", "bars": 1251},
        "checks": [
            {"check": "sharpe >= min_sharpe", "value": 0.911, "threshold": 0.8, "ok": True},
            {"check": "sub-window 1/2 beat equal_weight on sharpe", "window": "2023-04-05..2024-12-20",
             "value": 1.307, "threshold": 1.345, "ok": passed},
        ],
        "metrics": {"strategy": {"total_return_pct": 0.71, "sharpe": 0.911, "max_drawdown_pct": 0.258, "trades": 80},
                    "btc_hold": {"total_return_pct": 1.82, "sharpe": 0.887, "max_drawdown_pct": 0.531},
                    "equal_weight": {"total_return_pct": 0.83, "sharpe": 0.583, "max_drawdown_pct": 0.723}},
    }


def test_board_renders_with_nothing_but_config(cfg):
    page = TB.render(cfg, {}, {}, [], {}, _dt(2026, 9, 6, 18, 35))
    assert "Gate: <b>NOT RUN</b>" in page
    assert "No cycle has got past the gate" in page
    assert "No open positions" in page
    assert "Next ring <b>Sun 08:00 PM ET</b>" in page
    assert "<script>" in page and page.count("<table") == 0


def test_board_failing_gate_is_the_headline(cfg):
    page = TB.render(cfg, _gate(False), {}, [], {"last": 60000.0, "sma": 55000.0, "asof": "2026-09-05", "closes": {}},
                     _dt(2026, 9, 6, 23, 30))
    assert "FAILING 🔴" in page and "1/2 checks" in page
    assert "❌ sub-window 1/2 beat equal_weight on sharpe" in page and "2023-04-05..2024-12-20" in page
    assert "Regime: <b>ON 🟢</b>" in page and "+9.1%" in page
    assert "Next ring <b>Mon 12:00 AM ET</b>" in page
    assert "80 trades" in page


def test_board_with_state_and_journal(cfg):
    fl = PositionFloor.open("ETH/USD", 3000.0, 60.0, cfg)
    fl.update(3400.0, cfg)  # +3.3R -> locked_2R
    state = {
        "last_run_utc": "2026-09-06T20:00:40+00:00",
        "equity_high_water": 10_800.0,
        "halted": False, "halt_reason": "",
        "account_floor": {"basis": 10_600.0, "locked": 400.0, "high_water": 11_000.0, "history": []},
        "positions": {"ETH/USD": fl.to_dict()},
        "last_snapshot": {"equity": 10_500.0, "locked": 400.0, "regime_on": True, "open_count": 1},
    }
    journal = [
        {"ts": "2026-09-05T00:05:00+00:00", "run_id": "r1", "event": "snapshot", "equity": 10_000.0},
        {"ts": "2026-09-06T00:05:00+00:00", "run_id": "r2", "event": "plan", "regime_on": True, "tradeable": 10_100.0,
         "targets": {"ETH/USD": 2500.0}, "notes": ["Regime ON."],
         "candidates": [{"symbol": "ETH/USD", "score": 0.8, "momentum": 0.12, "reasons": ["ema_trend", "donchian_breakout"]},
                        {"symbol": "LTC/USD", "score": 0.4, "momentum": -0.02, "reasons": ["ema_trend"]}]},
        {"ts": "2026-09-06T00:05:03+00:00", "run_id": "r2", "event": "entry", "symbol": "ETH/USD", "notional": 2500.0},
        {"ts": "2026-09-06T00:05:04+00:00", "run_id": "r2", "event": "snapshot", "equity": 10_500.0},
    ]
    btc = {"last": 60000.0, "sma": 62000.0, "asof": "2026-09-05",
           "closes": {"2026-09-05": 58000.0, "2026-09-06": 60000.0}}
    c = copy.deepcopy(cfg)
    c["risk"]["starting_equity"] = 10_000   # the fixture's snapshots are on a $10k base
    page = TB.render(c, _gate(True), state, journal, btc, _dt(2026, 9, 6, 10, 0))
    assert "PASSED 🟢" in page
    assert "Regime: <b>OFF 🟡" in page
    assert "ETH/USD" in page and "locked_2R" in page and "+3.3R" in page
    assert "$400" in page and "basis $10,600" in page
    assert "2.8% below high-water $10,800" in page
    assert "planned entries: ETH/USD $2,500" in page
    assert "ema_trend, donchian_breakout" in page
    assert "entry ETH/USD" in page
    assert "linechart" in page          # two snapshots -> a real chart
    assert "$10,500" in page and "$10,345" in page   # BTC hold: 10,000 * 60000/58000
    assert "Next ring <b>Sun 12:00 PM ET</b>" in page


def test_board_cli_offline_writes_file(tmp_path):
    out = tmp_path / "talon.html"
    assert TB.main(["--offline", "--out", str(out)]) == 0
    s = out.read_text(encoding="utf-8")
    assert "<title>TALON" in s and "Gate:" in s
