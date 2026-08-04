"""IBKR paper execution adapter — Client Portal Web API via `ibind` with OAuth 1.0a.

Headless: no TWS, no IB Gateway. Requires first-party OAuth self-service setup in
IBKR Client Portal (consumer key, signing/encryption RSA keys, DH prime, access
token + secret). BOTH bots run on IBKR under separate usernames, so credentials
are namespaced per bot prefix (set as GitHub Actions secrets):

  {PREFIX}_IBKR_ACCOUNT_ID           e.g. DUH123456 (paper account id)
  {PREFIX}_IBKR_CONSUMER_KEY
  {PREFIX}_IBKR_ACCESS_TOKEN
  {PREFIX}_IBKR_ACCESS_TOKEN_SECRET
  {PREFIX}_IBKR_DH_PRIME
  {PREFIX}_IBKR_ENCRYPTION_KEY_FP    path to that user's private_encryption.pem
  {PREFIX}_IBKR_SIGNATURE_KEY_FP     path to that user's private_signature.pem

The adapter maps prefix vars onto ibind's IBIND_OAUTH1A_* env vars right before
client construction (sequential construction is safe; each workflow runs one bot,
and the scoreboard builds brokers one at a time).

NOTE: exact env names follow the installed ibind version's docs; verified by the
`ibkr-smoke` workflow before either bot is allowed to go live. Bracket = parent
limit BUY with attached STP + LMT sell children (parentId), the CP API's shape.
"""
from __future__ import annotations

import os
import time

from ibind import IbkrClient, make_order_request, QuestionType

_OAUTH_FIELDS = ["CONSUMER_KEY", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET",
                 "DH_PRIME", "ENCRYPTION_KEY_FP", "SIGNATURE_KEY_FP"]


class IBKRBroker:
    def __init__(self, env_prefix: str):
        self.account_id = os.environ[f"{env_prefix}_IBKR_ACCOUNT_ID"]
        # map this bot's namespaced creds onto ibind's global IBIND_* env vars
        os.environ["IBIND_USE_OAUTH"] = "true"
        for field in _OAUTH_FIELDS:
            val = os.environ.get(f"{env_prefix}_IBKR_{field}")
            if val is not None:
                os.environ[f"IBIND_OAUTH1A_{field}"] = val
        self.c = IbkrClient(use_oauth=True, account_id=self.account_id)
        # initialize a brokerage session for trading endpoints
        try:
            self.c.initialize_brokerage_session()
        except Exception:
            pass  # some ibind versions do this lazily

    # ---- answers to IBKR's order-confirmation prompts (paper): accept all ----
    ANSWERS = {
        QuestionType.PRICE_PERCENTAGE_CONSTRAINT: True,
        QuestionType.ORDER_VALUE_LIMIT: True,
        QuestionType.MISSING_MARKET_DATA: True,
        QuestionType.STOP_ORDER_RISKS: True,
    }

    def _conid(self, symbol: str) -> int:
        res = self.c.stock_conid_by_symbol(symbol).data
        return int(res[symbol])

    def account(self) -> dict:
        led = self.c.get_ledger(self.account_id).data.get("USD", {})
        summary = self.c.portfolio_summary(self.account_id).data \
            if hasattr(self.c, "portfolio_summary") else {}
        equity = float(led.get("netliquidationvalue") or
                       summary.get("networth", {}).get("amount", 0))
        cash = float(led.get("cashbalance", 0))
        return {"equity": equity, "cash": cash,
                "last_equity": equity - float(led.get("netliquidationvalue_change", 0) or 0),
                "buying_power": cash, "status": "ACTIVE"}

    def positions(self) -> list[dict]:
        out = []
        for p in (self.c.positions(self.account_id).data or []):
            if float(p.get("position", 0)) == 0:
                continue
            qty = float(p["position"])
            px = float(p.get("mktPrice", 0))
            avg = float(p.get("avgCost", 0))
            out.append({"symbol": p.get("contractDesc", p.get("ticker", "?")).split()[0],
                        "qty": qty, "avg_entry": avg,
                        "market_value": float(p.get("mktValue", qty * px)),
                        "unrealized_pl": float(p.get("unrealizedPnl", 0)),
                        "current_price": px})
        return out

    def open_orders(self) -> list[dict]:
        res = self.c.live_orders(force=True).data or {}
        out = []
        for o in res.get("orders", []):
            if str(o.get("status", "")).lower() in ("filled", "cancelled", "inactive"):
                continue
            out.append({"id": str(o["orderId"]), "symbol": o.get("ticker", "?"),
                        "side": str(o.get("side", "")).lower(),
                        "qty": float(o.get("totalSize", 0)),
                        "type": str(o.get("origOrderType", o.get("orderType", ""))).lower(),
                        "limit_price": float(o["price"]) if o.get("price") else None,
                        "order_class": "bracket" if o.get("parentId") else None})
        return out

    def submit_bracket_buy(self, symbol: str, qty: int, limit_price: float,
                           stop_price: float, target_price: float,
                           tif: str = "day") -> dict:
        conid = self._conid(symbol)
        tag = f"arena-{symbol}-{int(time.time())}"
        parent = make_order_request(conid=conid, side="BUY", quantity=qty,
                                    order_type="LMT", price=round(limit_price, 2),
                                    acct_id=self.account_id, coid=tag,
                                    tif=tif.upper())
        stop = make_order_request(conid=conid, side="SELL", quantity=qty,
                                  order_type="STP", price=round(stop_price, 2),
                                  acct_id=self.account_id, parent_id=tag, tif="GTC")
        target = make_order_request(conid=conid, side="SELL", quantity=qty,
                                    order_type="LMT", price=round(target_price, 2),
                                    acct_id=self.account_id, parent_id=tag, tif="GTC")
        res = self.c.place_order([parent, stop, target], self.ANSWERS, self.account_id).data
        oid = None
        try:
            oid = res[0].get("order_id") or res[0].get("id")
        except Exception:
            pass
        return {"id": str(oid), "symbol": symbol, "qty": qty, "status": "submitted"}

    def replace_stop(self, symbol: str, new_stop: float) -> dict:
        for o in self.open_orders():
            if o["symbol"] == symbol and o["side"] == "sell" and "stp" in o["type"]:
                r = self.c.modify_order(
                    order_id=o["id"], account_id=self.account_id,
                    order_request={"price": round(new_stop, 2)}, answers=self.ANSWERS).data
                return {"id": o["id"], "status": "replaced", "ok": True, "raw": r}
        return {"ok": False, "error": "no open stop order found"}

    def close_position(self, symbol: str) -> dict:
        # cancel this symbol's working orders, then market-sell the position
        for o in self.open_orders():
            if o["symbol"] == symbol:
                try:
                    self.c.cancel_order(o["id"], self.account_id)
                except Exception:
                    pass
        for p in self.positions():
            if p["symbol"] == symbol and p["qty"] > 0:
                req = make_order_request(conid=self._conid(symbol), side="SELL",
                                         quantity=int(p["qty"]), order_type="MKT",
                                         acct_id=self.account_id,
                                         coid=f"close-{symbol}-{int(time.time())}",
                                         tif="DAY")
                self.c.place_order(req, self.ANSWERS, self.account_id)
        return {"symbol": symbol, "closed": True}

    def close_all(self) -> None:
        self.cancel_all_orders()
        for p in self.positions():
            if p["qty"] > 0:
                self.close_position(p["symbol"])

    def cancel_all_orders(self) -> None:
        for o in self.open_orders():
            try:
                self.c.cancel_order(o["id"], self.account_id)
            except Exception:
                pass
