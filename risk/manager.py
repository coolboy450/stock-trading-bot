"""Risk management shared by the backtester and the live/paper engine."""

from __future__ import annotations

import math
from dataclasses import dataclass

from config.settings import RiskSettings


@dataclass
class AccountState:
    """Snapshot of the account used for risk checks."""

    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    open_positions: int

    @property
    def drawdown_pct(self) -> float:
        return 0.0 if self.peak_equity <= 0 else max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_pnl_pct(self) -> float:
        return 0.0 if self.day_start_equity <= 0 else (self.equity - self.day_start_equity) / self.day_start_equity


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, settings: RiskSettings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ gates
    def can_open_position(self, state: AccountState) -> RiskDecision:
        """Check all portfolio-level limits before opening a new position."""
        s = self.settings
        if state.open_positions >= s.max_open_positions:
            return RiskDecision(False, f"max open positions reached ({state.open_positions}/{s.max_open_positions})")
        if state.daily_pnl_pct <= -s.max_daily_loss_pct:
            return RiskDecision(
                False, f"daily loss limit hit ({state.daily_pnl_pct:.2%} <= -{s.max_daily_loss_pct:.2%})"
            )
        if state.drawdown_pct >= s.max_drawdown_pct:
            return RiskDecision(False, f"max drawdown exceeded ({state.drawdown_pct:.2%} >= {s.max_drawdown_pct:.2%})")
        if state.cash <= state.equity * s.min_cash_reserve_pct:
            return RiskDecision(False, "cash reserve exhausted")
        return RiskDecision(True)

    # ------------------------------------------------------------------ levels
    def stop_loss_price(self, entry_price: float) -> float:
        return entry_price * (1 - self.settings.stop_loss_pct)

    def take_profit_price(self, entry_price: float) -> float:
        return entry_price * (1 + self.settings.take_profit_pct)

    # ------------------------------------------------------------------ sizing
    def position_size(
        self,
        state: AccountState,
        price: float,
        *,
        stop_price: float | None = None,
        lot_size: int = 1,
        cost_pct: float = 0.0,
    ) -> int:
        """Number of shares to buy (a multiple of ``lot_size``; 0 = don't trade).

        The smallest of:
          * ``position_size_pct`` x equity
          * ``risk_per_trade_pct`` x equity / (price - stop)   (if enabled)
          * cash available above the reserve (incl. estimated costs)
        """
        if price <= 0:
            return 0
        s = self.settings
        budget = state.equity * s.position_size_pct
        qty = budget / price
        if s.risk_per_trade_pct and stop_price is not None and 0 < stop_price < price:
            qty = min(qty, state.equity * s.risk_per_trade_pct / (price - stop_price))
        spendable = max(0.0, state.cash - state.equity * s.min_cash_reserve_pct)
        qty = min(qty, spendable / (price * (1 + cost_pct)))
        lots = math.floor(qty / lot_size + 1e-9)
        return max(0, lots * lot_size)

    # ------------------------------------------------------------------ exits
    @staticmethod
    def check_exit(
        low: float, high: float, stop_loss: float | None, take_profit: float | None, open_: float | None = None
    ) -> tuple[str, float] | None:
        """Return (reason, fill price) if a stop/target is hit in a bar, else None.

        Gaps through a level fill at the open. If both levels are inside one bar,
        the stop is assumed to hit first (conservative).
        """
        if stop_loss is not None:
            if open_ is not None and open_ <= stop_loss:
                return "STOP_LOSS", open_
            if low <= stop_loss:
                return "STOP_LOSS", stop_loss
        if take_profit is not None:
            if open_ is not None and open_ >= take_profit:
                return "TAKE_PROFIT", open_
            if high >= take_profit:
                return "TAKE_PROFIT", take_profit
        return None
