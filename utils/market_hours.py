"""Market session helpers (weekday, sessions, holidays)."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from config.settings import MarketSettings


def _parse(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def is_trading_day(market: MarketSettings, now: datetime | None = None) -> bool:
    local = _local_now(market, now)
    return local.weekday() in market.trading_days and local.date().isoformat() not in set(market.holidays)


def is_market_open(market: MarketSettings, now: datetime | None = None) -> bool:
    """True if ``now`` (aware, or naive UTC) is inside a trading session."""
    local = _local_now(market, now)
    if not is_trading_day(market, local):
        return False
    t = local.time()
    return any(_parse(start) <= t <= _parse(end) for start, end in market.sessions)


def _local_now(market: MarketSettings, now: datetime | None) -> datetime:
    tz = ZoneInfo(market.timezone)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(tz)
