"""Performance metrics: win rate, profit factor, max drawdown, Sharpe, etc."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class PerformanceMetrics:
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    volatility_pct: float
    num_trades: int
    win_rate_pct: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    expectancy: float
    largest_win: float
    largest_loss: float
    exposure_pct: float
    buy_and_hold_return_pct: float | None = None

    def to_dict(self) -> dict[str, float | int | None]:
        """JSON-safe dict (non-finite values such as an infinite profit factor become ``None``)."""
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in asdict(self).items()}

    def format(self) -> str:
        pf = "inf" if math.isinf(self.profit_factor) else f"{self.profit_factor:.2f}"
        lines = [
            f"Total return     : {self.total_return_pct:8.2f} %",
            f"CAGR             : {self.cagr_pct:8.2f} %",
            f"Max drawdown     : {self.max_drawdown_pct:8.2f} %",
            f"Sharpe ratio     : {self.sharpe_ratio:8.2f}",
            f"Sortino ratio    : {self.sortino_ratio:8.2f}",
            f"Volatility (ann.): {self.volatility_pct:8.2f} %",
            f"Trades           : {self.num_trades:8d}",
            f"Win rate         : {self.win_rate_pct:8.2f} %",
            f"Profit factor    : {pf:>8}",
            f"Avg win / loss   : {self.avg_win:,.2f} / {self.avg_loss:,.2f}",
            f"Expectancy/trade : {self.expectancy:,.2f}",
            f"Exposure         : {self.exposure_pct:8.2f} %",
        ]
        if self.buy_and_hold_return_pct is not None:
            lines.append(f"Buy & hold       : {self.buy_and_hold_return_pct:8.2f} %")
        return "\n".join(lines)


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough decline as a fraction (0.25 = -25%)."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (peak - equity) / peak
    return float(dd.max())


def sharpe_ratio(returns: pd.Series, periods_per_year: int = 252, risk_free_rate: float = 0.0) -> float:
    """Annualised Sharpe of periodic returns (risk-free rate is annual)."""
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / periods_per_year
    std = excess.std(ddof=1)
    return 0.0 if std == 0 or np.isnan(std) else float(excess.mean() / std * math.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int = 252, risk_free_rate: float = 0.0) -> float:
    returns = returns.dropna()
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / periods_per_year
    downside = np.sqrt((np.minimum(excess, 0) ** 2).mean())
    return 0.0 if downside == 0 else float(excess.mean() / downside * math.sqrt(periods_per_year))


def profit_factor(pnls: Sequence[float]) -> float:
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def compute_metrics(
    equity: pd.Series,
    trade_pnls: Sequence[float],
    *,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
    exposure: float | None = None,
    benchmark: pd.Series | None = None,
) -> PerformanceMetrics:
    """Compute performance metrics from an equity curve and closed-trade PnLs.

    Args:
        equity: Equity value per bar.
        trade_pnls: Net PnL of each closed trade.
        periods_per_year: 252 for daily bars, 252*6.5 for hourly US, etc.
        exposure: Fraction of bars with an open position.
        benchmark: Price series for buy-and-hold comparison.
    """
    equity = equity.dropna()
    if equity.empty:
        raise ValueError("equity curve is empty")
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    total_return = end / start - 1
    years = max(len(equity) / periods_per_year, 1e-9)
    cagr = (end / start) ** (1 / years) - 1 if end > 0 else -1.0
    rets = equity.pct_change().dropna()

    pnls = list(trade_pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    bh = None
    if benchmark is not None and len(benchmark.dropna()) > 1:
        b = benchmark.dropna()
        bh = (float(b.iloc[-1]) / float(b.iloc[0]) - 1) * 100

    return PerformanceMetrics(
        total_return_pct=total_return * 100,
        cagr_pct=cagr * 100,
        max_drawdown_pct=max_drawdown(equity) * 100,
        sharpe_ratio=sharpe_ratio(rets, periods_per_year, risk_free_rate),
        sortino_ratio=sortino_ratio(rets, periods_per_year, risk_free_rate),
        volatility_pct=float(rets.std(ddof=1) * math.sqrt(periods_per_year) * 100) if len(rets) > 1 else 0.0,
        num_trades=len(pnls),
        win_rate_pct=(len(wins) / len(pnls) * 100) if pnls else 0.0,
        profit_factor=profit_factor(pnls),
        avg_win=float(np.mean(wins)) if wins else 0.0,
        avg_loss=float(np.mean(losses)) if losses else 0.0,
        expectancy=float(np.mean(pnls)) if pnls else 0.0,
        largest_win=max(wins) if wins else 0.0,
        largest_loss=min(losses) if losses else 0.0,
        exposure_pct=(exposure or 0.0) * 100,
        buy_and_hold_return_pct=bh,
    )


def periods_per_year_for(interval: str, market: str = "US") -> int:
    """Approximate bars per year for annualisation."""
    hours_per_day = {"US": 6.5, "TH": 4.5, "CRYPTO": 24}.get(market, 6.5)
    days = 365 if market == "CRYPTO" else 252
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "60m": 60}.get(interval)
    if minutes is None:
        return days
    return int(days * hours_per_day * 60 / minutes)
