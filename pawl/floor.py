"""
The rising floor.

Alpaca crypto supports market, limit and stop_limit -- there is no
trailing_stop order type. So PAWL synthesises one: a resting stop_limit sell
lives at the broker, and the bot moves it upward by cancel-and-replace.

Why this beats a fast polling loop:
  - The resting order is exchange-side. Protection does not depend on this
    process being alive, on GitHub Actions firing on time, or on a network hop.
    Between cycles the floor is enforced in the matching engine, not by us.
  - Moving the floor is never urgent, because it only ever moves UP. Missing a
    ratchet cycle leaves you protected at a slightly lower price -- it never
    leaves you unprotected. So a 4-hour ratchet is sufficient where a bot-side
    stop would need seconds.

The one real gap: cancel-and-replace has a short window where the quantity is
free. Ratchets are therefore never issued when the last price is already within
one ATR of the current floor -- if we are that close to stopping out, we leave
the existing order alone and let it work.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class FloorState:
    symbol: str
    entry_price: float
    high_water: float          # highest DAILY CLOSE since entry, not highest tick
    floor_price: float
    order_id: Optional[str] = None
    last_ratchet: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FloorState":
        return FloorState(**d)


def floor_multiple(cfg: dict) -> float:
    mode = cfg["floor"].get("mode", "tight")
    if mode == "catastrophe":
        return float(cfg["floor"].get("catastrophe_atr_multiple", 10.0))
    return float(cfg["floor"]["atr_multiple"])


def compute_floor(high_water: float, atr_value: float, multiple: float,
                  cfg: dict | None = None) -> float:
    """Floor price. In catastrophe mode the floor is whichever of the ATR
    distance and the flat percentage sits FURTHER from the high-water mark --
    the point is to be out of the way of normal volatility, so we take the
    looser of the two, never the tighter."""
    by_atr = high_water - multiple * atr_value
    if cfg and cfg["floor"].get("mode") == "catastrophe":
        by_pct = high_water * (1.0 - float(cfg["floor"].get("catastrophe_pct", 0.35)))
        return min(by_atr, by_pct)
    return by_atr


def enabled(cfg: dict) -> bool:
    return cfg["floor"].get("mode", "tight") != "none"


def open_floor(symbol: str, entry_price: float, close: float, atr_value: float, cfg: dict) -> FloorState:
    hw = max(entry_price, close)
    return FloorState(
        symbol=symbol,
        entry_price=entry_price,
        high_water=hw,
        floor_price=compute_floor(hw, atr_value, floor_multiple(cfg), cfg),
        last_ratchet=datetime.now(timezone.utc).isoformat(),
    )


def ratchet(state: FloorState, close: float, atr_value: float, cfg: dict) -> tuple[FloorState, bool]:
    """Update the floor. Returns (state, moved) -- moved is True only when the
    floor actually rose enough to be worth a cancel-and-replace."""
    new_hw = max(state.high_water, close)
    candidate = compute_floor(new_hw, atr_value, floor_multiple(cfg), cfg)

    # Never lower the floor. Ever. This is the whole invariant.
    new_floor = max(state.floor_price, candidate)

    # Don't churn orders for trivial moves -- each cancel/replace is a window
    # of exposure. Require a move of at least 10% of one ATR.
    moved = (new_floor - state.floor_price) > 0.10 * atr_value

    # If price is already sitting inside one ATR of the floor, leave the
    # resting order alone: this is exactly when we least want a gap.
    if close - state.floor_price < atr_value:
        moved = False

    state.high_water = new_hw
    if moved:
        state.floor_price = new_floor
        state.last_ratchet = datetime.now(timezone.utc).isoformat()
    return state, moved


def limit_for(stop_price: float, cfg: dict) -> float:
    """A stop_limit whose limit sits AT the stop will often not fill when price
    is moving fast through it. Put the limit below the stop so the resulting
    order is marketable, and accept that offset as the cost of certainty."""
    return stop_price * (1.0 - cfg["floor"]["limit_offset_pct"])


def breached(state: FloorState, last_price: float) -> bool:
    """Bot-side backstop. The resting order is the primary defence; this only
    catches the case where the order went missing (cancelled, rejected, or
    never placed) and we must exit at market."""
    return last_price <= state.floor_price
