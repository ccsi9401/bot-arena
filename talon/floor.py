"""The rising floor: levels that move up and never down.

PositionFloor — per trade. Starts one ``initial_atr_mult`` ATR below entry (that
distance is 1R). As the trade's high-water mark advances, the floor ratchets:
break-even (plus a cost pad) at +1R, then locked-profit steps, then a percentage
trail. The current floor is ALWAYS in the candidate set, so the floor is monotonic
by construction — no code path can lower it.

AccountFloor — per account. Once equity is ``activation_gain_pct`` above its basis,
a fraction of the gain is swept into ``locked``; the basis is raised so the same
gain is never swept twice. ``locked`` is enforced non-decreasing in ``__setattr__``:
releasing the reserve is a human action performed by editing state, not something
the bot can do.

Both are plain dataclasses with to_dict/from_dict so they live in JSON state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

# Unit constants, not tunables.
BPS = 10_000.0          # basis points per unit
_STEP_EPS = 1e-9        # float tolerance so 0.10 / 0.05 counts as exactly 2 whole steps

EVAL_COMPLETED_BAR = "completed_bar"   # floors evaluated on the last completed bar's close
EVAL_CURRENT_PRICE = "current_price"   # floors evaluated on the live mark every cycle

STAGE_BASEMENT = "basement"
STAGE_BREAKEVEN = "breakeven"
STAGE_TRAILING = "trailing"


def _pcfg(cfg: dict) -> dict:
    return cfg["rising_floor"]["position"]


def _acfg(cfg: dict) -> dict:
    return cfg["rising_floor"]["account"]


def default_cost_bps(cfg: dict) -> float:
    """Round-trip cost pad: fee + slippage, both from the costs block."""
    return float(cfg["costs"]["fee_bps"]) + float(cfg["costs"]["slippage_bps"])


def r_unit_for(entry: float, atr_value: float, cfg: dict) -> float:
    """1R = max(ATR * initial_atr_mult, entry * min_r_pct). The max guards against a
    hair-trigger stop when ATR is tiny. Shared by the floor and the planner's sizing."""
    p = _pcfg(cfg)
    atr_value = float(atr_value) if atr_value is not None and not math.isnan(float(atr_value)) else 0.0
    return max(atr_value * float(p["initial_atr_mult"]), float(entry) * float(p["min_r_pct"]))


@dataclass
class PositionFloor:
    symbol: str
    entry: float
    r_unit: float
    floor: float
    high_water: float
    stage: str = STAGE_BASEMENT
    cost_pad: float = 0.0

    # ----------------------------------------------------------------- open
    @classmethod
    def open(cls, symbol: str, entry: float, atr_value: float, cfg: dict,
             cost_bps: float | None = None) -> "PositionFloor":
        """New floor at entry - 1R. ``cost_bps`` defaults to fee + slippage from config."""
        entry = float(entry)
        if cost_bps is None:
            cost_bps = default_cost_bps(cfg)
        ru = r_unit_for(entry, atr_value, cfg)
        return cls(
            symbol=symbol,
            entry=entry,
            r_unit=ru,
            floor=entry - ru,
            high_water=entry,
            stage=STAGE_BASEMENT,
            cost_pad=entry * float(cost_bps) / BPS,
        )

    # --------------------------------------------------------------- update
    def update(self, price: float, cfg: dict) -> float:
        """Push high_water up, rebuild the candidate floors, take the max.
        Returns the new floor. Never lowers it: the current floor is a candidate."""
        p = _pcfg(cfg)
        price = float(price)
        if price > self.high_water:
            self.high_water = price
        r_now = (self.high_water - self.entry) / self.r_unit

        candidates: list[tuple[float, str]] = [(self.floor, self.stage)]

        breakeven_at = float(p["breakeven_at_r"])
        if r_now >= breakeven_at:
            candidates.append((self.entry + self.cost_pad, STAGE_BREAKEVEN))

        for step in p.get("ratchet_steps", []):
            at_r, lock_r = float(step["at_r"]), float(step["lock_r"])
            if r_now >= at_r:
                candidates.append((self.entry + lock_r * self.r_unit, f"locked_{lock_r:g}R"))

        past_basement = self.stage != STAGE_BASEMENT or r_now >= breakeven_at
        if past_basement:
            candidates.append((self.high_water * (1.0 - float(p["trail_pct"])), STAGE_TRAILING))

        best_floor, best_stage = max(candidates, key=lambda c: c[0])
        if best_floor > self.floor:
            self.stage = best_stage
        elif best_floor == self.floor and self.stage == STAGE_BASEMENT and best_stage != STAGE_BASEMENT:
            self.stage = best_stage
        self.floor = max(self.floor, best_floor)
        return self.floor

    # ------------------------------------------------------------- queries
    def r_multiple(self, price: float) -> float:
        return (float(price) - self.entry) / self.r_unit

    def breached(self, price: float) -> bool:
        return float(price) <= self.floor

    # --------------------------------------------------------------- state
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PositionFloor":
        return cls(
            symbol=str(d["symbol"]),
            entry=float(d["entry"]),
            r_unit=float(d["r_unit"]),
            floor=float(d["floor"]),
            high_water=float(d["high_water"]),
            stage=str(d.get("stage", STAGE_BASEMENT)),
            cost_pad=float(d.get("cost_pad", 0.0)),
        )


@dataclass
class AccountFloor:
    basis: float
    locked: float = 0.0
    high_water: float = 0.0
    history: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.basis = float(self.basis)
        if self.high_water < self.basis:
            self.high_water = self.basis

    def __setattr__(self, name: str, value) -> None:
        # The one invariant that matters: locked never goes down inside the bot.
        if name == "locked" and "locked" in self.__dict__ and float(value) < self.__dict__["locked"]:
            raise ValueError(
                f"AccountFloor.locked cannot decrease ({self.__dict__['locked']} -> {value}); "
                "releasing the reserve is a human edit to state, not a bot action"
            )
        object.__setattr__(self, name, value)

    def update(self, total_equity: float, cfg: dict, ts: str) -> float:
        """Sweep part of the gain over basis into ``locked`` once the gain reaches
        ``activation_gain_pct``. Sweeps only whole ``step_pct`` increments of basis,
        then raises basis to ``total_equity - sweep``. Returns the amount swept."""
        a = _acfg(cfg)
        total_equity = float(total_equity)
        if total_equity > self.high_water:
            self.high_water = total_equity
        if self.basis <= 0:
            return 0.0
        gain = total_equity - self.basis
        gain_pct = gain / self.basis
        if gain_pct + _STEP_EPS < float(a["activation_gain_pct"]):
            return 0.0
        step = float(a["step_pct"])
        whole_steps = math.floor(gain_pct / step + _STEP_EPS)
        eligible_gain = self.basis * whole_steps * step
        sweep = eligible_gain * float(a["sweep_fraction"])
        if sweep <= 0:
            return 0.0
        self.locked = self.locked + sweep
        self.basis = total_equity - sweep
        self.history.append({
            "ts": ts,
            "equity": round(total_equity, 2),
            "swept": round(sweep, 2),
            "locked": round(self.locked, 2),
            "basis": round(self.basis, 2),
        })
        return sweep

    def tradeable(self, total_equity: float) -> float:
        return max(float(total_equity) - self.locked, 0.0)

    def to_dict(self) -> dict:
        return {
            "basis": self.basis,
            "locked": self.locked,
            "high_water": self.high_water,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AccountFloor":
        return cls(
            basis=float(d["basis"]),
            locked=float(d.get("locked", 0.0)),
            high_water=float(d.get("high_water", 0.0)),
            history=list(d.get("history", [])),
        )
