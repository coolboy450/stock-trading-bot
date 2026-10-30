"""Commission + slippage model (used by the paper broker and the backtester)."""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import ExecutionSettings


@dataclass(frozen=True)
class CostModel:
    commission_pct: float = 0.001
    min_commission: float = 0.0
    commission_vat_pct: float = 0.0  # e.g. 0.07 = Thai VAT charged on the commission
    slippage_pct: float = 0.0005

    @classmethod
    def from_settings(cls, s: ExecutionSettings) -> "CostModel":
        return cls(s.commission_pct, s.min_commission, s.commission_vat_pct, s.slippage_pct)

    def fill_price(self, price: float, side: str) -> float:
        """Market-order fill price after adverse slippage (BUY pays up, SELL receives less)."""
        return price * (1 + self.slippage_pct) if side.upper() == "BUY" else price * (1 - self.slippage_pct)

    def commission(self, notional: float) -> float:
        if notional <= 0:
            return 0.0
        base = max(notional * self.commission_pct, self.min_commission)
        return base * (1 + self.commission_vat_pct)

    @property
    def cost_pct(self) -> float:
        """Approximate one-way cost as a fraction (for position sizing)."""
        return self.slippage_pct + self.commission_pct * (1 + self.commission_vat_pct)
