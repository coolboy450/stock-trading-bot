"""Daily per-stock up/down summary (``daily_summary`` table) + text report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.price_change import classify_direction
from config.settings import Settings
from database import repository as repo
from database.models import DailySummary, Direction, PriceHistory, Signal, Stock

_INTRADAY_PREFERENCE = ["1h", "60m", "30m", "15m", "5m", "1m"]


@dataclass
class SummaryRow:
    symbol: str
    date: date
    close: float
    percent_change: float | None
    direction: Direction | None
    volume: float


def _bars_in_window(s: Session, stock_id: int, interval: str, start: datetime, end: datetime) -> list[PriceHistory]:
    return list(
        s.scalars(
            select(PriceHistory)
            .where(
                PriceHistory.stock_id == stock_id,
                PriceHistory.interval == interval,
                PriceHistory.timestamp >= start,
                PriceHistory.timestamp < end,
            )
            .order_by(PriceHistory.timestamp)
        )
    )


def latest_trading_date(s: Session, stock: Stock, tz: str) -> date | None:
    bar = repo.last_bar(s, stock.id, "1d")
    if bar is None:
        bar = s.scalar(
            select(PriceHistory).where(PriceHistory.stock_id == stock.id).order_by(PriceHistory.timestamp.desc()).limit(1)
        )
    if bar is None:
        return None
    return bar.timestamp.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).date()


def summarize_stock(s: Session, stock: Stock, day: date, tz: str) -> DailySummary | None:
    """Build/update the summary row for ``stock`` on local date ``day``.

    Uses the daily bar if present, otherwise aggregates intraday bars.
    """
    start, end = repo.day_bounds_utc(day, tz)
    bars = _bars_in_window(s, stock.id, "1d", start, end)
    interval = "1d"
    if not bars:
        for iv in _INTRADAY_PREFERENCE:
            bars = _bars_in_window(s, stock.id, iv, start, end)
            if bars:
                interval = iv
                break
    if not bars:
        return None

    prev = repo.last_bar(s, stock.id, "1d", before=start) or repo.last_bar(s, stock.id, interval, before=start)
    prev_close = prev.close if prev else bars[0].prev_close
    open_, close = bars[0].open, bars[-1].close
    high, low = max(b.high for b in bars), min(b.low for b in bars)
    change = close - prev_close if prev_close else None
    pct = change / prev_close * 100 if prev_close else None

    last_sig = s.scalar(
        select(Signal)
        .where(Signal.stock_id == stock.id, Signal.timestamp >= start, Signal.timestamp < end)
        .order_by(Signal.timestamp.desc())
        .limit(1)
    )
    row = s.scalar(select(DailySummary).where(DailySummary.stock_id == stock.id, DailySummary.date == day))
    if row is None:
        row = DailySummary(stock_id=stock.id, date=day, open=open_, high=high, low=low, close=close)
        s.add(row)
    row.prev_close, row.open, row.high, row.low, row.close = prev_close, open_, high, low, close
    row.volume = sum(b.volume for b in bars)
    row.change, row.percent_change = change, pct
    row.direction = classify_direction(pct)
    row.intraday_range_pct = (high - low) / low * 100 if low else None
    row.last_signal = last_sig.signal_type.value if last_sig else None
    s.flush()
    return row


def build_daily_summary(s: Session, settings: Settings, day: date | None = None) -> list[SummaryRow]:
    """Summarise every watchlist stock for ``day`` (default: each stock's latest trading day)."""
    rows: list[SummaryRow] = []
    for item in settings.watchlist:
        stock = repo.get_stock(s, item.symbol)
        if stock is None:
            continue
        tz = settings.market_for(item).timezone
        target = day or latest_trading_date(s, stock, tz)
        if target is None:
            continue
        summary = summarize_stock(s, stock, target, tz)
        if summary is not None:
            rows.append(SummaryRow(stock.symbol, target, summary.close, summary.percent_change,
                                   summary.direction, summary.volume))
    return rows


def format_report(rows: list[SummaryRow]) -> str:
    if not rows:
        return "No price data for the requested day."
    ups = [r for r in rows if r.direction == Direction.UP]
    downs = [r for r in rows if r.direction == Direction.DOWN]
    ranked = sorted(rows, key=lambda r: r.percent_change if r.percent_change is not None else 0, reverse=True)
    lines = [f"Daily summary {rows[0].date:%Y-%m-%d}: {len(ups)} up / {len(downs)} down / "
             f"{len(rows) - len(ups) - len(downs)} flat"]
    for r in ranked:
        arrow = {"UP": "▲ ขึ้น", "DOWN": "▼ ลง"}.get(r.direction.value if r.direction else "", "■ ทรงตัว")
        pct = f"{r.percent_change:+.2f}%" if r.percent_change is not None else "n/a"
        lines.append(f"{r.symbol:<12} {r.close:>12,.2f}  {pct:>8}  {arrow}")
    return "\n".join(lines)
