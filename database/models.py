"""SQLAlchemy ORM models (database schema).

All timestamps are stored as **naive UTC** datetimes so the schema behaves the
same on SQLite and PostgreSQL. Convert to market local time only for display.

Tables
------
stocks              master list of instruments
price_history       OHLCV bars + change / percent_change / direction (UP/DOWN)
signals             BUY / SELL / HOLD decisions produced by strategies
orders              every order sent to a broker (paper or live) and its result
trades              round-trip positions (OPEN -> CLOSED) with PnL
portfolios          current account state: cash, positions value, equity
portfolio_snapshots equity curve over time (daily / monthly PnL, drawdown)
daily_summary       per-stock daily up/down summary
backtest_runs       stored backtest results (params + metrics)
alert_logs          every alert sent (or failed)
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (the storage convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base for all models."""


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK so SQLite and PostgreSQL behave identically.
    return Enum(enum_cls, name=name, native_enum=False, validate_strings=True, length=20)


class Direction(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"
    UNCHANGED = "UNCHANGED"


class SignalType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TradingMode(str, enum.Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class ExitReason(str, enum.Enum):
    SIGNAL = "SIGNAL"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"


class Stock(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="US")
    exchange: Mapped[str | None] = mapped_column(String(32))
    sector: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str | None] = mapped_column(String(8))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    prices: Mapped[list[PriceHistory]] = relationship(back_populates="stock", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Stock {self.symbol} ({self.market})>"


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("stock_id", "interval", "timestamp", name="uq_price_stock_interval_ts"),
        Index("ix_price_stock_interval_ts", "stock_id", "interval", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    prev_close: Mapped[float | None] = mapped_column(Float)
    change: Mapped[float | None] = mapped_column(Float)  # close - prev_close
    percent_change: Mapped[float | None] = mapped_column(Float)  # % vs prev_close
    direction: Mapped[Direction | None] = mapped_column(_enum(Direction, "direction"))
    source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    stock: Mapped[Stock] = relationship(back_populates="prices")


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_stock_ts", "stock_id", "timestamp"),
        UniqueConstraint("stock_id", "strategy", "interval", "timestamp", name="uq_signal_bar"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[SignalType] = mapped_column(_enum(SignalType, "signal_type"), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # bar time
    price: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    indicators: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    acted_on: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    action_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    stock: Mapped[Stock] = relationship()


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    peak_equity: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    trades: Mapped[list[Trade]] = relationship(back_populates="portfolio")

    @property
    def drawdown_pct(self) -> float:
        return 0.0 if self.peak_equity <= 0 else max(0.0, (self.peak_equity - self.equity) / self.peak_equity)


class Trade(Base):
    """A round-trip position. ``status=OPEN`` rows are the current positions."""

    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_portfolio_status", "portfolio_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    strategy_used: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    status: Mapped[TradeStatus] = mapped_column(_enum(TradeStatus, "trade_status"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    entry_commission: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    exit_commission: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    pnl: Mapped[float | None] = mapped_column(Float)  # net of commissions
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[ExitReason | None] = mapped_column(_enum(ExitReason, "exit_reason"))
    notes: Mapped[str | None] = mapped_column(Text)

    portfolio: Mapped[Portfolio] = relationship(back_populates="trades")
    stock: Mapped[Stock] = relationship()
    orders: Mapped[list[Order]] = relationship(back_populates="trade")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_portfolio_status", "portfolio_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    mode: Mapped[TradingMode] = mapped_column(_enum(TradingMode, "trading_mode"), nullable=False)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType, "order_type"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float)
    reference_price: Mapped[float | None] = mapped_column(Float)  # market price when submitted
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus, "order_status"), nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    filled_price: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0)  # cost in currency
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="paper")
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    purpose: Mapped[str | None] = mapped_column(String(32))  # ENTRY / EXIT_SIGNAL / STOP_LOSS ...
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime)

    trade: Mapped[Trade | None] = relationship(back_populates="orders")
    stock: Mapped[Stock] = relationship()


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_snapshot_portfolio_ts", "portfolio_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)


class DailySummary(Base):
    __tablename__ = "daily_summary"
    __table_args__ = (UniqueConstraint("stock_id", "date", name="uq_daily_summary_stock_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    prev_close: Mapped[float | None] = mapped_column(Float)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    change: Mapped[float | None] = mapped_column(Float)
    percent_change: Mapped[float | None] = mapped_column(Float)
    direction: Mapped[Direction | None] = mapped_column(_enum(Direction, "direction"))
    intraday_range_pct: Mapped[float | None] = mapped_column(Float)  # (high-low)/low*100
    last_signal: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    stock: Mapped[Stock] = relationship()


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    final_equity: Mapped[float] = mapped_column(Float, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trades: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    channels: Mapped[str | None] = mapped_column(String(128))
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
