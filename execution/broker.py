"""Broker interface + paper (simulated) broker."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from execution.costs import CostModel


@dataclass
class OrderRequest:
    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: float
    order_type: str = "MARKET"  # "MARKET" | "LIMIT"
    limit_price: float | None = None
    reference_price: float | None = None  # latest known market price

    def __post_init__(self) -> None:
        self.side = self.side.upper()
        self.order_type = self.order_type.upper()
        if self.side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid side {self.side}")
        if self.order_type not in {"MARKET", "LIMIT"}:
            raise ValueError(f"invalid order type {self.order_type}")
        if self.quantity <= 0:
            raise ValueError("quantity must be > 0")
        if self.order_type == "LIMIT" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("limit orders need a positive limit_price")


@dataclass
class OrderResult:
    status: str  # FILLED | PENDING | REJECTED | CANCELLED
    filled_quantity: float = 0.0
    filled_price: float | None = None
    commission: float = 0.0
    slippage_cost: float = 0.0
    broker_order_id: str | None = None
    message: str = ""

    @property
    def filled(self) -> bool:
        return self.status == "FILLED"


class BrokerError(RuntimeError):
    pass


class BaseBroker(ABC):
    name: str = "base"
    is_live: bool = False

    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult:
        """Submit an order. Must not raise for business rejections - return REJECTED."""

    @abstractmethod
    def check_order(self, broker_order_id: str, order: OrderRequest, market_price: float | None) -> OrderResult:
        """Re-check a PENDING order (limit orders)."""

    def cancel_order(self, broker_order_id: str) -> bool:
        return True


class PaperBroker(BaseBroker):
    """Simulated fills with slippage + commission.

    * MARKET: fills immediately at ``reference_price`` +/- slippage.
    * LIMIT : fills at the limit (or better) if marketable, else stays PENDING and is
      re-checked each cycle via :meth:`check_order`.
    """

    name = "paper"
    is_live = False

    def __init__(self, costs: CostModel) -> None:
        self.costs = costs

    def place_order(self, order: OrderRequest) -> OrderResult:
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        if order.reference_price is None or order.reference_price <= 0:
            return OrderResult("REJECTED", broker_order_id=order_id, message="no market price available")
        result = self._try_fill(order, order.reference_price)
        result.broker_order_id = order_id
        return result

    def check_order(self, broker_order_id: str, order: OrderRequest, market_price: float | None) -> OrderResult:
        if market_price is None:
            return OrderResult("PENDING", broker_order_id=broker_order_id, message="no price")
        result = self._try_fill(order, market_price)
        result.broker_order_id = broker_order_id
        return result

    def _try_fill(self, order: OrderRequest, market_price: float) -> OrderResult:
        if order.order_type == "MARKET":
            price = self.costs.fill_price(market_price, order.side)
        else:
            assert order.limit_price is not None
            marketable = market_price <= order.limit_price if order.side == "BUY" else market_price >= order.limit_price
            if not marketable:
                return OrderResult("PENDING", message=f"limit {order.limit_price:.4f} not reached ({market_price:.4f})")
            # Price improvement capped at the limit; slippage still applies.
            slipped = self.costs.fill_price(market_price, order.side)
            price = min(slipped, order.limit_price) if order.side == "BUY" else max(slipped, order.limit_price)
        notional = price * order.quantity
        return OrderResult(
            "FILLED",
            filled_quantity=order.quantity,
            filled_price=price,
            commission=self.costs.commission(notional),
            slippage_cost=abs(price - market_price) * order.quantity,
            message="paper fill",
        )
