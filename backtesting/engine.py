"""Event-driven, single-symbol, long-only backtester.

Execution model (no look-ahead):
  * a signal produced on bar *t* (using its close) is executed at bar *t+1*'s **open**
  * stop-loss / take-profit are checked intrabar against each bar's low/high
    (gap through a level -> fill at the open; both in one bar -> stop first)
  * every fill pays slippage + commission from :class:`CostModel`
  * position sizing and portfolio limits use the same :class:`RiskManager` as live
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from analysis.metrics import PerformanceMetrics, compute_metrics
from config.settings import RiskSettings
from execution.costs import CostModel
from risk.manager import AccountState, RiskManager
from strategies.base import BUY, SELL, BaseStrategy


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskSettings = field(default_factory=RiskSettings)
    lot_size: int = 1
    periods_per_year: int = 252
    use_stops: bool = True


@dataclass
class BacktestTrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    quantity: int
    stop_loss: float | None
    take_profit: float | None
    entry_commission: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_commission: float = 0.0
    exit_reason: str | None = None
    entry_reason: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity - self.entry_commission - self.exit_commission

    @property
    def pnl_pct(self) -> float:
        cost = self.entry_price * self.quantity + self.entry_commission
        return self.pnl / cost * 100 if cost else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        d["exit_time"] = self.exit_time.isoformat() if self.exit_time else None
        d["pnl"], d["pnl_pct"] = round(self.pnl, 4), round(self.pnl_pct, 4)
        return d


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    params: dict
    equity_curve: pd.Series
    trades: list[BacktestTrade]
    metrics: PerformanceMetrics
    signals: pd.DataFrame
    blocked_entries: list[tuple[datetime, str]]

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve.iloc[-1])

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])

    def summary(self) -> str:
        start, end = self.equity_curve.index[0], self.equity_curve.index[-1]
        header = (
            f"Backtest {self.symbol} | {self.strategy} {self.params}\n"
            f"Period {start:%Y-%m-%d} -> {end:%Y-%m-%d} ({len(self.equity_curve)} bars)\n"
            f"Equity {self.equity_curve.iloc[0]:,.2f} -> {self.final_equity:,.2f}\n"
        )
        blocked = f"\nEntries blocked by risk rules: {len(self.blocked_entries)}" if self.blocked_entries else ""
        return header + "-" * 48 + "\n" + self.metrics.format() + blocked


class Backtester:
    def __init__(self, strategy: BaseStrategy, config: BacktestConfig | None = None) -> None:
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.risk = RiskManager(self.config.risk)

    def run(self, df: pd.DataFrame, symbol: str = "SYMBOL") -> BacktestResult:
        if len(df) < self.strategy.min_bars + 2:
            raise ValueError(f"Need at least {self.strategy.min_bars + 2} bars, got {len(df)}")
        cfg, costs = self.config, self.config.costs
        data = self.strategy.generate_signals(df.sort_index())

        cash = cfg.initial_capital
        peak = cfg.initial_capital
        position: BacktestTrade | None = None
        trades: list[BacktestTrade] = []
        blocked: list[tuple[datetime, str]] = []
        equity_values: list[float] = []
        bars_in_market = 0
        day_start_equity = cfg.initial_capital
        current_day = None

        def close_position(ts: datetime, raw_price: float, reason: str, slip: bool = True) -> None:
            nonlocal cash, position
            assert position is not None
            price = costs.fill_price(raw_price, "SELL") if slip else raw_price
            commission = costs.commission(price * position.quantity)
            cash += price * position.quantity - commission
            position.exit_time, position.exit_price = ts, price
            position.exit_commission, position.exit_reason = commission, reason
            trades.append(position)
            position = None

        opens, highs, lows, closes = (data[c].to_numpy() for c in ("open", "high", "low", "close"))
        signals, reasons = data["signal"].to_numpy(), data["reason"].to_numpy()
        index = data.index

        for i in range(len(data)):
            ts = index[i].to_pydatetime()
            if current_day != ts.date():
                current_day = ts.date()
                day_start_equity = equity_values[-1] if equity_values else cfg.initial_capital

            # 1) execute yesterday's signal at today's open
            if i > 0:
                prev_signal = signals[i - 1]
                if prev_signal == SELL and position is not None:
                    close_position(ts, opens[i], "SIGNAL")
                elif prev_signal == BUY and position is None:
                    equity_now = cash  # flat -> equity == cash
                    state = AccountState(equity_now, cash, peak, day_start_equity, 0)
                    decision = self.risk.can_open_position(state)
                    if not decision.allowed:
                        blocked.append((ts, decision.reason))
                    else:
                        entry = costs.fill_price(opens[i], "BUY")
                        stop = self.risk.stop_loss_price(entry) if cfg.use_stops else None
                        target = self.risk.take_profit_price(entry) if cfg.use_stops else None
                        qty = self.risk.position_size(
                            state, entry, stop_price=stop, lot_size=cfg.lot_size, cost_pct=costs.cost_pct
                        )
                        if qty > 0:
                            commission = costs.commission(entry * qty)
                            cash -= entry * qty + commission
                            position = BacktestTrade(
                                symbol, ts, entry, qty, stop, target, commission, entry_reason=str(reasons[i - 1])
                            )
                        else:
                            blocked.append((ts, "position size rounds to 0"))

            # 2) intrabar stop-loss / take-profit
            if position is not None and cfg.use_stops:
                hit = self.risk.check_exit(lows[i], highs[i], position.stop_loss, position.take_profit, opens[i])
                if hit is not None:
                    reason, level = hit
                    # Stops fill with slippage; targets fill at the limit price.
                    close_position(ts, level, reason, slip=(reason == "STOP_LOSS"))

            # 3) mark to market at the close
            equity = cash + (position.quantity * closes[i] if position else 0.0)
            peak = max(peak, equity)
            equity_values.append(equity)
            bars_in_market += position is not None

        if position is not None:
            close_position(index[-1].to_pydatetime(), closes[-1], "END_OF_BACKTEST")
            equity_values[-1] = cash

        equity_curve = pd.Series(equity_values, index=index, name="equity")
        metrics = compute_metrics(
            equity_curve,
            [t.pnl for t in trades],
            periods_per_year=cfg.periods_per_year,
            exposure=bars_in_market / len(data),
            benchmark=data["close"],
        )
        return BacktestResult(
            symbol, self.strategy.name, dict(self.strategy.params), equity_curve, trades, metrics, data, blocked
        )
