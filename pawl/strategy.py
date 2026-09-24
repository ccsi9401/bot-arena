"""
PAWL strategy core.

Pure functions only. No network, no broker, no clock. Everything in here takes
price history plus config and returns a decision. The live runner and the
backtest both call THESE functions -- there is no second copy of the sizing
logic anywhere in this package, and there must never be one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ANNUALISATION = 365.0  # crypto trades every day


# ----------------------------------------------------------------------------
# indicators
# ----------------------------------------------------------------------------

def sma(close: pd.Series, days: int) -> pd.Series:
    return close.rolling(days, min_periods=days).mean()


def trailing_return(close: pd.Series, days: int) -> pd.Series:
    return close / close.shift(days) - 1.0


def realised_vol(close: pd.Series, days: int) -> pd.Series:
    """Annualised realised volatility of daily log returns."""
    r = np.log(close / close.shift(1))
    return r.rolling(days, min_periods=days).std() * np.sqrt(ANNUALISATION)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, days: int) -> pd.Series:
    """Wilder-style ATR on daily bars."""
    prev = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev).abs(),
        (low - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / days, adjust=False, min_periods=days).mean()


# ----------------------------------------------------------------------------
# decisions
# ----------------------------------------------------------------------------

@dataclass
class AssetView:
    """Everything the strategy knows about one asset at one point in time."""
    symbol: str
    close: float
    sma_asset: float
    mom: float
    vol: float
    atr: float
    dollar_vol_30d: float

    @property
    def valid(self) -> bool:
        vals = [self.close, self.sma_asset, self.mom, self.vol, self.atr]
        return all(v is not None and np.isfinite(v) for v in vals) and self.vol > 0


@dataclass
class Decision:
    regime_on: bool
    targets: Dict[str, float] = field(default_factory=dict)   # symbol -> weight of equity
    exits: List[str] = field(default_factory=list)
    reasons: Dict[str, str] = field(default_factory=dict)
    rotation_enabled: bool = True


def build_views(bars: Dict[str, pd.DataFrame], cfg: dict, asof: Optional[pd.Timestamp] = None) -> Dict[str, AssetView]:
    """bars: symbol -> DataFrame indexed by date with open/high/low/close/volume."""
    out: Dict[str, AssetView] = {}
    a_sma = cfg["regime"]["asset_sma_days"]
    look = cfg["momentum"]["lookback_days"]
    vlook = cfg["sizing"]["vol_lookback_days"]
    adays = cfg["floor"]["atr_days"]

    for sym, df in bars.items():
        if df is None or df.empty:
            continue
        d = df if asof is None else df.loc[:asof]
        if len(d) < max(a_sma, look, vlook, adays) + 2:
            continue
        c = d["close"]
        dv = (c * d["volume"]).rolling(30, min_periods=15).median()
        out[sym] = AssetView(
            symbol=sym,
            close=float(c.iloc[-1]),
            sma_asset=float(sma(c, a_sma).iloc[-1]),
            mom=float(trailing_return(c, look).iloc[-1]),
            vol=float(realised_vol(c, vlook).iloc[-1]),
            atr=float(atr(d["high"], d["low"], c, adays).iloc[-1]),
            dollar_vol_30d=float(dv.iloc[-1]) if np.isfinite(dv.iloc[-1]) else 0.0,
        )
    return out


def regime_on(bars: Dict[str, pd.DataFrame], cfg: dict, asof: Optional[pd.Timestamp] = None) -> bool:
    """The market-wide switch. BTC above its 200d SMA, or nothing trades."""
    anchor = cfg["regime"]["anchor"]
    df = bars.get(anchor)
    if df is None or df.empty:
        return False
    d = df if asof is None else df.loc[:asof]
    days = cfg["regime"]["sma_days"]
    if len(d) < days + 1:
        return False
    s = sma(d["close"], days).iloc[-1]
    if not np.isfinite(s):
        return False
    return bool(d["close"].iloc[-1] > s)


def eligible(views: Dict[str, AssetView], cfg: dict, held: Optional[set] = None,
             rotation_enabled: bool = True, blocked: Optional[set] = None) -> List[AssetView]:
    """Assets that pass liquidity, trend filter and the momentum entry band.

    Held positions use the (lower) exit threshold instead of the entry
    threshold -- that gap is the buffer band, and it is the single most
    important anti-whipsaw device in the system.
    """
    held = held or set()
    blocked = blocked or set()
    core = set(cfg["universe"]["core"])
    universe = list(cfg["universe"]["core"])
    if rotation_enabled:
        universe += list(cfg["universe"]["rotation"])

    min_dv = cfg["universe"]["min_median_dollar_vol_30d"]
    enter = cfg["momentum"]["entry_threshold"]
    exit_ = cfg["momentum"]["exit_threshold"]

    picks = []
    for sym in universe:
        v = views.get(sym)
        if v is None or not v.valid:
            continue
        if sym in blocked and sym not in held:
            continue          # cooling off after an exit -- no instant re-entry
        # Core names skip the screen. Alpaca reports only its own venue's volume
        # (~$150k/day for BTC since 2023), so a $3M screen on Alpaca bars blocked
        # BTC and ETH -- the two most liquid assets in crypto -- every day.
        if sym not in core and v.dollar_vol_30d < min_dv:
            continue
        if v.close <= v.sma_asset:
            continue
        threshold = exit_ if sym in held else enter
        if v.mom <= threshold:
            continue
        picks.append(v)
    return picks


def rank(picks: List[AssetView], cfg: dict) -> List[AssetView]:
    if cfg["momentum"]["rank_by"] == "risk_adjusted":
        key = lambda v: v.mom / v.vol
    else:
        key = lambda v: v.mom
    return sorted(picks, key=key, reverse=True)[: cfg["momentum"]["max_positions"]]


def target_weights(chosen: List[AssetView], cfg: dict) -> Dict[str, float]:
    """Volatility targeting: size inversely to each asset's own realised vol,
    then apply the per-position and total caps. Small in chaos, large in calm."""
    if not chosen:
        return {}
    s = cfg["sizing"]
    raw = {v.symbol: s["target_vol_annual"] / v.vol for v in chosen}
    capped = {k: min(w, s["max_weight_per_position"]) for k, w in raw.items()}
    total = sum(capped.values())
    if total > s["max_total_invested"]:
        scale = s["max_total_invested"] / total
        capped = {k: w * scale for k, w in capped.items()}
    return capped


def decide(bars: Dict[str, pd.DataFrame], cfg: dict, held: Optional[set] = None,
           asof: Optional[pd.Timestamp] = None, rotation_enabled: bool = True,
           blocked: Optional[set] = None, allow_entries: bool = True) -> Decision:
    """The whole signal stack in one call. This is what both the live runner
    and the backtest use."""
    held = set(held or [])
    d = Decision(regime_on=False, rotation_enabled=rotation_enabled)

    if not regime_on(bars, cfg, asof):
        d.regime_on = False
        d.exits = sorted(held)
        for h in held:
            d.reasons[h] = "regime off: anchor below its long-term trend"
        return d

    d.regime_on = True
    views = build_views(bars, cfg, asof)
    picks = eligible(views, cfg, held=held, rotation_enabled=rotation_enabled, blocked=blocked)
    chosen = rank(picks, cfg)
    if not allow_entries:
        # Turnover budget spent, or a daily-loss halt: we may keep and trim what
        # we hold, but we may not open anything new.
        chosen = [v for v in chosen if v.symbol in held]
    d.targets = target_weights(chosen, cfg)

    chosen_syms = set(d.targets)
    d.exits = sorted(held - chosen_syms)
    for h in d.exits:
        v = views.get(h)
        if v is None:
            d.reasons[h] = "no data"
        elif v.close <= v.sma_asset:
            d.reasons[h] = "below own trend"
        elif v.mom <= cfg["momentum"]["exit_threshold"]:
            d.reasons[h] = f"momentum {v.mom:.1%} below exit band"
        else:
            d.reasons[h] = "displaced by a higher-ranked asset"
    for s in chosen_syms:
        if s not in held:
            v = views[s]
            d.reasons[s] = f"entry: mom {v.mom:.1%}, vol {v.vol:.0%}, weight {d.targets[s]:.1%}"
    return d


# ----------------------------------------------------------------------------
# rebalance filter -- the fee discipline
# ----------------------------------------------------------------------------

def orders_needed(targets: Dict[str, float], current: Dict[str, float], equity: float,
                  cfg: dict) -> Dict[str, float]:
    """Return symbol -> signed USD delta, but only where the gap is big enough
    to be worth a round trip. At 50bps a turn, churn is the main enemy."""
    s = cfg["sizing"]
    band_eq = s["drift_band_pct_of_equity"] * equity
    out: Dict[str, float] = {}
    for sym in set(targets) | set(current):
        tgt_w = targets.get(sym, 0.0)
        cur_w = current.get(sym, 0.0)
        delta_usd = (tgt_w - cur_w) * equity
        if tgt_w == 0.0:
            out[sym] = delta_usd          # full exits always go through
            continue
        band_tgt = s["drift_band_pct_of_target"] * tgt_w * equity
        if abs(delta_usd) < max(band_eq, band_tgt):
            continue
        if abs(delta_usd) < s["min_notional_usd"]:
            continue
        out[sym] = delta_usd
    return {k: v for k, v in out.items() if abs(v) > 1e-9}
