"""
Kraken adapters -- spot and the new CFTC-regulated US perpetuals.

TWO VERY DIFFERENT VENUES SHARING A BRAND.

Kraken Pro SPOT is the worst-priced venue in this package: since the July 2026
tier rework, entry tier is 0.40% maker / 0.80% taker. That is a 1.60% taker
round trip, more than three times Alpaca. It is partly redeemable because tiers
now also qualify on Assets on Platform assessed in real time, so holding $20k+
moves you down the schedule without trading. It has genuinely rich order types
-- including a NATIVE trailing stop, which would retire PAWL's ratchet
entirely -- and margin shorting. What it does not have is a spot paper
environment.

Kraken Derivatives US is the interesting one, and the actual reason to move.
CFTC-regulated perpetual futures for US clients, launched June 2026, listed on
Bitnomial and cleared through NinjaTrader Clearing. Contracts on BTC, ETH, SOL,
XRP, ADA, LINK, DOGE, LTC and AVAX, on an 8-hour funding cycle.

Why that matters more than any fee table: it is the first onshore US venue that
makes the highest-evidence tier of the strategy manual reachable at all. You can
short. You can collect funding. Perp fees run an order of magnitude below spot.
Everything PAWL had to delete because Alpaca is long-only spot comes back.

Two warnings that belong right here, not in a footnote:

  1. LEVERAGE IS NOT THE FEATURE. Perps offer it; PAWL does not use it. The
     config caps notional at 1.0x equity and the adapter refuses to exceed it.
     Use perps for instrument access -- shorting and funding -- and nothing else.
  2. This venue is months old. Separate onboarding, a futures account, an API
     surface with little public history, and no paper environment I can
     confirm. Prove the strategy on Alpaca paper first. That is what the sim
     broker in this package is for.

Both classes below are deliberately unfinished at the order-placement layer.
I am not going to hand you code that sends live orders to an API whose exact
request shapes I could not test against a real endpoint from this sandbox.
The capability sheets, the cost model and the wiring are correct and usable now;
fill in _request() against the current Kraken docs before enabling either.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from .base import Broker, BrokerError, Capabilities

KRAKEN_SPOT_CAPS = Capabilities(
    name="Kraken Pro spot",
    resting_stop=True, good_till_cancelled=True,
    shorting=True,               # via margin, for eligible clients
    perpetuals=False,
    native_trailing_stop=True,   # would replace PAWL's ratchet outright
    taker_fee=0.0080, maker_fee=0.0040, paper_supported=False,
    notes="Entry tier 0.40/0.80 since the Jul 2026 rework -- worse than Alpaca. "
          "Tiers now also qualify on Assets on Platform in real time ($20k+ moves "
          "you down the schedule). Native trailing stop and margin shorting. No spot sandbox.",
)

KRAKEN_US_PERPS_CAPS = Capabilities(
    name="Kraken Derivatives US (CFTC-regulated perps)",
    resting_stop=True, good_till_cancelled=True,
    shorting=True, perpetuals=True, native_trailing_stop=True,
    taker_fee=0.0005, maker_fee=0.0002, paper_supported=False,
    notes="BTC/ETH/SOL/XRP/ADA/LINK/DOGE/LTC/AVAX, 8h funding. Order-of-magnitude "
          "cheaper than spot and it unlocks shorting and funding carry onshore. "
          "New venue, separate futures onboarding via NinjaTrader Clearing.",
)


class _KrakenBase(Broker):
    def __init__(self, key: Optional[str] = None, secret: Optional[str] = None,
                 max_notional_x_equity: float = 1.0):
        import os
        self.key = key or os.environ.get("PAWL_API_KEY", "")
        self.secret = secret or os.environ.get("PAWL_API_SECRET", "")
        self.max_notional_x_equity = max_notional_x_equity
        if not self.key or not self.secret:
            raise BrokerError("PAWL_API_KEY / PAWL_API_SECRET are not set")

    def _unimplemented(self, what: str):
        raise BrokerError(
            f"{self.caps.name}: {what} is not wired up yet. The capability sheet and "
            "cost model are correct; the request shapes were never tested against a "
            "live endpoint, so this adapter deliberately refuses rather than guessing. "
            "Implement against the current Kraken API docs, then run `pawl venues` "
            "and the gate before arming."
        )

    def guard_notional(self, notional: float, equity: float) -> None:
        """Perps make leverage available. PAWL does not take it."""
        if equity > 0 and notional > self.max_notional_x_equity * equity:
            raise BrokerError(
                f"order would take notional to {notional/equity:.2f}x equity; "
                f"cap is {self.max_notional_x_equity:.2f}x. Leverage is how accounts die, "
                "and it is not what this venue was chosen for."
            )

    def equity(self) -> float: self._unimplemented("equity()")
    def positions(self) -> Dict[str, dict]: self._unimplemented("positions()")
    def open_orders(self) -> List[dict]: self._unimplemented("open_orders()")
    def market(self, symbol, side, qty) -> dict: self._unimplemented("market()")
    def cancel(self, order_id: str) -> None: self._unimplemented("cancel()")
    def cancel_floor_for(self, symbol: str) -> None: self._unimplemented("cancel_floor_for()")
    def daily_bars(self, symbols, days: int = 1200): self._unimplemented("daily_bars()")


class KrakenSpotBroker(_KrakenBase):
    caps = KRAKEN_SPOT_CAPS


class KrakenUSPerpsBroker(_KrakenBase):
    caps = KRAKEN_US_PERPS_CAPS

    def funding_rate(self, symbol: str) -> float:
        """8h funding. On a venue with this, the manual's Tier A opens up:
        long spot + short perp collects it, and extreme funding is a positioning
        signal in its own right. Neither is implemented in the long-only build."""
        self._unimplemented("funding_rate()")
