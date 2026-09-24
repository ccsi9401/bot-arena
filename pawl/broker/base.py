"""
Broker abstraction.

The point of this layer is that the venue is a config line, not a rewrite.
It exists because the venues differ in ways that change the STRATEGY, not just
the plumbing -- specifically whether you can leave a protective order resting
at the exchange, and whether you can go short.

Capabilities are declared, and PAWL adapts. A venue that cannot hold a resting
stop gets a bot-side floor and a louder warning; a venue that can short unlocks
strategies the long-only build cannot reach.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd


class BrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capabilities:
    name: str
    resting_stop: bool        # can a protective order sit at the exchange?
    good_till_cancelled: bool # or does TIF expire out from under you?
    shorting: bool
    perpetuals: bool
    native_trailing_stop: bool
    taker_fee: float
    maker_fee: float
    paper_supported: bool
    notes: str = ""

    @property
    def roundtrip_taker(self) -> float:
        return 2 * self.taker_fee

    def floor_mode(self) -> str:
        """Which floor implementation this venue can actually support."""
        if self.native_trailing_stop:
            return "native_trailing"
        if self.resting_stop and self.good_till_cancelled:
            return "resting_ratchet"
        return "bot_side"          # weakest: protection dies with the process


class Broker(ABC):
    caps: Capabilities

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def positions(self) -> Dict[str, dict]: ...

    @abstractmethod
    def open_orders(self) -> List[dict]: ...

    @abstractmethod
    def market(self, symbol: str, side: str, qty: float) -> dict: ...

    @abstractmethod
    def cancel(self, order_id: str) -> None: ...

    @abstractmethod
    def cancel_floor_for(self, symbol: str) -> None: ...

    @abstractmethod
    def daily_bars(self, symbols: List[str], days: int) -> Dict[str, pd.DataFrame]: ...

    def protective_stop(self, symbol: str, qty: float, stop: float, limit: float) -> Optional[dict]:
        """Place a resting protective order. Returns None on venues that cannot
        hold one -- callers must treat None as 'this position is protected only
        while the ratchet loop runs' and say so in the journal."""
        return None
