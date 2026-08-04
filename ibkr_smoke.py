#!/usr/bin/env python3
"""IBKR adapter integration smoke test (runs on GitHub Actions with real OAuth creds).

1. Auth + account summary reads
2. Positions + open orders read
3. Places a 1-share far-from-market bracket buy on SPY, verifies it appears,
   then cancels everything it created.

Exits non-zero on any failure. GLIDER may not go live until this passes.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker_ibkr import IBKRBroker


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="glider", choices=["scalpel", "glider"])
    args = ap.parse_args()
    b = IBKRBroker(args.bot.upper())

    acct = b.account()
    print("account:", acct)
    assert acct["equity"] > 0, "equity should be positive on a funded paper account"

    print("positions:", b.positions())
    print("open orders (before):", b.open_orders())

    # far-below-market bracket: should rest without filling
    ack = b.submit_bracket_buy("SPY", qty=1, limit_price=100.00,
                               stop_price=90.00, target_price=120.00, tif="day")
    print("placed:", ack)
    time.sleep(3)
    after = b.open_orders()
    print("open orders (after):", after)
    assert any(o["symbol"] == "SPY" for o in after), "test order not visible"

    b.cancel_all_orders()
    time.sleep(2)
    final = b.open_orders()
    print("open orders (final):", final)
    assert not any(o["symbol"] == "SPY" for o in final), "cancel failed"

    print("IBKR SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
