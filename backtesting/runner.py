"""Helpers to run backtests from settings / the database and persist results."""

from __future__ import annotations

import pandas as pd
from sqlalchemy.orm import Session

from analysis.metrics import periods_per_year_for
from backtesting.engine import BacktestConfig, Backtester, BacktestResult
from config.settings import Settings
from database.models import BacktestRun
from execution.costs import CostModel
from strategies import STRATEGIES, get_strategy


def config_from_settings(settings: Settings, market: str = "US", interval: str = "1d") -> BacktestConfig:
    return BacktestConfig(
        initial_capital=settings.execution.initial_capital,
        costs=CostModel.from_settings(settings.execution),
        risk=settings.risk,
        lot_size=settings.markets.get(market, settings.markets["US"]).lot_size,
        periods_per_year=periods_per_year_for(interval, market),
    )


def run_backtest(
    df: pd.DataFrame,
    symbol: str,
    strategy_name: str,
    settings: Settings,
    *,
    params: dict | None = None,
    market: str = "US",
    interval: str = "1d",
) -> BacktestResult:
    merged = {**settings.strategy.params_for(strategy_name), **(params or {})}
    strategy = get_strategy(strategy_name, merged)
    return Backtester(strategy, config_from_settings(settings, market, interval)).run(df, symbol)


def compare_strategies(
    df: pd.DataFrame, symbol: str, settings: Settings, *, market: str = "US", interval: str = "1d"
) -> pd.DataFrame:
    """Run every registered strategy (config params) and return a metrics table."""
    rows = []
    for name in STRATEGIES:
        try:
            res = run_backtest(df, symbol, name, settings, market=market, interval=interval)
        except ValueError as exc:
            rows.append({"strategy": name, "error": str(exc)})
            continue
        m = res.metrics
        rows.append(
            {
                "strategy": name,
                "return_%": round(m.total_return_pct, 2),
                "cagr_%": round(m.cagr_pct, 2),
                "max_dd_%": round(m.max_drawdown_pct, 2),
                "sharpe": round(m.sharpe_ratio, 2),
                "trades": m.num_trades,
                "win_rate_%": round(m.win_rate_pct, 1),
                "profit_factor": round(m.profit_factor, 2),
                "buy_hold_%": round(m.buy_and_hold_return_pct or 0, 2),
            }
        )
    return pd.DataFrame(rows).set_index("strategy")


def save_backtest(session: Session, result: BacktestResult, interval: str, initial_capital: float) -> BacktestRun:
    run = BacktestRun(
        symbol=result.symbol,
        strategy=result.strategy,
        params=result.params,
        interval=interval,
        start=result.equity_curve.index[0].to_pydatetime(),
        end=result.equity_curve.index[-1].to_pydatetime(),
        initial_capital=initial_capital,
        final_equity=result.final_equity,
        metrics=result.metrics.to_dict(),
        trades=[t.to_dict() for t in result.trades],
    )
    session.add(run)
    session.flush()
    return run
