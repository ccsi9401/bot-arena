"""Execution adapters — the ONLY modules that place orders.

Two venues, one interface:
  AlpacaBroker — SCALPEL's venue (paper endpoint hard-coded)
  IBKRBroker   — GLIDER's venue (paper account, Web API via ibind/OAuth), broker_ibkr.py

Market DATA for both bots comes from core/data.py (Alpaca IEX), so scans are
identical across venues; only execution differs. Adapter contract:

  account() -> {equity, cash, last_equity, buying_power, status}
  positions() -> [{symbol, qty, avg_entry, market_value, unrealized_pl, current_price}]
  open_orders() -> [{id, symbol, side, qty, type, limit_price, order_class}]
  submit_bracket_buy(symbol, qty, limit_price, stop_price, target_price) -> ack
  replace_stop(order_id|symbol, new_stop) ; close_position(symbol)
  close_all() ; cancel_all_orders()
"""
from __future__ import annotations

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.trading.requests import (
    LimitOrderRequest, StopLossRequest, TakeProfitRequest,
    GetOrdersRequest, ReplaceOrderRequest,
)

from .common import ROOT

PAPER_ONLY = True  # never change


def _load_secrets_env() -> None:
    f = ROOT / "secrets.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def make_broker(cfg: dict):
    kind = cfg.get("execution_broker", "alpaca")
    if kind == "alpaca":
        return AlpacaBroker(cfg["account_env_prefix"])
    if kind == "ibkr":
        from .broker_ibkr import IBKRBroker
        return IBKRBroker(cfg["account_env_prefix"])
    raise ValueError(f"unknown execution_broker {kind!r}")


class AlpacaBroker:
    def __init__(self, env_prefix: str):
        _load_secrets_env()
        self.trading = TradingClient(os.environ[f"{env_prefix}_API_KEY"],
                                     os.environ[f"{env_prefix}_API_SECRET"], paper=True)

    def account(self) -> dict:
        a = self.trading.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash),
                "last_equity": float(a.last_equity),
                "buying_power": float(a.buying_power), "status": str(a.status)}

    def positions(self) -> list[dict]:
        return [{"symbol": p.symbol, "qty": float(p.qty),
                 "avg_entry": float(p.avg_entry_price),
                 "market_value": float(p.market_value),
                 "unrealized_pl": float(p.unrealized_pl),
                 "current_price": float(p.current_price)}
                for p in self.trading.get_all_positions()]

    def open_orders(self) -> list[dict]:
        orders = self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
        return [{"id": str(o.id), "symbol": o.symbol, "side": str(o.side),
                 "qty": float(o.qty or 0), "type": str(o.type),
                 "limit_price": float(o.limit_price) if o.limit_price else None,
                 "order_class": str(o.order_class) if o.order_class else None}
                for o in orders]

    def submit_bracket_buy(self, symbol: str, qty: int, limit_price: float,
                           stop_price: float, target_price: float,
                           tif: str = "day") -> dict:
        order = self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC if tif == "gtc" else TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            take_profit=TakeProfitRequest(limit_price=round(target_price, 2))))
        return {"id": str(order.id), "symbol": symbol, "qty": qty,
                "status": str(order.status)}

    def replace_stop(self, symbol: str, new_stop: float) -> dict:
        for o in self.open_orders():
            if o["symbol"] == symbol and "sell" in o["side"].lower() \
                    and o["type"].lower().endswith("stop"):
                r = self.trading.replace_order_by_id(
                    o["id"], ReplaceOrderRequest(stop_price=round(new_stop, 2)))
                return {"id": str(r.id), "status": str(r.status), "ok": True}
        return {"ok": False, "error": "no open stop order found"}

    def close_position(self, symbol: str) -> dict:
        self.trading.close_position(symbol, cancel_orders=True)
        return {"symbol": symbol, "closed": True}

    def close_all(self) -> None:
        self.trading.close_all_positions(cancel_orders=True)

    def cancel_all_orders(self) -> None:
        self.trading.cancel_orders()
