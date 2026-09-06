"""Alpaca order adapter (REST, no SDK).

LIVE-MONEY GUARD — non-negotiable. The class points at paper-api.alpaca.markets
unless BOTH ``meta.mode == "live"`` in config AND ``TALON_FORCE_LIVE=I_UNDERSTAND``
is in the environment. Two independent switches. If only one is set the
constructor raises rather than guessing which one you meant.

Credentials come from TALON_API_KEY / TALON_API_SECRET. They are never written to
disk and never logged; no exception message here includes them.

Symbols: Alpaca's positions endpoint returns ``BTCUSD`` while orders and data use
``BTC/USD``. ``position_map`` re-inserts the slash.
"""
from __future__ import annotations

import os
import time

import requests

from talon import credentials

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
FORCE_LIVE_ENV = "TALON_FORCE_LIVE"
FORCE_LIVE_VALUE = "I_UNDERSTAND"
QUOTE_CCY = "USD"


class CredentialsError(RuntimeError):
    """TALON_API_KEY / TALON_API_SECRET are not set."""


class LiveGuardError(RuntimeError):
    """Exactly one of the two live switches is set."""


def resolve_base_url(cfg: dict, env: dict | None = None) -> str:
    """The two-switch guard, factored out so it is unit-testable without a network."""
    env = os.environ if env is None else env
    mode_live = str(cfg["meta"].get("mode", "paper")).lower() == "live"
    env_live = env.get(FORCE_LIVE_ENV) == FORCE_LIVE_VALUE
    if mode_live and env_live:
        return LIVE_URL
    if mode_live != env_live:
        raise LiveGuardError(
            "live trading needs BOTH meta.mode: live in config AND "
            f"{FORCE_LIVE_ENV}={FORCE_LIVE_VALUE} in the environment; exactly one is set "
            f"(config mode={'live' if mode_live else 'paper'}, env switch={'set' if env_live else 'unset'}). "
            "Refusing to guess."
        )
    return PAPER_URL


def with_slash(sym: str, known: list[str] | None = None) -> str:
    """'BTCUSD' -> 'BTC/USD'. Prefers an exact match against the configured universe."""
    if "/" in sym:
        return sym
    for k in known or []:
        if k.replace("/", "") == sym:
            return k
    if sym.endswith(QUOTE_CCY) and len(sym) > len(QUOTE_CCY):
        return f"{sym[:-len(QUOTE_CCY)]}/{QUOTE_CCY}"
    return sym


def without_slash(sym: str) -> str:
    return sym.replace("/", "")


class Alpaca:
    def __init__(self, cfg: dict, session: requests.Session | None = None):
        self.cfg = cfg
        self.base = resolve_base_url(cfg)
        key, secret = credentials(cfg)
        if not key or not secret:
            prefix = cfg["meta"].get("account_env_prefix", "TALON")
            raise CredentialsError(f"{prefix}_API_KEY / {prefix}_API_SECRET are not set in the environment")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json"}
        self._s = session or requests.Session()
        self._timeout = float(cfg["data"]["timeout_s"])
        self.known = list(cfg["universe"]["symbols"])

    @property
    def is_paper(self) -> bool:
        return self.base == PAPER_URL

    # ------------------------------------------------------------- transport
    def _req(self, method: str, path: str, **kw) -> dict | list:
        r = self._s.request(method, f"{self.base}{path}", headers=self._headers, timeout=self._timeout, **kw)
        if r.status_code >= 400:
            # body only — never the request headers
            raise RuntimeError(f"Alpaca {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        if not r.text:
            return {}
        return r.json()

    def _get(self, path: str, params: dict | None = None):
        return self._req("GET", path, params=params)

    def _post(self, path: str, body: dict):
        return self._req("POST", path, json=body)

    def _delete(self, path: str):
        return self._req("DELETE", path)

    # --------------------------------------------------------------- account
    def account(self) -> dict:
        return self._get("/v2/account")

    def equity(self) -> float:
        return float(self.account()["equity"])

    def positions(self) -> list[dict]:
        return list(self._get("/v2/positions"))

    def position_map(self) -> dict[str, dict]:
        """{'BTC/USD': {qty, market_value, avg_entry_price, current_price, unrealized_pl}}"""
        out = {}
        for p in self.positions():
            sym = with_slash(str(p.get("symbol", "")), self.known)
            out[sym] = {
                "qty": float(p.get("qty", 0) or 0),
                "market_value": float(p.get("market_value", 0) or 0),
                "avg_entry_price": float(p.get("avg_entry_price", 0) or 0),
                "current_price": float(p.get("current_price", 0) or 0),
                "unrealized_pl": float(p.get("unrealized_pl", 0) or 0),
                "raw_symbol": p.get("symbol"),
            }
        return out

    # ---------------------------------------------------------------- orders
    def buy_notional(self, symbol: str, notional: float) -> dict:
        body = {
            "symbol": with_slash(symbol, self.known),
            "notional": f"{float(notional):.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
        }
        return self._post("/v2/orders", body)

    def sell_qty(self, symbol: str, qty: float) -> dict:
        body = {
            "symbol": with_slash(symbol, self.known),
            "qty": f"{float(qty):.9f}".rstrip("0").rstrip("."),
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc",
        }
        return self._post("/v2/orders", body)

    def close_position(self, symbol: str) -> dict:
        """DELETE /v2/positions/{BTCUSD} — Alpaca wants the slash-less form in the path."""
        return self._delete(f"/v2/positions/{without_slash(symbol)}")

    def close_all(self) -> list:
        res = self._delete("/v2/positions?cancel_orders=true")
        return list(res) if isinstance(res, list) else [res]

    def order(self, order_id: str) -> dict:
        return self._get(f"/v2/orders/{order_id}")

    def fill_price(self, order_id: str, wait_s: float, polls: int) -> float | None:
        """Poll briefly for filled_avg_price; None if the order has not filled yet."""
        for _ in range(int(polls)):
            o = self.order(order_id)
            fap = o.get("filled_avg_price")
            if fap:
                return float(fap)
            time.sleep(float(wait_s))
        return None
