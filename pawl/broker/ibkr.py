"""
IBKR crypto -- evaluated and REJECTED for this bot. Kept here so the reasoning
survives, and so nobody re-litigates it from the fee table alone.

Headline: 0.12%-0.18% of trade value, USD 1.75 minimum per order. That is
roughly HALF Alpaca's cost, and on a pure fee comparison IBKR wins outright.
The universe is good too -- 20 assets including BTC, ETH, SOL, LINK, AVAX,
AAVE, UNI, LTC, BCH, DOGE, XRP, ADA, NEAR, APT, SUI.

The disqualifier is the order model, not the price. Per IBKR's own TWS API
documentation, crypto supports MARKET and LIMIT only. Time in force is IOC, or
for limits a 5-minute expiry. There are no stop orders, and there is no GTC.

That means you cannot leave a protective order resting at the exchange. At all.
PAWL's entire risk design is a broker-resident floor that survives the bot
being dead; on IBKR the floor would collapse back to a bot-side poll, and a
bot-side poll on a 4-hour GitHub Actions schedule is not protection, it is a
rumour of protection. Halving the commission does not buy that back.

Second, smaller problem: the USD 1.75 per-order minimum is 0.35% on a $500
order. At small position sizes IBKR is not actually cheaper than Alpaca.

Revisit if IBKR ever exposes stop orders with GTC on crypto.
"""
from .base import Capabilities

IBKR_CAPS = Capabilities(
    name="Interactive Brokers crypto",
    resting_stop=False,          # <-- the disqualifier
    good_till_cancelled=False,   # IOC, or 5-minute limits
    shorting=False, perpetuals=False, native_trailing_stop=False,
    taker_fee=0.0018, maker_fee=0.0018, paper_supported=True,
    notes="Cheapest headline cost (0.12-0.18%, $1.75/order min) but market and "
          "limit orders ONLY, IOC or 5-minute TIF. No resting protective order is "
          "possible, so the floor cannot live at the exchange. Rejected on that basis.",
)
