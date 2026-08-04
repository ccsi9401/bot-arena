"""Market data layer — Alpaca IEX for BOTH bots, regardless of execution venue.

One data source keeps the competition's scans identical; only execution differs.
Uses the SCALPEL account's keys (any Alpaca key pair can query the data API);
override with DATA_API_KEY / DATA_API_SECRET if desired.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient

from .common import UTC, now_et
from .broker import _load_secrets_env


class MarketData:
    def __init__(self):
        _load_secrets_env()
        key = os.environ.get("DATA_API_KEY") or os.environ["SCALPEL_API_KEY"]
        secret = os.environ.get("DATA_API_SECRET") or os.environ["SCALPEL_API_SECRET"]
        self.data = StockHistoricalDataClient(key, secret)
        self._clock_client = TradingClient(key, secret, paper=True)

    def market_open(self) -> bool:
        return bool(self._clock_client.get_clock().is_open)

    def daily_bars(self, symbols: list[str], days: int = 320) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
            start=datetime.now(tz=UTC) - timedelta(days=int(days * 1.6)),
            feed=DataFeed.IEX)
        return self.data.get_stock_bars(req).df

    def minute_bars_today(self, symbols: list[str]) -> pd.DataFrame:
        start_et = now_et().replace(hour=9, minute=30, second=0, microsecond=0)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start_et.astimezone(UTC), feed=DataFeed.IEX)
        return self.data.get_stock_bars(req).df

    def last_trades(self, symbols: list[str]) -> dict[str, float]:
        req = StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
        return {s: float(t.price)
                for s, t in self.data.get_stock_latest_trade(req).items()}
