"""Application settings loaded from YAML + environment variables.

Secrets (API keys, tokens) should live in ``.env`` / environment variables and be
referenced from YAML with ``${VAR_NAME}`` or ``${VAR_NAME:-default}``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

Market = Literal["TH", "US", "CRYPTO", "OTHER"]


class WatchItem(BaseModel):
    """A symbol the bot monitors and trades."""

    symbol: str
    market: Market = "US"
    name: str | None = None
    sector: str | None = None
    exchange: str | None = None
    strategy: str | None = None  # per-symbol strategy override

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class MarketSettings(BaseModel):
    """Trading calendar / conventions for one market."""

    timezone: str = "America/New_York"
    sessions: list[tuple[str, str]] = [("09:30", "16:00")]
    trading_days: list[int] = [0, 1, 2, 3, 4]  # Monday=0
    holidays: list[str] = []  # ISO dates, e.g. "2026-12-25"
    lot_size: int = Field(1, ge=1)
    currency: str = "USD"


class DatabaseSettings(BaseModel):
    url: str = "sqlite:///./data/stock_bot.db"
    echo: bool = False


class DataSettings(BaseModel):
    provider: Literal["yahoo", "alpha_vantage", "synthetic"] = "yahoo"
    fallback_providers: list[Literal["yahoo", "alpha_vantage", "synthetic"]] = []
    interval: str = "1d"
    history_period: str = "2y"
    lookback_bars: int = Field(300, ge=50)
    alpha_vantage_api_key: str | None = None
    alpha_vantage_outputsize: Literal["compact", "full"] = "compact"
    request_timeout: int = 20
    max_retries: int = 3
    retry_delay_seconds: float = 2.0


class StrategySettings(BaseModel):
    name: str = "ma_crossover"
    params: dict[str, dict[str, Any]] = {}

    def params_for(self, name: str) -> dict[str, Any]:
        return dict(self.params.get(name, {}))


class RiskSettings(BaseModel):
    position_size_pct: float = Field(0.10, gt=0, le=1)
    risk_per_trade_pct: float | None = Field(0.01, gt=0, le=1)
    stop_loss_pct: float = Field(0.05, gt=0, lt=1)
    take_profit_pct: float = Field(0.10, gt=0)
    max_daily_loss_pct: float = Field(0.03, gt=0, le=1)
    max_open_positions: int = Field(5, ge=1)
    max_drawdown_pct: float = Field(0.15, gt=0, le=1)
    min_cash_reserve_pct: float = Field(0.0, ge=0, lt=1)


class ExecutionSettings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    broker: Literal["paper", "alpaca", "settrade"] = "paper"
    portfolio_name: str = "default"
    initial_capital: float = Field(100_000.0, gt=0)
    commission_pct: float = Field(0.001, ge=0)
    min_commission: float = Field(0.0, ge=0)
    commission_vat_pct: float = Field(0.0, ge=0)
    slippage_pct: float = Field(0.0005, ge=0)
    order_type: Literal["market", "limit"] = "market"
    limit_offset_pct: float = Field(0.002, ge=0)
    limit_order_ttl_minutes: int = Field(60, ge=1)
    live_max_order_value: float = Field(5_000.0, gt=0)
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None

    @model_validator(mode="after")
    def _paper_broker_only_in_paper(self) -> "ExecutionSettings":
        if self.mode == "live" and self.broker == "paper":
            raise ValueError("mode=live requires a real broker (alpaca/settrade), not 'paper'")
        return self


class AlertSettings(BaseModel):
    channels: list[Literal["console", "telegram", "discord", "line"]] = ["console"]
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    line_channel_access_token: str | None = None
    line_to: str | None = None
    notify_signals: bool = True
    notify_trades: bool = True
    price_move_pct: float = Field(5.0, gt=0)
    drawdown_alert_pct: float = Field(0.10, gt=0, le=1)
    cooldown_minutes: int = Field(60, ge=0)


class IntradayJob(BaseModel):
    enabled: bool = False
    every_minutes: int = Field(5, ge=1)
    interval: str = "5m"
    only_market_hours: bool = True


class EndOfDayJob(BaseModel):
    enabled: bool = True
    hour: int = Field(17, ge=0, le=23)
    minute: int = Field(15, ge=0, le=59)
    day_of_week: str = "mon-fri"


class SchedulerSettings(BaseModel):
    timezone: str = "Asia/Bangkok"
    intraday: IntradayJob = IntradayJob()
    end_of_day: EndOfDayJob = EndOfDayJob()


class LoggingSettings(BaseModel):
    level: str = "INFO"
    directory: str = "./logs"
    rotation: str = "10 MB"
    retention: str = "30 days"
    json_logs: bool = False


class DashboardSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


def _default_markets() -> dict[str, MarketSettings]:
    return {
        "TH": MarketSettings(
            timezone="Asia/Bangkok",
            sessions=[("10:00", "12:30"), ("14:30", "16:30")],
            lot_size=100,
            currency="THB",
        ),
        "US": MarketSettings(),
        "CRYPTO": MarketSettings(
            timezone="UTC", sessions=[("00:00", "23:59")], trading_days=list(range(7)), currency="USDT"
        ),
        "OTHER": MarketSettings(),
    }


class Settings(BaseModel):
    """Root configuration object."""

    database: DatabaseSettings = DatabaseSettings()
    data: DataSettings = DataSettings()
    watchlist: list[WatchItem] = []
    markets: dict[str, MarketSettings] = Field(default_factory=_default_markets)
    strategy: StrategySettings = StrategySettings()
    risk: RiskSettings = RiskSettings()
    execution: ExecutionSettings = ExecutionSettings()
    alerts: AlertSettings = AlertSettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    logging: LoggingSettings = LoggingSettings()
    dashboard: DashboardSettings = DashboardSettings()

    @model_validator(mode="after")
    def _fill_markets(self) -> "Settings":
        defaults = _default_markets()
        for key, value in defaults.items():
            self.markets.setdefault(key, value)
        return self

    def market_for(self, item: WatchItem) -> MarketSettings:
        return self.markets.get(item.market, MarketSettings())

    @property
    def is_live(self) -> bool:
        return self.execution.mode == "live"


def _expand_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` with environment values."""
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default if default is not None else "")

        expanded = _ENV_PATTERN.sub(repl, value)
        return expanded if expanded != "" or value == "" else None
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_settings(path: str | Path | None = None, env_file: str | Path | None = None) -> Settings:
    """Load settings from a YAML file (falls back to the example config).

    Args:
        path: YAML config path. Defaults to ``config/config.yaml`` or the example file.
        env_file: Optional ``.env`` path. Defaults to ``<project>/.env`` if present.
    """
    env_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    if path is None:
        path = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Settings.model_validate(_expand_env(raw))
