"""Fetches prices from providers and stores them (with up/down % change) in the DB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from loguru import logger
from sqlalchemy.orm import Session

from config.settings import Settings, WatchItem
from data.providers import DataProvider, DataProviderError
from database import repository as repo
from database.models import Stock

# How far to re-fetch before the last stored bar (catches late corrections).
_OVERLAP = {"1d": timedelta(days=5), "1h": timedelta(days=2)}
_DEFAULT_OVERLAP = timedelta(hours=6)


@dataclass
class CollectResult:
    symbol: str
    interval: str
    bars_written: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class PriceCollector:
    def __init__(self, settings: Settings, provider: DataProvider) -> None:
        self.settings = settings
        self.provider = provider

    def ensure_stock(self, session: Session, item: WatchItem) -> Stock:
        market = self.settings.market_for(item)
        return repo.get_or_create_stock(
            session,
            item.symbol,
            market=item.market,
            name=item.name,
            sector=item.sector,
            exchange=item.exchange,
            currency=market.currency,
        )

    def backfill(self, session: Session, item: WatchItem, interval: str | None = None, period: str | None = None) -> CollectResult:
        """Download full history (default ``data.history_period``, e.g. 2 years)."""
        interval = interval or self.settings.data.interval
        return self._collect(session, item, interval, period=period or self.settings.data.history_period)

    def update(self, session: Session, item: WatchItem, interval: str | None = None) -> CollectResult:
        """Incremental update: fetch from shortly before the last stored bar."""
        interval = interval or self.settings.data.interval
        stock = self.ensure_stock(session, item)
        last = repo.last_bar(session, stock.id, interval)
        if last is None:
            return self._collect(session, item, interval, period=self._initial_period(interval))
        start = last.timestamp - _OVERLAP.get(interval, _DEFAULT_OVERLAP)
        return self._collect(session, item, interval, start=start)

    def _initial_period(self, interval: str) -> str:
        # Yahoo intraday limits: 1m ~7d, <1h ~60d.
        if interval == "1m":
            return "5d"
        if interval in {"5m", "15m", "30m"}:
            return "30d"
        if interval in {"1h", "60m"}:
            return "6mo"
        return self.settings.data.history_period

    def _collect(self, session: Session, item: WatchItem, interval: str, **fetch_kwargs) -> CollectResult:
        stock = self.ensure_stock(session, item)
        try:
            df = self.provider.fetch_history(item.symbol, interval=interval, **fetch_kwargs)
        except DataProviderError as exc:
            logger.error("Price fetch failed for {} [{}]: {}", item.symbol, interval, exc)
            return CollectResult(item.symbol, interval, 0, error=str(exc))
        source = getattr(self.provider, "last_source", None) or self.provider.name
        written = repo.upsert_prices(session, stock, df, interval, source)
        logger.info("{} [{}]: {} bars fetched, {} written (source={})", item.symbol, interval, len(df), written, source)
        return CollectResult(item.symbol, interval, written)
