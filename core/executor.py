"""Stage 4 — Executor. Zero discretion: places exactly what the validator approved."""
from __future__ import annotations

def execute(approved: list[dict], broker, tif: str = "day") -> dict:
    results = []
    for order in approved:
        action = order["action"]
        try:
            if action == "liquidate_all":
                broker.cancel_all_orders()
                broker.close_all()
                results.append({"action": action, "ok": True})
            elif action == "close":
                results.append({**broker.close_position(order["symbol"]),
                                "action": action, "ok": True})
            elif action == "raise_stop":
                res = broker.replace_stop(order["symbol"], order["new_stop"])
                results.append({**res, "action": action, "symbol": order["symbol"]})
            elif action == "buy":
                ack = broker.submit_bracket_buy(
                    symbol=order["symbol"], qty=order["qty"],
                    limit_price=order["entry_limit"],
                    stop_price=order["stop"], target_price=order["target"],
                    tif=tif,
                )
                results.append({**ack, "action": action, "ok": True})
        except Exception as e:  # record, never raise — one bad order can't stop the rest
            results.append({"action": action, "symbol": order.get("symbol"),
                            "ok": False, "error": f"{type(e).__name__}: {e}"})
    return {"results": results,
            "placed": sum(1 for x in results if x.get("ok")),
            "failed": sum(1 for x in results if not x.get("ok"))}

