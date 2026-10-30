"""Trading engine: data -> signals -> risk checks -> orders -> DB + alerts.

One call to :meth:`TradingEngine.run_cycle` processes the whole watchlist once.
It is idempotent per bar: re-running on the same bar will not place duplicate
orders because each (stock, strategy, interval, bar) signal is acted on once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger
from sqlalchemy.orm import Session

from config.settings import Settings, WatchItem
from data.collector import PriceCollector
from data.providers import DataProvider
from database import repository as repo
from database.models import (
    ExitReason,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    PortfolioSnapshot,
    Signal,
    SignalType,
    Stock,
    Trade,
    TradeStatus,
    TradingMode,
    utcnow,
)
from database.session import Database
from execution.broker import BaseBroker, OrderRequest, OrderResult
from execution.costs import CostModel
from monitoring.alerts import AlertManager
from risk.manager import AccountState, RiskManager
from strategies import BUY, SELL, BaseStrategy, get_strategy
from utils.logger import trade_logger
from utils.market_hours import is_market_open

_SIGNAL_TYPE = {BUY: SignalType.BUY, SELL: SignalType.SELL}


@dataclass
class SymbolReport:
    symbol: str
    signal: str = "HOLD"
    price: float | None = None
    action: str = ""
    error: str | None = None


@dataclass
class CycleReport:
    started_at: datetime
    interval: str
    symbols: list[SymbolReport] = field(default_factory=list)
    finished_at: datetime | None = None
    equity: float = 0.0
    cash: float = 0.0
    open_positions: int = 0
    drawdown_pct: float = 0.0

    @property
    def errors(self) -> list[SymbolReport]:
        return [s for s in self.symbols if s.error]

    def format(self) -> str:
        lines = [f"Cycle {self.started_at:%Y-%m-%d %H:%M:%S} UTC [{self.interval}]"]
        for s in self.symbols:
            price = f"{s.price:,.4f}" if s.price is not None else "-"
            status = f"ERROR: {s.error}" if s.error else s.action
            lines.append(f"  {s.symbol:<12} {s.signal:<5} {price:>14}  {status}")
        lines.append(
            f"Equity {self.equity:,.2f} | Cash {self.cash:,.2f} | Open {self.open_positions} | "
            f"DD {self.drawdown_pct:.2%}"
        )
        return "\n".join(lines)


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        provider: DataProvider,
        broker: BaseBroker,
        alerts: AlertManager,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """``clock`` returns naive-UTC "now"; override it to replay history (forward tests)."""
        self.settings = settings
        self.db = db
        self.broker = broker
        self.alerts = alerts
        self.collector = PriceCollector(settings, provider)
        self.risk = RiskManager(settings.risk)
        self.costs = CostModel.from_settings(settings.execution)
        self.mode = TradingMode.LIVE if settings.is_live else TradingMode.PAPER
        self._strategies: dict[str, BaseStrategy] = {}
        self.clock = clock

    # ================================================================ public API
    def run_cycle(self, interval: str | None = None, *, fetch: bool = True) -> CycleReport:
        """Process every watchlist symbol once and snapshot the portfolio."""
        interval = interval or self.settings.data.interval
        report = CycleReport(started_at=self.clock(), interval=interval)
        logger.info("=== Trading cycle start [{}] mode={} ===", interval, self.mode.value)

        try:
            with self.alerts.deferred(), self.db.session() as s:
                self._process_pending_orders(s, self.portfolio(s))
        except Exception as exc:
            logger.exception("Pending-order processing failed")
            self.alerts.error("pending_orders", exc)

        for item in self.settings.watchlist:
            sym_report = SymbolReport(item.symbol)
            try:
                # One transaction per symbol: a failure only rolls back that symbol.
                # Alert logs are written after the commit (see AlertManager.deferred).
                with self.alerts.deferred(), self.db.session() as s:
                    self._process_symbol(s, item, interval, fetch, sym_report)
            except Exception as exc:
                logger.exception("Error processing {}", item.symbol)
                sym_report.error = str(exc)
                self.alerts.error(f"symbol {item.symbol}", exc)
            report.symbols.append(sym_report)

        with self.alerts.deferred(), self.db.session() as s:
            pf = self.portfolio(s)
            self.revalue(s, pf, snapshot=True)
            report.equity, report.cash, report.drawdown_pct = pf.equity, pf.cash, pf.drawdown_pct
            report.open_positions = len(repo.open_trades(s, pf.id))
        self.alerts.drawdown(self.settings.execution.portfolio_name, report.drawdown_pct, self.settings.risk.max_drawdown_pct)
        report.finished_at = self.clock()
        logger.info("=== Trading cycle done: equity={:,.2f} open={} ===", report.equity, report.open_positions)
        return report

    def portfolio(self, session: Session) -> Portfolio:
        currency = (
            self.settings.market_for(self.settings.watchlist[0]).currency if self.settings.watchlist else "USD"
        )
        return repo.get_or_create_portfolio(
            session,
            self.settings.execution.portfolio_name,
            mode=self.mode,
            initial_capital=self.settings.execution.initial_capital,
            currency=currency,
        )

    def close_position(self, symbol: str, reason: ExitReason = ExitReason.MANUAL) -> str:
        """Manually close an open position at the latest stored price."""
        with self.alerts.deferred(), self.db.session() as s:
            pf = self.portfolio(s)
            stock = repo.get_stock(s, symbol)
            trade = repo.open_trade_for(s, pf.id, stock.id) if stock else None
            if trade is None:
                return f"No open position in {symbol}"
            price = repo.latest_close(s, stock.id)
            note = self._exit(s, pf, trade, price, reason, signal=None)
            self.revalue(s, pf, snapshot=True)
            return note

    def revalue(self, session: Session, pf: Portfolio, *, snapshot: bool = False) -> None:
        """Mark open positions to market, update equity/peak and optionally snapshot."""
        positions = []
        value = 0.0
        for t in repo.open_trades(session, pf.id):
            price = repo.latest_close(session, t.stock_id) or t.entry_price
            value += t.quantity * price
            positions.append(
                {
                    "symbol": t.stock.symbol,
                    "quantity": t.quantity,
                    "entry_price": round(t.entry_price, 6),
                    "last_price": round(price, 6),
                    "unrealized_pnl": round((price - t.entry_price) * t.quantity - t.entry_commission, 2),
                    "stop_loss": t.stop_loss,
                    "take_profit": t.take_profit,
                }
            )
        pf.positions_value = value
        pf.equity = pf.cash + value
        pf.peak_equity = max(pf.peak_equity, pf.equity)
        if snapshot:
            session.add(
                PortfolioSnapshot(
                    portfolio_id=pf.id,
                    timestamp=self.clock(),
                    cash=pf.cash,
                    positions_value=value,
                    equity=pf.equity,
                    drawdown_pct=pf.drawdown_pct,
                    open_positions=len(positions),
                    positions=positions,
                )
            )
        session.flush()

    def account_state(self, session: Session, pf: Portfolio) -> AccountState:
        self.revalue(session, pf)
        tz = ZoneInfo(self.settings.scheduler.timezone)
        today = self.clock().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
        day_start, _ = repo.day_bounds_utc(today, self.settings.scheduler.timezone)
        return AccountState(
            equity=pf.equity,
            cash=pf.cash,
            peak_equity=pf.peak_equity,
            day_start_equity=repo.equity_at_start_of(session, pf, day_start),
            open_positions=len(repo.open_trades(session, pf.id)) + self._pending_entries(session, pf),
        )

    # ================================================================ internals
    def _bar_still_open(self, item: WatchItem, interval: str, bar_time: datetime) -> bool:
        """True if ``bar_time`` is today's daily bar and the market has not closed yet."""
        if interval != "1d":
            return False
        market = self.settings.market_for(item)
        now = self.clock()
        if not is_market_open(market, now):
            return False
        tz, utc = ZoneInfo(market.timezone), ZoneInfo("UTC")
        bar_day = bar_time.replace(tzinfo=utc).astimezone(tz).date()
        return bar_day == now.replace(tzinfo=utc).astimezone(tz).date()

    def _strategy_for(self, item: WatchItem) -> BaseStrategy:
        name = item.strategy or self.settings.strategy.name
        if name not in self._strategies:
            self._strategies[name] = get_strategy(name, self.settings.strategy.params_for(name))
        return self._strategies[name]

    def _process_symbol(self, s: Session, item: WatchItem, interval: str, fetch: bool, rep: SymbolReport) -> None:
        stock = self.collector.ensure_stock(s, item)
        if fetch:
            result = self.collector.update(s, item, interval)
            if not result.ok:
                rep.error = f"fetch failed: {result.error}"
                self.alerts.error(f"data {item.symbol}", result.error or "unknown")
                # fall through and use already-stored data if any

        df = repo.get_price_df(s, stock.id, interval, limit=self.settings.data.lookback_bars)
        if df.empty:
            rep.action = "no data"
            return
        bar_time = df.index[-1].to_pydatetime()
        price = float(df["close"].iloc[-1])
        rep.price = price
        pct = df["percent_change"].iloc[-1]
        self.alerts.price_move(item.symbol, None if pd.isna(pct) else float(pct), price, bar_time)

        pf = self.portfolio(s)
        trade = repo.open_trade_for(s, pf.id, stock.id)
        actions: list[str] = []

        # 1) protective exits (checked on the latest price each cycle)
        exited = False
        if trade is not None and not self._has_pending(s, pf, stock, OrderSide.SELL):
            if trade.stop_loss is not None and price <= trade.stop_loss:
                actions.append(self._exit(s, pf, trade, price, ExitReason.STOP_LOSS, None))
                exited = True
            elif trade.take_profit is not None and price >= trade.take_profit:
                actions.append(self._exit(s, pf, trade, price, ExitReason.TAKE_PROFIT, None))
                exited = True
            trade = repo.open_trade_for(s, pf.id, stock.id)

        # 2) strategy signal - from completed bars only. A daily bar for a session
        # that is still trading changes until the close, so it must not trigger trades.
        closed = df
        if self._bar_still_open(item, interval, bar_time):
            closed = df.iloc[:-1]
            actions.append("today's bar still open - signal from last closed bar")
        signal_bar_time = closed.index[-1].to_pydatetime() if len(closed) else bar_time
        strategy = self._strategy_for(item)
        if len(closed) < strategy.min_bars:
            rep.action = "; ".join(actions + [f"need {strategy.min_bars} bars, have {len(closed)}"])
            return
        sig_df = strategy.generate_signals(closed[["open", "high", "low", "close", "volume"]])
        sig_val = int(sig_df["signal"].iloc[-1])
        sig_type = _SIGNAL_TYPE.get(sig_val, SignalType.HOLD)
        rep.signal = sig_type.value
        signal, created = repo.save_signal(
            s,
            stock=stock,
            strategy=strategy.name,
            signal_type=sig_type,
            interval=interval,
            timestamp=signal_bar_time,
            price=price,
            reason=str(sig_df["reason"].iloc[-1]) or None,
            indicators=strategy.latest_indicators(sig_df),
        )
        if created and sig_type != SignalType.HOLD:
            logger.info("Signal {} {} @ {:.4f}: {}", sig_type.value, item.symbol, price, signal.reason)
            self.alerts.signal(item.symbol, sig_type.value, price, signal.reason or "", strategy.name)

        if signal.acted_on:
            actions.append("signal already handled")
        elif sig_type == SignalType.SELL and trade is not None:
            if self._has_pending(s, pf, stock, OrderSide.SELL):
                actions.append("exit order already pending")
            else:
                actions.append(self._exit(s, pf, trade, price, ExitReason.SIGNAL, signal))
            signal.acted_on = True
        elif sig_type == SignalType.BUY and trade is None and not exited:
            actions.append(self._enter(s, pf, stock, item, price, strategy.name, signal))
            signal.acted_on = True
        elif sig_type == SignalType.BUY and trade is not None:
            actions.append("already in position")
        if actions and sig_type != SignalType.HOLD:
            signal.action_note = "; ".join(a for a in actions if a)
        rep.action = "; ".join(a for a in actions if a) or "-"

    def _enter(
        self, s: Session, pf: Portfolio, stock: Stock, item: WatchItem, price: float, strategy: str, signal: Signal
    ) -> str:
        if self._has_pending(s, pf, stock, OrderSide.BUY):
            return "entry order already pending"
        state = self.account_state(s, pf)
        decision = self.risk.can_open_position(state)
        if not decision.allowed:
            logger.warning("Entry {} blocked: {}", stock.symbol, decision.reason)
            self.alerts.risk_block(stock.symbol, decision.reason)
            return f"blocked: {decision.reason}"

        ex = self.settings.execution
        is_limit = ex.order_type == "limit"
        planned = price * (1 - ex.limit_offset_pct) if is_limit else self.costs.fill_price(price, "BUY")
        qty = self.risk.position_size(
            state,
            planned,
            stop_price=self.risk.stop_loss_price(planned),
            lot_size=self.settings.market_for(item).lot_size,
            cost_pct=self.costs.cost_pct,
        )
        if qty <= 0:
            return "position size is 0 (not enough cash / lot size too large)"
        request = OrderRequest(
            stock.symbol, "BUY", qty, "LIMIT" if is_limit else "MARKET",
            limit_price=round(planned, 4) if is_limit else None, reference_price=price,
        )
        order = self._new_order(s, pf, stock, request, purpose="ENTRY", signal=signal)
        result = self._place(request)
        return self._apply_result(s, pf, order, result, strategy=strategy)

    def _exit(
        self, s: Session, pf: Portfolio, trade: Trade, price: float | None, reason: ExitReason, signal: Signal | None
    ) -> str:
        if price is None:
            return "no price to exit"
        request = OrderRequest(trade.stock.symbol, "SELL", trade.quantity, "MARKET", reference_price=price)
        order = self._new_order(s, pf, trade.stock, request, purpose=reason.value, signal=signal, trade=trade)
        result = self._place(request)
        return self._apply_result(s, pf, order, result)

    def _place(self, request: OrderRequest) -> OrderResult:
        try:
            return self.broker.place_order(request)
        except Exception as exc:
            logger.exception("Broker error placing order")
            self.alerts.error("broker.place_order", exc)
            return OrderResult("REJECTED", message=f"broker error: {exc}")

    def _new_order(
        self, s: Session, pf: Portfolio, stock: Stock, req: OrderRequest, *, purpose: str,
        signal: Signal | None, trade: Trade | None = None,
    ) -> Order:
        order = Order(
            portfolio_id=pf.id,
            stock_id=stock.id,
            trade_id=trade.id if trade else None,
            signal_id=signal.id if signal else None,
            mode=self.mode,
            side=OrderSide(req.side),
            order_type=OrderType(req.order_type),
            quantity=req.quantity,
            limit_price=req.limit_price,
            reference_price=req.reference_price,
            status=OrderStatus.PENDING,
            broker=self.broker.name,
            created_at=self.clock(),
            purpose=purpose,
        )
        s.add(order)
        s.flush()
        return order

    def _apply_result(self, s: Session, pf: Portfolio, order: Order, result: OrderResult, strategy: str | None = None) -> str:
        """Persist a broker result and, if filled, open/close the trade and move cash."""
        order.broker_order_id = result.broker_order_id or order.broker_order_id
        order.message = result.message
        side, symbol = order.side.value, order.stock.symbol
        if result.status in {"REJECTED", "CANCELLED"}:
            order.status = OrderStatus(result.status)
            logger.warning("Order {} {} {} {}: {}", order.id, side, symbol, result.status, result.message)
            return f"{side} {result.status.lower()}: {result.message}"
        if result.status == "PENDING":
            return f"{side} {order.order_type.value.lower()} order pending @ {order.limit_price}"

        price = float(result.filled_price or 0)
        qty = float(result.filled_quantity or order.quantity)
        if side == "BUY" and not self.broker.is_live and price * qty + result.commission > pf.cash + 1e-6:
            order.status = OrderStatus.REJECTED
            order.message = "insufficient cash at fill time"
            return "BUY rejected: insufficient cash"

        order.status = OrderStatus.FILLED
        order.filled_quantity, order.filled_price = qty, price
        order.commission, order.slippage = result.commission, result.slippage_cost
        order.filled_at = self.clock()
        mode = self.mode.value

        if side == "BUY":
            if strategy is None and order.signal_id:
                sig = s.get(Signal, order.signal_id)
                strategy = sig.strategy if sig else None
            trade = Trade(
                portfolio_id=pf.id,
                stock_id=order.stock_id,
                strategy_used=strategy or self.settings.strategy.name,
                mode=self.mode,
                status=TradeStatus.OPEN,
                quantity=qty,
                entry_price=price,
                entry_time=order.filled_at,
                stop_loss=round(self.risk.stop_loss_price(price), 6),
                take_profit=round(self.risk.take_profit_price(price), 6),
                entry_commission=result.commission,
            )
            s.add(trade)
            s.flush()
            order.trade_id = trade.id
            pf.cash -= price * qty + result.commission
            trade_logger.info("[{}] BUY {} x{} @ {:.4f} comm={:.2f} SL={} TP={}", mode, symbol, qty, price,
                              result.commission, trade.stop_loss, trade.take_profit)
            self.alerts.trade("BUY", symbol, qty, price, mode=mode, reason=order.purpose or "")
            return f"BOUGHT {qty:g} @ {price:,.4f}"

        trade = order.trade
        assert trade is not None, "SELL order without trade"
        trade.exit_price, trade.exit_time = price, order.filled_at
        trade.exit_commission = result.commission
        trade.pnl = (price - trade.entry_price) * trade.quantity - trade.entry_commission - trade.exit_commission
        cost_basis = trade.entry_price * trade.quantity + trade.entry_commission
        trade.pnl_pct = trade.pnl / cost_basis * 100 if cost_basis else 0.0
        trade.exit_reason = ExitReason(order.purpose) if order.purpose in ExitReason.__members__ else ExitReason.SIGNAL
        trade.status = TradeStatus.CLOSED
        pf.cash += price * qty - result.commission
        pf.realized_pnl += trade.pnl
        trade_logger.info("[{}] SELL {} x{} @ {:.4f} pnl={:+.2f} ({})", mode, symbol, qty, price, trade.pnl,
                          trade.exit_reason.value)
        self.alerts.trade("SELL", symbol, qty, price, pnl=trade.pnl, mode=mode, reason=trade.exit_reason.value)
        return f"SOLD {qty:g} @ {price:,.4f} ({trade.exit_reason.value}, pnl {trade.pnl:+,.2f})"

    def _process_pending_orders(self, s: Session, pf: Portfolio) -> None:
        ttl = timedelta(minutes=self.settings.execution.limit_order_ttl_minutes)
        for order in repo.pending_orders(s, pf.id):
            if self.clock() - order.created_at > ttl:
                if order.broker_order_id:
                    self.broker.cancel_order(order.broker_order_id)
                order.status = OrderStatus.EXPIRED
                order.message = "expired (ttl)"
                logger.info("Order {} expired", order.id)
                continue
            request = OrderRequest(
                order.stock.symbol, order.side.value, order.quantity, order.order_type.value,
                limit_price=order.limit_price, reference_price=order.reference_price,
            )
            price = repo.latest_close(s, order.stock_id)
            result = self.broker.check_order(order.broker_order_id or "", request, price)
            self._apply_result(s, pf, order, result)

    def _has_pending(self, s: Session, pf: Portfolio, stock: Stock, side: OrderSide) -> bool:
        return any(o.stock_id == stock.id and o.side == side for o in repo.pending_orders(s, pf.id))

    def _pending_entries(self, s: Session, pf: Portfolio) -> int:
        return sum(1 for o in repo.pending_orders(s, pf.id) if o.side == OrderSide.BUY)
