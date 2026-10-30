"""Data-access helpers shared by the collector, engine, dashboard and CLI."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analysis.price_change import compute_changes
from database.models import (
    Direction,
    Order,
    OrderStatus,
    Portfolio,
    PortfolioSnapshot,
    PriceHistory,
    Signal,
    SignalType,
    Stock,
    Trade,
    TradeStatus,
    TradingMode,
)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def _float_or_none(value: object) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


# --------------------------------------------------------------------------- stocks
def get_stock(session: Session, symbol: str) -> Stock | None:
    return session.scalar(select(Stock).where(Stock.symbol == symbol.upper()))


def get_or_create_stock(
    session: Session,
    symbol: str,
    *,
    market: str = "US",
    name: str | None = None,
    sector: str | None = None,
    exchange: str | None = None,
    currency: str | None = None,
) -> Stock:
    """Return the stock row, creating it (or filling missing metadata) as needed."""
    stock = get_stock(session, symbol)
    if stock is None:
        stock = Stock(
            symbol=symbol.upper(), market=market, name=name, sector=sector, exchange=exchange, currency=currency
        )
        session.add(stock)
        session.flush()
    else:
        for attr, value in (("name", name), ("sector", sector), ("exchange", exchange), ("currency", currency)):
            if value and not getattr(stock, attr):
                setattr(stock, attr, value)
    return stock


# --------------------------------------------------------------------------- prices
def last_bar(session: Session, stock_id: int, interval: str, before: datetime | None = None) -> PriceHistory | None:
    stmt = select(PriceHistory).where(PriceHistory.stock_id == stock_id, PriceHistory.interval == interval)
    if before is not None:
        stmt = stmt.where(PriceHistory.timestamp < before)
    return session.scalar(stmt.order_by(PriceHistory.timestamp.desc()).limit(1))


def upsert_prices(session: Session, stock: Stock, df: pd.DataFrame, interval: str, source: str) -> int:
    """Insert/update OHLCV bars and (re)compute change / % change / direction.

    ``df`` must have a naive-UTC ``DatetimeIndex`` and OHLCV columns. Returns the
    number of bars inserted or updated.
    """
    if df is None or df.empty:
        return 0
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    start, end = df.index[0].to_pydatetime(), df.index[-1].to_pydatetime()

    existing = {
        row.timestamp: row
        for row in session.scalars(
            select(PriceHistory).where(
                PriceHistory.stock_id == stock.id,
                PriceHistory.interval == interval,
                PriceHistory.timestamp >= start,
                PriceHistory.timestamp <= end,
            )
        )
    }
    count = 0
    for ts, row in df.iterrows():
        ts_py = ts.to_pydatetime()
        values = {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0) or 0),
            "adj_close": None if pd.isna(row.get("adj_close")) else float(row.get("adj_close")),
            "source": source,
        }
        bar = existing.get(ts_py)
        if bar is None:
            session.add(PriceHistory(stock_id=stock.id, interval=interval, timestamp=ts_py, **values))
            count += 1
        elif any(getattr(bar, k) != v for k, v in values.items() if k != "source"):
            for k, v in values.items():
                setattr(bar, k, v)
            count += 1
    session.flush()
    recompute_changes(session, stock.id, interval, since=start)
    return count


def recompute_changes(session: Session, stock_id: int, interval: str, since: datetime) -> None:
    """Recalculate change columns for every bar at/after ``since``.

    Done after each upsert so back-filled history never leaves stale % changes.
    """
    prev = last_bar(session, stock_id, interval, before=since)
    bars = list(
        session.scalars(
            select(PriceHistory)
            .where(PriceHistory.stock_id == stock_id, PriceHistory.interval == interval, PriceHistory.timestamp >= since)
            .order_by(PriceHistory.timestamp)
        )
    )
    if not bars:
        return
    frame = pd.DataFrame({"close": [b.close for b in bars]}, index=pd.DatetimeIndex([b.timestamp for b in bars]))
    changed = compute_changes(frame, prev_close=prev.close if prev else None)
    for bar, pc, ch, pct, direction in zip(
        bars, changed["prev_close"], changed["change"], changed["percent_change"], changed["direction"]
    ):
        bar.prev_close, bar.change, bar.percent_change = _float_or_none(pc), _float_or_none(ch), _float_or_none(pct)
        bar.direction = direction if isinstance(direction, Direction) else None
    session.flush()


def get_price_df(
    session: Session,
    stock_id: int,
    interval: str,
    *,
    limit: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Load bars as a DataFrame indexed by timestamp (ascending)."""
    stmt = select(PriceHistory).where(PriceHistory.stock_id == stock_id, PriceHistory.interval == interval)
    if start is not None:
        stmt = stmt.where(PriceHistory.timestamp >= start)
    if end is not None:
        stmt = stmt.where(PriceHistory.timestamp <= end)
    stmt = stmt.order_by(PriceHistory.timestamp.desc())
    if limit:
        stmt = stmt.limit(limit)
    rows = list(session.scalars(stmt))[::-1]
    if not rows:
        return pd.DataFrame(columns=[*OHLCV_COLUMNS, "percent_change"])
    df = pd.DataFrame(
        {
            "open": [r.open for r in rows],
            "high": [r.high for r in rows],
            "low": [r.low for r in rows],
            "close": [r.close for r in rows],
            "volume": [r.volume for r in rows],
            "percent_change": [r.percent_change for r in rows],
            "direction": [r.direction.value if r.direction else None for r in rows],
        },
        index=pd.DatetimeIndex([r.timestamp for r in rows], name="timestamp"),
    )
    return df


