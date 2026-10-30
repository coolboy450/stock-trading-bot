"""Yahoo Finance provider (via ``yfinance``) - US, Thai (``.BK``), ETFs, indices, crypto (``BTC-USD``).

Free and keyless, but unofficial: data may be delayed ~15 min, rate limited, or
break when Yahoo changes its site. Intraday history is limited
(1m: ~7 days, 5m/15m: ~60 days, 1h: ~730 days).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from data.providers.base import DataProvider, DataProviderError, normalize_ohlcv
from utils.retry import retry


class YahooProvider(DataProvider):
    name = "yahoo"

    def __init__(self, max_retries: int = 3, retry_delay: float = 2.0, timeout: int = 20) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise DataProviderError("yfinance is not installed: pip install yfinance") from exc
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout

    def fetch_history(
        self,
        symbol: str,
        interval: str = "1d",
        period: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        @retry(exceptions=(Exception,), tries=self.max_retries, delay=self.retry_delay)
        def _download() -> pd.DataFrame:
            import yfinance as yf

            kwargs: dict = {"interval": interval, "auto_adjust": False, "actions": False, "timeout": self.timeout}
            if start is not None:
                kwargs["start"] = start
                if end is not None:
                    kwargs["end"] = end
            else:
                kwargs["period"] = period or "1mo"
            raw = yf.Ticker(symbol).history(**kwargs)
            if raw is None or raw.empty:
                raise DataProviderError(f"Yahoo returned no data for {symbol} ({interval})")
            return raw

        try:
            raw = _download()
        except DataProviderError:
            raise
        except Exception as exc:
            raise DataProviderError(f"Yahoo request failed for {symbol}: {exc}") from exc
        return normalize_ohlcv(raw)
