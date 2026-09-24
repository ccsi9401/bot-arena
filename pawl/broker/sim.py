"""
Simulated broker -- paper trading for venues that don't offer it.

Kraken has no spot sandbox and I could find no paper environment for Kraken
Derivatives US. That is a real problem, because the whole discipline here is
"prove it in paper first" and a venue you cannot paper on quietly pushes you
into proving it with money.

So: SimBroker keeps a local ledger and fills against REAL current prices pulled
from a data source, charging the TARGET venue's fee schedule. You get an honest
paper run for Kraken while the account and the API wiring are still unbuilt.

What it models: fees at the target venue's rates, slippage, resting protective
stops (checked against each new bar's low), and cash accounting.
What it does not model: partial fills, order rejection, queue position, real
latency, funding payments, or the venue being down. It is a rehearsal, not a
simulation of the venue's failure modes -- and the failure modes are most of
what a paper run is supposed to teach you. Treat a clean sim run as necessary
and nowhere near sufficient.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import pandas as pd

from .base import Broker, BrokerError, Capabilities

SIM_CAPS = Capabilities(
    name="Simulated (local ledger, real prices)",
    resting_stop=True, good_till_cancelled=True,
    shorting=False, perpetuals=False, native_trailing_stop=False,
    taker_fee=0.0025, maker_fee=0.0015, paper_supported=True,
    notes="Local ledger filled against live prices at a target venue's fee schedule. "
          "Models fees, slippage and resting stops. Does not model rejection, partial "
          "fills, latency, funding, or outages.",
)

LEDGER = "state/sim_ledger.json"


class SimBroker(Broker):
    def __init__(self, target_venue: str = "alpaca", data_broker: Optional[Broker] = None,
                 start_equity: float = 5_000.0, slippage: float = 0.0010,
                 ledger_path: str = LEDGER):
        from . import capabilities
        tgt = capabilities(target_venue)
        self.caps = Capabilities(
            name=f"Simulated [{tgt.name}]",
            resting_stop=tgt.resting_stop, good_till_cancelled=tgt.good_till_cancelled,
            shorting=tgt.shorting, perpetuals=tgt.perpetuals,
            native_trailing_stop=tgt.native_trailing_stop,
            taker_fee=tgt.taker_fee, maker_fee=tgt.maker_fee, paper_supported=True,
            notes=f"Simulated against {tgt.name} fees. " + SIM_CAPS.notes,
        )
        self.slippage = slippage
        self.path = ledger_path
        self.data = data_broker
        if self.data is None:
            from .alpaca import AlpacaBroker
            self.data = AlpacaBroker(paper=True)      # prices only
        self.book = self._load(start_equity)

    # -- ledger ------------------------------------------------------------
    def _load(self, start_equity: float) -> dict:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        return {"cash": start_equity, "qty": {}, "stops": {}, "orders": [], "last_px": {}}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.book, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -- data, and stop processing rides along on it -----------------------
    def daily_bars(self, symbols: List[str], days: int = 1200) -> Dict[str, pd.DataFrame]:
        bars = self.data.daily_bars(symbols, days)
        for sym, df in bars.items():
            if df is None or df.empty:
                continue
            self.book["last_px"][sym] = float(df["close"].iloc[-1])
            stop = self.book["stops"].get(sym)
            if stop and float(df["low"].iloc[-1]) <= stop["stop_price"]:
                q = self.book["qty"].get(sym, 0.0)
                if q > 0:
                    fill = min(stop["stop_price"], float(df["close"].iloc[-1]))
                    self._settle(sym, "sell", q, fill, note="resting stop filled")
                self.book["stops"].pop(sym, None)
        self._save()
        return bars

    def _settle(self, symbol: str, side: str, qty: float, price: float, note: str = "") -> dict:
        slip = 1 + self.slippage if side == "buy" else 1 - self.slippage
        px = price * slip
        gross = qty * px
        fee = gross * self.caps.taker_fee
        if side == "buy":
            if gross + fee > self.book["cash"] + 1e-6:
                raise BrokerError(f"sim: insufficient cash for {symbol} (need {gross+fee:.2f}, have {self.book['cash']:.2f})")
            self.book["cash"] -= gross + fee
            self.book["qty"][symbol] = self.book["qty"].get(symbol, 0.0) + qty
        else:
            have = self.book["qty"].get(symbol, 0.0)
            qty = min(qty, have)
            self.book["cash"] += gross - fee
            self.book["qty"][symbol] = have - qty
            if self.book["qty"][symbol] <= 1e-10:
                self.book["qty"].pop(symbol, None)
        rec = {"symbol": symbol, "side": side, "qty": qty, "price": px, "fee": fee, "note": note}
        self.book["orders"].append(rec)
        self._save()
        return rec

    # -- Broker interface --------------------------------------------------
    def equity(self) -> float:
        v = self.book["cash"]
        for s, q in self.book["qty"].items():
            v += q * self.book["last_px"].get(s, 0.0)
        return v

    def positions(self) -> Dict[str, dict]:
        out = {}
        for s, q in self.book["qty"].items():
            px = self.book["last_px"].get(s, 0.0)
            out[s] = {"symbol": s, "qty": str(q), "market_value": str(q * px),
                      "current_price": str(px), "unrealized_plpc": "0"}
        return out

    def open_orders(self) -> List[dict]:
        return [{"id": f"sim-stop-{s}", "symbol": s, "type": "stop_limit", "side": "sell"}
                for s in self.book["stops"]]

    def market(self, symbol: str, side: str, qty: float) -> dict:
        px = self.book["last_px"].get(symbol)
        if not px:
            raise BrokerError(f"sim: no price for {symbol} -- call daily_bars first")
        return self._settle(symbol, side, qty, px, note="market")

    def cancel(self, order_id: str) -> None:
        sym = order_id.replace("sim-stop-", "")
        self.book["stops"].pop(sym, None)
        self._save()

    def cancel_floor_for(self, symbol: str) -> None:
        self.book["stops"].pop(symbol, None)
        self._save()

    def protective_stop(self, symbol: str, qty: float, stop: float, limit: float) -> Optional[dict]:
        self.book["stops"][symbol] = {"stop_price": stop, "limit_price": limit, "qty": qty}
        self._save()
        return {"id": f"sim-stop-{symbol}"}