def latest_close(session: Session, stock_id: int, interval: str | None = None) -> float | None:
    stmt = select(PriceHistory.close).where(PriceHistory.stock_id == stock_id)
    if interval:
        stmt = stmt.where(PriceHistory.interval == interval)
    return session.scalar(stmt.order_by(PriceHistory.timestamp.desc()).limit(1))


# --------------------------------------------------------------------------- signals
def save_signal(
    session: Session,
    *,
    stock: Stock,
    strategy: str,
    signal_type: SignalType,
    interval: str,
    timestamp: datetime,
    price: float,
    reason: str | None,
    indicators: dict[str, Any] | None = None,
) -> tuple[Signal, bool]:
    """Store a signal once per (stock, strategy, interval, bar). Returns (signal, created)."""
    existing = session.scalar(
        select(Signal).where(
            Signal.stock_id == stock.id,
            Signal.strategy == strategy,
            Signal.interval == interval,
            Signal.timestamp == timestamp,
        )
    )
    if existing is not None:
        return existing, False
    sig = Signal(
        stock_id=stock.id,
        strategy=strategy,
        signal_type=signal_type,
        interval=interval,
        timestamp=timestamp,
        price=price,
        reason=reason,
        indicators=indicators,
    )
    session.add(sig)
    session.flush()
    return sig, True


# --------------------------------------------------------------------------- portfolio
def get_or_create_portfolio(
    session: Session, name: str, *, mode: TradingMode, initial_capital: float, currency: str = "USD"
) -> Portfolio:
    pf = session.scalar(select(Portfolio).where(Portfolio.name == name))
    if pf is None:
        pf = Portfolio(
            name=name,
            mode=mode,
            currency=currency,
            initial_capital=initial_capital,
            cash=initial_capital,
            equity=initial_capital,
            peak_equity=initial_capital,
        )
        session.add(pf)
        session.flush()
    elif pf.mode != mode:
        raise ValueError(
            f"Portfolio '{name}' is {pf.mode.value} but config requests {mode.value}. "
            "Use a different execution.portfolio_name for each mode."
        )
    return pf


def open_trades(session: Session, portfolio_id: int) -> list[Trade]:
    return list(
        session.scalars(
            select(Trade).where(Trade.portfolio_id == portfolio_id, Trade.status == TradeStatus.OPEN).order_by(Trade.id)
        )
    )


def open_trade_for(session: Session, portfolio_id: int, stock_id: int) -> Trade | None:
    return session.scalar(
        select(Trade).where(
            Trade.portfolio_id == portfolio_id, Trade.stock_id == stock_id, Trade.status == TradeStatus.OPEN
        )
    )


def pending_orders(session: Session, portfolio_id: int) -> list[Order]:
    return list(
        session.scalars(
            select(Order).where(Order.portfolio_id == portfolio_id, Order.status == OrderStatus.PENDING).order_by(Order.id)
        )
    )


def realized_pnl_since(session: Session, portfolio_id: int, since: datetime) -> float:
    value = session.scalar(
        select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
            Trade.portfolio_id == portfolio_id, Trade.status == TradeStatus.CLOSED, Trade.exit_time >= since
        )
    )
    return float(value or 0.0)


def equity_at_start_of(session: Session, portfolio: Portfolio, day_start_utc: datetime) -> float:
    """Equity of the last snapshot before ``day_start_utc`` (or initial capital)."""
    snap = session.scalar(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.portfolio_id == portfolio.id, PortfolioSnapshot.timestamp < day_start_utc)
        .order_by(PortfolioSnapshot.timestamp.desc())
        .limit(1)
    )
    return snap.equity if snap else portfolio.initial_capital


def snapshots_df(session: Session, portfolio_id: int, since: datetime | None = None) -> pd.DataFrame:
    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == portfolio_id)
    if since:
        stmt = stmt.where(PortfolioSnapshot.timestamp >= since)
    rows = list(session.scalars(stmt.order_by(PortfolioSnapshot.timestamp)))
    if not rows:
        return pd.DataFrame(columns=["equity", "cash", "positions_value", "drawdown_pct"])
    return pd.DataFrame(
        {
            "equity": [r.equity for r in rows],
            "cash": [r.cash for r in rows],
            "positions_value": [r.positions_value for r in rows],
            "drawdown_pct": [r.drawdown_pct for r in rows],
        },
        index=pd.DatetimeIndex([r.timestamp for r in rows], name="timestamp"),
    )


def period_pnl(session: Session, portfolio: Portfolio, freq: str = "D") -> pd.DataFrame:
    """PnL per period (``D`` daily, ``M`` monthly) from the equity curve."""
    snaps = snapshots_df(session, portfolio.id)
    if snaps.empty:
        return pd.DataFrame(columns=["period", "equity", "pnl", "pnl_pct"])
    rule = "ME" if freq.upper().startswith("M") else "D"
    closing = snaps["equity"].resample(rule).last().dropna()
    prev = closing.shift(1)
    prev.iloc[0] = portfolio.initial_capital
    out = pd.DataFrame({"equity": closing, "pnl": closing - prev, "pnl_pct": (closing / prev - 1) * 100})
    fmt = "%Y-%m" if rule == "ME" else "%Y-%m-%d"
    out.insert(0, "period", [ts.strftime(fmt) for ts in out.index])
    return out.reset_index(drop=True)


def day_bounds_utc(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """UTC naive [start, end) of a calendar day in the given timezone."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    utc = ZoneInfo("UTC")
    return start_local.astimezone(utc).replace(tzinfo=None), end_local.astimezone(utc).replace(tzinfo=None)
