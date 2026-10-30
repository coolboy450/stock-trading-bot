"""Alpha Vantage provider (https://www.alphavantage.co). Requires a (free) API key.

The free tier is heavily rate-limited (a few requests per minute / ~25 per day),
so prefer it as a fallback rather than the primary source.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import requests

from data.providers.base import DataProvider, DataProviderError, normalize_ohlcv
from utils.retry import retry

BASE_URL = "https://www.alphavantage.co/query"
_INTRADAY = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min", "60m": "60min"}


class AlphaVantageProvider(DataProvider):
    name = "alpha_vantage"

    def __init__(
        self,
        api_key: str | None,
        outputsize: str = "compact",
        timeout: int = 20,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise DataProviderError("Alpha Vantage requires data.alpha_vantage_api_key (ALPHA_VANTAGE_API_KEY)")
        self.api_key = api_key
        self.outputsize = outputsize
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.http = session or requests.Session()

    def _get(self, params: dict) -> dict:
        @retry(exceptions=(requests.RequestException,), tries=self.max_retries, delay=self.retry_delay)
        def _call() -> dict:
            resp = self.http.get(BASE_URL, params={**params, "apikey": self.api_key}, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()

        try:
            payload = _call()
        except requests.RequestException as exc:
            raise DataProviderError(f"Alpha Vantage request failed: {exc}") from exc
        for key in ("Error Message", "Note", "Information"):
            if key in payload:
                raise DataProviderError(f"Alpha Vantage: {payload[key]}")
        return payload

    def fetch_history(
        self,
        symbol: str,
        interval: str = "1d",
        period: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        if interval in _INTRADAY:
            params = {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": _INTRADAY[interval],
                "outputsize": self.outputsize,
            }
        elif interval == "1d":
            params = {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": self.outputsize}
        else:
            raise DataProviderError(f"Alpha Vantage does not support interval {interval}")

        payload = self._get(params)
        series_key = next((k for k in payload if k.startswith("Time Series")), None)
        if series_key is None:
            raise DataProviderError(f"Alpha Vantage returned no series for {symbol}")
        df = pd.DataFrame.from_dict(payload[series_key], orient="index")
        df = df.rename(columns=lambda c: c.split(". ", 1)[-1])
        # Timestamps are US/Eastern for US equities.
        df.index = pd.to_datetime(df.index).tz_localize("America/New_York")
        df = normalize_ohlcv(df)
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df
