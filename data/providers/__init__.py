"""Data providers + factory with automatic fallback."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from loguru import logger

from config.settings import DataSettings
from data.providers.base import DataProvider, DataProviderError, normalize_ohlcv
from data.providers.synthetic import SyntheticProvider


class FallbackProvider(DataProvider):
    """Tries each provider in order until one returns data."""

    def __init__(self, providers: list[DataProvider]) -> None:
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.last_source: str | None = None

    def fetch_history(
        self,
        symbol: str,
        interval: str = "1d",
        period: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        errors: list[str] = []
        for provider in self.providers:
            try:
                df = provider.fetch_history(symbol, interval=interval, period=period, start=start, end=end)
                if not df.empty:
                    self.last_source = provider.name
                    return df
                errors.append(f"{provider.name}: empty")
            except DataProviderError as exc:
                logger.warning("Provider {} failed for {}: {}", provider.name, symbol, exc)
                errors.append(f"{provider.name}: {exc}")
        raise DataProviderError(f"All providers failed for {symbol}: {'; '.join(errors)}")


def _build_one(name: str, settings: DataSettings) -> DataProvider:
    if name == "yahoo":
        from data.providers.yahoo import YahooProvider

        return YahooProvider(settings.max_retries, settings.retry_delay_seconds, settings.request_timeout)
    if name == "alpha_vantage":
        from data.providers.alpha_vantage import AlphaVantageProvider

        return AlphaVantageProvider(
            settings.alpha_vantage_api_key,
            outputsize=settings.alpha_vantage_outputsize,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
            retry_delay=settings.retry_delay_seconds,
        )
    if name == "synthetic":
        return SyntheticProvider()
    raise ValueError(f"Unknown data provider: {name}")


def build_provider(settings: DataSettings) -> FallbackProvider:
    """Primary provider followed by configured fallbacks (misconfigured ones are skipped)."""
    providers: list[DataProvider] = []
    for name in [settings.provider, *settings.fallback_providers]:
        try:
            providers.append(_build_one(name, settings))
        except DataProviderError as exc:
            logger.warning("Skipping provider {}: {}", name, exc)
    return FallbackProvider(providers)


__all__ = ["DataProvider", "DataProviderError", "FallbackProvider", "SyntheticProvider", "build_provider", "normalize_ohlcv"]
