"""Alpaca REST adapter, PAPER ONLY.

There is no live switch in this bot. The base URL is paper-api.alpaca.markets and the
constructor raises if config says anything but ``mode: paper``. Credentials come from
TWINCOIN_API_KEY / TWINCOIN_API_SECRET and are never written or logged.

Symbols: positions come back as ``BTCUSD``; orders and data use ``BTC/USD``.
Crypto orders on Alpaca support market, limit and stop_limit with time_in_force gtc.
"""
from __future__ import annotations

import time

import requests

from twincoin import credentials

PAPER_URL = "https://paper-api.alpaca.markets"


class CredentialsError(RuntimeError):
    pass


class PaperOnlyError(RuntimeError):
    pass


def with_slash(sym: str, known: list[str]) -> str:
    if "/" in sym:
        return sym
    for k in known:
        if k.replace("/", "") == sym:
            return k
    return sym[:-3] + "/" + sym[-3:] if sym.endswith("USD") else sym


class Alpaca:
    def __init__(self, cfg: dict, session: requests.Session | None = None):
        if str(cfg["meta"].get("mode", "paper")).lower() != "paper":
            raise PaperOnlyError("twincoin is paper-only; config meta.mode must be 'paper'")
        key, secret = credentials(cfg)
        if not key or not secret:
            p = cfg["meta"].get("account_env_prefix", "TWINCOIN")
            raise CredentialsError(f"{p}_API_KEY / {p}_API_SECRET are not set in the environment")
        self.base = PAPER_URL
        self._h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json"}
        self._s = session or requests.Session()
        self._timeout = float(cfg["data"]["timeout_s"])
        self.known = list(cfg["universe"]["symbols"])
        self.fill_wait = float(cfg["data"].get("fill_wait_s", 25))

    # transport
    def _req(self, method: str, path: str, **kw):
        r = self._s.request(method, f"{self.base}{path}", headers=self._h, timeout=self._timeout, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    # account / positions
    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def positions(self) -> dict[str, dict]:
        out = {}
        for p in self._req("GET", "/v2/positions"):
            sym = with_slash(p["symbol"], self.known)
            out[sym] = {"qty": float(p["qty"]), "qty_available": float(p.get("qty_available", p["qty"])),
                        "avg_entry_price": float(p["avg_entry_price"]), "current_price": float(p["current_price"]),
                        "market_value": float(p["market_value"])}
        return out

    def asset(self, symbol: str) -> dict:
        return self._req("GET", f"/v2/assets/{symbol.replace('/', '%2F')}")

    def clock(self) -> dict:
        return self._req("GET", "/v2/clock")

    # orders
    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"status": "open", "limit": 200}
        if symbol:
            params["symbols"] = symbol
        return list(self._req("GET", "/v2/orders", params=params))

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> None:
        try:
            self._req("DELETE", f"/v2/orders/{order_id}")
        except RuntimeError as e:
            if "404" in str(e) or "422" in str(e):   # already gone / already filled
                return
            raise

    def cancel_symbol_orders(self, symbol: str) -> int:
        n = 0
        for o in self.open_orders(symbol):
            self.cancel_order(o["id"])
            n += 1
        return n

    def submit(self, symbol: str, side: str, qty: float, order_type: str, limit_price: float | None = None,
               stop_price: float | None = None, client_order_id: str | None = None) -> dict:
        body = {"symbol": symbol, "side": side, "type": order_type, "time_in_force": "gtc", "qty": f"{qty:.9f}".rstrip("0").rstrip(".")}
        if limit_price is not None:
            body["limit_price"] = f"{limit_price:.2f}"
        if stop_price is not None:
            body["stop_price"] = f"{stop_price:.2f}"
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._req("POST", "/v2/orders", json=body)

    def wait_fill(self, order_id: str) -> dict:
        """Poll a market order until filled (or give up after fill_wait_s). Returns the final order."""
        deadline = time.time() + self.fill_wait
        o = self.get_order(order_id)
        while o.get("status") not in ("filled", "canceled", "rejected", "expired") and time.time() < deadline:
            time.sleep(1.0)
            o = self.get_order(order_id)
        return o
