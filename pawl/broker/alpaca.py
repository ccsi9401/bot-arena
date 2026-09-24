"""Alpaca crypto spot adapter.

Honest capability sheet: expensive (25bps taker) and long-only, but it is the
only venue in this package that combines a free paper environment with
stop_limit + GTC -- which is exactly what the resting floor needs. That
combination is why PAWL proves itself here before it trades anywhere else.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import requests

from .base import Broker, BrokerError, Capabilities

ALPACA_CAPS = Capabilities(
    name="Alpaca crypto spot",
    resting_stop=True, good_till_cancelled=True,
    shorting=False, perpetuals=False, native_trailing_stop=False,
    taker_fee=0.0025, maker_fee=0.0015, paper_supported=True,
    notes="Free paper trading with the same API as live. Market/limit/stop_limit, "
          "gtc or ioc. No shorting, no perps. 50bps round trip is the binding constraint.",
)

TRADE_PAPER = "https://paper-api.alpaca.markets"
TRADE_LIVE = "https://api.alpaca.markets"
DATA = "https://data.alpaca.markets"


class AlpacaBroker(Broker):
    def __init__(self, paper: bool = True, key: Optional[str] = None, secret: Optional[str] = None):
        self.caps = ALPACA_CAPS
        self.key = key or os.environ.get("PAWL_API_KEY", "")
        self.secret = secret or os.environ.get("PAWL_API_SECRET", "")
        if not self.key or not self.secret:
            raise BrokerError("PAWL_API_KEY / PAWL_API_SECRET are not set")
        self.base = TRADE_PAPER if paper else TRADE_LIVE
        self.s = requests.Session()
        self.s.headers.update({
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "accept": "application/json",
        })

    # -- plumbing ----------------------------------------------------------
    def _req(self, method: str, url: str, **kw):
        last = None
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=30, **kw)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise BrokerError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
                return r.json() if r.text else {}
            except requests.RequestException as e:      # transport only
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise BrokerError(f"{method} {url} failed after retries: {last}")

    # -- account -----------------------------------------------------------
    def account(self) -> dict:
        return self._req("GET", f"{self.base}/v2/account")

    def equity(self) -> float:
        return float(self.account()["equity"])

    def positions(self) -> Dict[str, dict]:
        rows = self._req("GET", f"{self.base}/v2/positions")
        out = {}
        for p in rows:
            sym = p["symbol"]
            if "/" not in sym and sym.endswith("USD"):      # BTCUSD -> BTC/USD
                sym = f"{sym[:-3]}/USD"
            out[sym] = p
        return out

    def open_orders(self) -> List[dict]:
        return self._req("GET", f"{self.base}/v2/orders", params={"status": "open", "limit": 200})

    # -- orders ------------------------------------------------------------
    def market(self, symbol: str, side: str, qty: float) -> dict:
        return self._req("POST", f"{self.base}/v2/orders", json={
            "symbol": symbol, "qty": _q(qty), "side": side,
            "type": "market", "time_in_force": "gtc",
        })

    def protective_stop(self, symbol: str, qty: float, stop: float, limit: float) -> Optional[dict]:
        return self._req("POST", f"{self.base}/v2/orders", json={
            "symbol": symbol, "qty": _q(qty), "side": "sell",
            "type": "stop_limit", "time_in_force": "gtc",
            "stop_price": f"{stop:.2f}", "limit_price": f"{limit:.2f}",
        })

    def cancel(self, order_id: str) -> None:
        try:
            self._req("DELETE", f"{self.base}/v2/orders/{order_id}")
        except BrokerError as e:
            if "404" in str(e) or "422" in str(e):
                return          # already gone or already filled -- fine
            raise

    def cancel_floor_for(self, symbol: str) -> None:
        """A resting stop_limit HOLDS the quantity. Anything that wants to sell
        or resize the position must clear the floor order first."""
        for o in self.open_orders():
            osym = o.get("symbol", "")
            norm = f"{osym[:-3]}/USD" if "/" not in osym and osym.endswith("USD") else osym
            if norm == symbol and o.get("type") == "stop_limit" and o.get("side") == "sell":
                self.cancel(o["id"])

    # -- data --------------------------------------------------------------
    def _raw_bars(self, symbols: List[str], timeframe: str = "1D", start: Optional[str] = None,
                  limit: int = 10000) -> dict:
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "limit": limit}
        if start:
            params["start"] = start
        out: Dict[str, list] = {}
        page = None
        while True:
            if page:
                params["page_token"] = page
            j = self._req("GET", f"{DATA}/v1beta3/crypto/us/bars", params=params)
            for sym, rows in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            page = j.get("next_page_token")
            if not page:
                break
        return out


def _q(qty: float) -> str:
    """Alpaca crypto decimals vary by asset; 8dp is safe for all of them and
    truncating (not rounding) avoids ordering a fraction more than we hold."""
    import math
    return f"{math.floor(qty * 1e8) / 1e8:.8f}"

    def daily_bars(self, symbols: List[str], days: int = 1200) -> Dict[str, pd.DataFrame]:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        raw = self._raw_bars(symbols, timeframe="1D", start=start)
        out: Dict[str, pd.DataFrame] = {}
        for sym, rows in raw.items():
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df["t"] = pd.to_datetime(df["t"], utc=True)
            df = df.set_index("t").sort_index().rename(
                columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            out[sym] = df[["open", "high", "low", "close", "volume"]].astype(float)
        return out
