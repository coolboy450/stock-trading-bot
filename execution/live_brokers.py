"""Real-money broker adapters.

.. warning::
   These send REAL orders. They are only constructed after
   :func:`execution.safety.confirm_live_trading` succeeds. Test against the
   broker's own paper/sandbox environment first.

* :class:`AlpacaBroker`  - US stocks via the Alpaca REST API (v2).
* :class:`SettradeBroker` - Thai stocks via SETTRADE Open API (skeleton; requires
  a broker-approved Open API account and the ``settrade-v2`` SDK).
"""

from __future__ import annotations

import time

import requests
from loguru import logger

from config.settings import ExecutionSettings
from execution.broker import BaseBroker, BrokerError, OrderRequest, OrderResult
from utils.retry import retry

_ALPACA_STATUS = {
    "filled": "FILLED",
    "canceled": "CANCELLED",
    "expired": "CANCELLED",
    "rejected": "REJECTED",
    "suspended": "REJECTED",
}


class AlpacaBroker(BaseBroker):
    name = "alpaca"

    def __init__(self, settings: ExecutionSettings, session: requests.Session | None = None) -> None:
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            raise BrokerError("ALPACA_API_KEY / ALPACA_API_SECRET are required for the Alpaca broker")
        self.base_url = settings.alpaca_base_url.rstrip("/")
        self.is_live = "paper-api" not in self.base_url
        self.max_order_value = settings.live_max_order_value
        self.http = session or requests.Session()
        self.http.headers.update(
            {"APCA-API-KEY-ID": settings.alpaca_api_key, "APCA-API-SECRET-KEY": settings.alpaca_api_secret}
        )

    @retry(exceptions=(requests.ConnectionError, requests.Timeout), tries=3, delay=1.0)
    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self.http.request(method, f"{self.base_url}{path}", timeout=15, **kwargs)
        if resp.status_code >= 400:
            raise BrokerError(f"Alpaca {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def place_order(self, order: OrderRequest) -> OrderResult:
        notional = (order.limit_price or order.reference_price or 0) * order.quantity
        if notional > self.max_order_value:
            return OrderResult("REJECTED", message=f"order value {notional:,.2f} > live_max_order_value")
        payload: dict = {
            "symbol": order.symbol,
            "qty": str(order.quantity),
            "side": order.side.lower(),
            "type": order.order_type.lower(),
            "time_in_force": "day",
        }
        if order.order_type == "LIMIT":
            payload["limit_price"] = f"{order.limit_price:.2f}"
        try:
            data = self._request("POST", "/v2/orders", json=payload)
        except (BrokerError, requests.RequestException) as exc:
            logger.error("Alpaca order failed: {}", exc)
            return OrderResult("REJECTED", message=str(exc))
        # Market orders usually fill within seconds; poll briefly.
        # Anything still open afterwards stays PENDING and is re-checked next cycle.
        for _ in range(5):
            result = self._to_result(data)
            if result.status != "PENDING":
                return result
            time.sleep(1)
            try:
                data = self._request("GET", f"/v2/orders/{data['id']}")
            except (BrokerError, requests.RequestException) as exc:
                logger.warning("Alpaca status poll failed: {}", exc)
                break
        return self._to_result(data)

    def check_order(self, broker_order_id: str, order: OrderRequest, market_price: float | None) -> OrderResult:
        try:
            return self._to_result(self._request("GET", f"/v2/orders/{broker_order_id}"))
        except (BrokerError, requests.RequestException) as exc:
            return OrderResult("PENDING", broker_order_id=broker_order_id, message=f"status check failed: {exc}")

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self._request("DELETE", f"/v2/orders/{broker_order_id}")
            return True
        except (BrokerError, requests.RequestException) as exc:
            logger.error("Alpaca cancel failed: {}", exc)
            return False

    @staticmethod
    def _to_result(data: dict) -> OrderResult:
        status = _ALPACA_STATUS.get(data.get("status", ""), "PENDING")
        filled_qty = float(data.get("filled_qty") or 0)
        price = data.get("filled_avg_price")
        return OrderResult(
            status,
            filled_quantity=filled_qty,
            filled_price=float(price) if price else None,
            commission=0.0,  # Alpaca equities are commission-free (regulatory fees not modelled)
            broker_order_id=data.get("id"),
            message=data.get("status", ""),
        )


class SettradeBroker(BaseBroker):
    """Placeholder for SETTRADE Open API (Thai equities).

    To implement: ``pip install settrade-v2``, obtain app_id/app_secret/broker_id/
    app_code from your broker, then map :class:`OrderRequest` to
    ``Investor.get_equity(account).place_order(...)`` (price/volume/side/pin).
    """

    name = "settrade"
    is_live = True

    def __init__(self, settings: ExecutionSettings) -> None:
        raise BrokerError(
            "SettradeBroker is not implemented. Thai stocks are supported for data, signals, "
            "backtesting and paper trading; live execution requires a SETTRADE Open API account."
        )

    def place_order(self, order: OrderRequest) -> OrderResult:  # pragma: no cover
        raise NotImplementedError

    def check_order(self, broker_order_id: str, order: OrderRequest, market_price: float | None) -> OrderResult:  # pragma: no cover
        raise NotImplementedError
