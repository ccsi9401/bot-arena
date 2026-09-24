from .base import Broker, BrokerError, Capabilities


def get_broker(name: str, paper: bool = True, **kw) -> Broker:
    n = (name or "alpaca").lower()
    if n == "alpaca":
        from .alpaca import AlpacaBroker
        return AlpacaBroker(paper=paper, **kw)
    if n in ("kraken", "kraken_spot"):
        from .kraken import KrakenSpotBroker
        return KrakenSpotBroker(**kw)
    if n in ("kraken_us_perps", "kraken_derivatives_us"):
        from .kraken import KrakenUSPerpsBroker
        return KrakenUSPerpsBroker(**kw)
    if n == "sim":
        from .sim import SimBroker
        return SimBroker(**kw)
    raise BrokerError(f"unknown broker '{name}'")


def capabilities(name: str) -> Capabilities:
    """Read a venue's capability sheet without opening an account or a socket.
    Used by `run_pawl.py venues` and by the gate's cost model."""
    from .alpaca import ALPACA_CAPS
    from .kraken import KRAKEN_SPOT_CAPS, KRAKEN_US_PERPS_CAPS
    from .ibkr import IBKR_CAPS
    from .sim import SIM_CAPS
    table = {
        "alpaca": ALPACA_CAPS, "kraken": KRAKEN_SPOT_CAPS,
        "kraken_us_perps": KRAKEN_US_PERPS_CAPS, "ibkr": IBKR_CAPS, "sim": SIM_CAPS,
    }
    key = (name or "alpaca").lower()
    if key not in table:
        raise BrokerError(f"unknown venue '{name}'")
    return table[key]


ALL_VENUES = ["alpaca", "kraken", "kraken_us_perps", "ibkr", "sim"]
