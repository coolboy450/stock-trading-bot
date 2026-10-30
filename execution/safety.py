"""Guards that must pass before any real-money order can be sent."""

from __future__ import annotations

import os
import sys

from loguru import logger

from config.settings import Settings
from execution.broker import BaseBroker, PaperBroker
from execution.costs import CostModel

CONFIRM_ENV = "LIVE_TRADING_CONFIRMATION"
CONFIRM_VALUE = "I_UNDERSTAND_THE_RISKS"


class LiveTradingNotConfirmed(RuntimeError):
    pass


def confirm_live_trading(settings: Settings, *, interactive: bool | None = None, input_fn=input) -> None:
    """Require three independent confirmations for live mode.

    1. ``execution.mode: live`` in config
    2. env ``LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISKS``
    3. typing the portfolio name at an interactive prompt
    """
    if not settings.is_live:
        return
    if os.environ.get(CONFIRM_ENV) != CONFIRM_VALUE:
        raise LiveTradingNotConfirmed(
            f"Live mode requested but {CONFIRM_ENV} is not set to {CONFIRM_VALUE}. Refusing to trade real money."
        )
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if not interactive:
        raise LiveTradingNotConfirmed("Live mode needs an interactive confirmation (run from a terminal).")
    name = settings.execution.portfolio_name
    print("\n" + "!" * 70)
    print("  LIVE TRADING MODE - REAL MONEY WILL BE USED")
    print(f"  Broker: {settings.execution.broker}   Portfolio: {name}")
    print(f"  Max order value: {settings.execution.live_max_order_value:,.2f}")
    print("  This software is for education/research and comes with NO WARRANTY.")
    print("!" * 70)
    answer = input_fn(f"Type the portfolio name '{name}' to continue: ").strip()
    if answer != name:
        raise LiveTradingNotConfirmed("Confirmation did not match - aborting.")
    logger.warning("LIVE trading confirmed by operator for portfolio {}", name)


def build_broker(settings: Settings) -> BaseBroker:
    """Create the broker for the configured mode (paper unless explicitly live)."""
    costs = CostModel.from_settings(settings.execution)
    if not settings.is_live:
        return PaperBroker(costs)
    confirm_live_trading(settings)
    if settings.execution.broker == "alpaca":
        from execution.live_brokers import AlpacaBroker

        return AlpacaBroker(settings.execution)
    if settings.execution.broker == "settrade":
        from execution.live_brokers import SettradeBroker

        return SettradeBroker(settings.execution)
    raise ValueError(f"Unsupported live broker {settings.execution.broker}")
