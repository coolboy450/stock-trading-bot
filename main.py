#!/usr/bin/env python3
"""Stock Trading Bot - command-line entry point.

DISCLAIMER: For education and research only. Not investment advice.
Trading - especially with real money - carries a high risk of loss.

Examples::

    python main.py init-db
    python main.py backfill --period 2y
    python main.py backtest --symbol AAPL --strategy rsi --param period=10
    python main.py compare --symbol AAPL
    python main.py run-once
    python main.py schedule
    python main.py dashboard
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger  # noqa: E402

from config.settings import Settings, WatchItem, load_settings  # noqa: E402
from database.session import Database  # noqa: E402
from utils.logger import setup_logging  # noqa: E402

DISCLAIMER = (
    "⚠️  For education/research only - NOT investment advice. "
    "Past (backtest) performance does not guarantee future results."
)


# --------------------------------------------------------------------------- helpers
def _parse_params(pairs: list[str] | None) -> dict:
    out: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param must be key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        try:
            out[key] = float(value) if "." in value else int(value)
        except ValueError:
            out[key] = value
    return out


def _watch_items(settings: Settings, symbols: list[str] | None, market: str | None = None) -> list[WatchItem]:
    if not symbols:
        return settings.watchlist
    known = {w.symbol: w for w in settings.watchlist}
    items = []
    for sym in symbols:
        sym = sym.upper()
        default_market = "TH" if sym.endswith(".BK") else "US"
        items.append(known.get(sym) or WatchItem(symbol=sym, market=market or default_market))
    return items


def _provider(settings: Settings, override: str | None = None):
    from data.providers import build_provider

    data = settings.data.model_copy(update={"provider": override}) if override else settings.data
    return build_provider(data)


def _engine(settings: Settings, db: Database, provider_override: str | None = None, *, read_only: bool = False):
    """Build the trading engine. ``read_only`` uses a paper broker (no live confirmation)."""
    from execution.broker import PaperBroker
    from execution.costs import CostModel
    from execution.engine import TradingEngine
    from execution.safety import build_broker
    from monitoring.alerts import AlertManager
    from monitoring.notifier import build_notifier

    alerts = AlertManager(settings.alerts, build_notifier(settings.alerts), db)
    broker = PaperBroker(CostModel.from_settings(settings.execution)) if read_only else build_broker(settings)
    return TradingEngine(settings, db, _provider(settings, provider_override), broker, alerts)


# --------------------------------------------------------------------------- commands
def cmd_init_db(args, settings: Settings, db: Database) -> None:
    db.create_all()
    from data.collector import PriceCollector

    collector = PriceCollector(settings, _provider(settings, "synthetic"))
    with db.session() as s:
        for item in settings.watchlist:
            collector.ensure_stock(s, item)
    print(f"Database ready: {db.engine.url.render_as_string(hide_password=True)} "
          f"({len(settings.watchlist)} watchlist stocks registered)")


def cmd_backfill(args, settings: Settings, db: Database) -> None:
    from data.collector import PriceCollector

    db.create_all()
    collector = PriceCollector(settings, _provider(settings, args.provider))
    for item in _watch_items(settings, args.symbols):
        with db.session() as s:
            res = collector.backfill(s, item, interval=args.interval, period=args.period)
        print(f"{item.symbol:<12} {'OK' if res.ok else 'FAIL'}  {res.bars_written} bars"
              + (f"  ({res.error})" if res.error else ""))


def cmd_fetch(args, settings: Settings, db: Database) -> None:
    from data.collector import PriceCollector
    from database import repository as repo

    db.create_all()
    collector = PriceCollector(settings, _provider(settings, args.provider))
    for item in _watch_items(settings, args.symbols):
        with db.session() as s:
            res = collector.update(s, item, interval=args.interval)
            stock = repo.get_stock(s, item.symbol)
            bar = repo.last_bar(s, stock.id, args.interval or settings.data.interval) if stock else None
        if bar is None:
            print(f"{item.symbol:<12} no data ({res.error})")
            continue
        pct = f"{bar.percent_change:+.2f}%" if bar.percent_change is not None else "n/a"
        direction = bar.direction.value if bar.direction else "-"
        print(f"{item.symbol:<12} {bar.timestamp:%Y-%m-%d %H:%M}  close {bar.close:>12,.4f}  {pct:>8} {direction}")


def _load_backtest_df(args, settings: Settings, db: Database, item: WatchItem):
    """Use stored prices if available, otherwise fetch from the provider."""
    from database import repository as repo

    interval = args.interval or settings.data.interval
    start = datetime.fromisoformat(args.start) if args.start else None
    end = datetime.fromisoformat(args.end) if args.end else None
    db.create_all()
    with db.session() as s:
        stock = repo.get_stock(s, item.symbol)
        df = repo.get_price_df(s, stock.id, interval, start=start, end=end) if stock else None
    if df is None or df.empty or args.provider:
        provider = _provider(settings, args.provider)
        df = provider.fetch_history(item.symbol, interval=interval, period=args.period, start=start, end=end)
        logger.info("Fetched {} bars for {} from {}", len(df), item.symbol, provider.name)
    return df, interval


def cmd_backtest(args, settings: Settings, db: Database) -> None:
    from backtesting.runner import run_backtest, save_backtest

    item = _watch_items(settings, [args.symbol])[0]
    df, interval = _load_backtest_df(args, settings, db, item)
    name = args.strategy or item.strategy or settings.strategy.name
    result = run_backtest(df, item.symbol, name, settings, params=_parse_params(args.param),
                          market=item.market, interval=interval)
    print(result.summary())
    if args.trades:
        tdf = result.trades_df()
        print("\n" + (tdf[["entry_time", "entry_price", "exit_time", "exit_price", "quantity", "pnl", "exit_reason"]]
                      .to_string(index=False) if not tdf.empty else "No trades"))
    if args.save:
        with db.session() as s:
            run = save_backtest(s, result, interval, settings.execution.initial_capital)
        print(f"\nSaved as backtest_runs.id={run.id}")
    if args.csv:
        result.trades_df().to_csv(args.csv, index=False)
        result.equity_curve.to_csv(Path(args.csv).with_suffix(".equity.csv"))
        print(f"Trades -> {args.csv}")
    print("\n" + DISCLAIMER)


def cmd_compare(args, settings: Settings, db: Database) -> None:
    from backtesting.runner import compare_strategies

    item = _watch_items(settings, [args.symbol])[0]
    df, interval = _load_backtest_df(args, settings, db, item)
    table = compare_strategies(df, item.symbol, settings, market=item.market, interval=interval)
    print(f"Strategy comparison {item.symbol} ({len(df)} bars, {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d})")
    print(table.to_string())
    print("\n" + DISCLAIMER)


def cmd_run_once(args, settings: Settings, db: Database) -> None:
    db.create_all()
    engine = _engine(settings, db, args.provider)
    report = engine.run_cycle(interval=args.interval, fetch=not args.no_fetch)
    print(report.format())


def cmd_schedule(args, settings: Settings, db: Database) -> None:
    from scheduler.jobs import build_scheduler, end_of_day_job

    db.create_all()
    engine = _engine(settings, db, args.provider)
    scheduler = build_scheduler(engine)
    for job in scheduler.get_jobs():
        logger.info("Scheduled job: {}", job.name)
    if args.run_now:
        end_of_day_job(engine)
    print(f"Scheduler started ({settings.scheduler.timezone}). Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def cmd_dashboard(args, settings: Settings, db: Database) -> None:
    import uvicorn

    from dashboard.app import create_app

    db.create_all()
    uvicorn.run(create_app(settings, db), host=args.host or settings.dashboard.host,
                port=args.port or settings.dashboard.port, log_level="warning")


def cmd_summary(args, settings: Settings, db: Database) -> None:
    from analysis.daily_summary import build_daily_summary, format_report

    day = date.fromisoformat(args.date) if args.date else None
    with db.session() as s:
        rows = build_daily_summary(s, settings, day)
    print(format_report(rows))


def cmd_status(args, settings: Settings, db: Database) -> None:
    from database import repository as repo

    db.create_all()
    engine = _engine(settings, db, "synthetic", read_only=True)  # provider/broker unused
    with db.session() as s:
        pf = engine.portfolio(s)
        engine.revalue(s, pf)
        print(f"Portfolio {pf.name} [{pf.mode.value}] {pf.currency}")
        print(f"  Equity {pf.equity:,.2f} | Cash {pf.cash:,.2f} | Positions {pf.positions_value:,.2f}")
        print(f"  Return {(pf.equity / pf.initial_capital - 1) * 100:+.2f}% | Realized {pf.realized_pnl:+,.2f} | "
              f"Drawdown {pf.drawdown_pct:.2%}")
        trades = repo.open_trades(s, pf.id)
        for t in trades:
            last = repo.latest_close(s, t.stock_id) or t.entry_price
            print(f"  {t.stock.symbol:<10} x{t.quantity:g} entry {t.entry_price:,.4f} last {last:,.4f} "
                  f"({(last / t.entry_price - 1) * 100:+.2f}%) SL {t.stop_loss:,.4f} TP {t.take_profit:,.4f}")
        if not trades:
            print("  No open positions")
        daily = repo.period_pnl(s, pf, "D").tail(5)
        if not daily.empty:
            print("\nRecent daily PnL:\n" + daily.to_string(index=False))


def cmd_close(args, settings: Settings, db: Database) -> None:
    engine = _engine(settings, db, "synthetic")
    print(engine.close_position(args.symbol.upper()))


def cmd_test_alert(args, settings: Settings, db: Database) -> None:
    from monitoring.alerts import AlertManager
    from monitoring.notifier import build_notifier

    db.create_all()
    ok = AlertManager(settings.alerts, build_notifier(settings.alerts), db).send(
        "TEST", "Stock bot test alert", "If you can read this, notifications work.", "INFO")
    print("Delivered" if ok else "Delivery failed - check logs/errors.log")


def _yahoo_reachable(symbol: str) -> tuple[bool, str]:
    """Quick probe: can we download real prices right now?"""
    import logging

    yf_log = logging.getLogger("yfinance")
    old_level = yf_log.level
    yf_log.setLevel(logging.CRITICAL)  # a failed probe is expected offline - keep output clean
    logger.disable("utils.retry")
    try:
        from data.providers.yahoo import YahooProvider

        df = YahooProvider(max_retries=1, timeout=10).fetch_history(symbol, interval="1d", period="5d")
        return (not df.empty), "ok"
    except ImportError:
        return False, "yfinance not installed"
    except Exception as exc:  # network blocked, rate limit, Yahoo outage...
        text = str(exc)
        if any(k in text for k in ("curl", "Connection", "403", "resolve", "timed out", "Timeout")):
            return False, "cannot reach Yahoo Finance"
        return False, text.splitlines()[0][:100]
    finally:
        yf_log.setLevel(old_level)
        logger.enable("utils.retry")


def cmd_demo(args, settings: Settings, db: Database) -> None:
    """One command, works online or offline: probe -> backfill -> compare -> trade cycle -> summary -> status.

    Online uses real Yahoo prices, offline uses synthetic prices. Each mode has its own
    database and portfolio so fake and real data are never mixed. Always paper trading.
    """
    if not settings.watchlist:
        raise SystemExit("watchlist is empty - add symbols to the config")
    first = settings.watchlist[0].symbol

    if args.offline:
        online, why = False, "forced with --offline"
    else:
        print(f"Checking internet / Yahoo Finance ({first}) ...")
        online, why = _yahoo_reachable(first)
    mode = "online" if online else "offline"
    provider = "yahoo" if online else "synthetic"
    name = _demo_name(args.config, mode)

    demo = settings.model_copy(deep=True)
    demo.data.provider = provider
    demo.data.fallback_providers = []
    demo.database.url = f"sqlite:///./data/{name}.db"
    demo.execution.mode = "paper"  # the demo never sends real orders
    demo.execution.broker = "paper"
    demo.execution.portfolio_name = name
    demo.logging.level = "WARNING"
    setup_logging(demo.logging)
    demo_db = Database(demo.database.url)

    banner = "ONLINE - real prices from Yahoo Finance" if online else f"OFFLINE - synthetic (fake) prices ({why})"
    print("=" * 72 + f"\n MODE: {banner}\n DB  : {demo_db.engine.url.database}\n" + "=" * 72)

    print("\n[1/5] Download price history (2y)")
    cmd_backfill(argparse.Namespace(symbols=None, interval=None, provider=provider, period="2y"), demo, demo_db)

    print(f"\n[2/5] Backtest all strategies on {first}")
    try:
        cmd_compare(argparse.Namespace(symbol=first, interval=None, provider=None, period="2y", start=None, end=None),
                    demo, demo_db)
    except Exception as exc:
        print(f"  backtest skipped: {exc}")

    print("\n[3/5] Paper-trading cycle")
    cmd_run_once(argparse.Namespace(interval=None, provider=provider, no_fetch=False), demo, demo_db)

    print("\n[4/5] Daily summary (up / down)")
    cmd_summary(argparse.Namespace(date=None), demo, demo_db)

    print("\n[5/5] Portfolio")
    cmd_status(argparse.Namespace(), demo, demo_db)

    cfg = f" -c {args.config}" if args.config else ""
    print(f"\nDone ({mode}). Run again any time - it continues the same paper portfolio.\n"
          f"Dashboard on this data: python main.py{cfg} demo-dashboard --{mode}")


def _demo_name(config: str | None, mode: str) -> str:
    """Separate DB + portfolio per config and mode (never mix markets/currencies or fake/real data)."""
    stem = Path(config).stem.replace(".", "_") if config else "default"
    return f"demo_{stem}_{mode}"


def cmd_demo_dashboard(args, settings: Settings, db: Database) -> None:
    name = _demo_name(args.config, "offline" if args.offline else "online")
    demo = settings.model_copy(deep=True)
    demo.database.url = f"sqlite:///./data/{name}.db"
    demo.execution.mode, demo.execution.broker = "paper", "paper"
    demo.execution.portfolio_name = name
    cmd_dashboard(argparse.Namespace(host=None, port=None), demo, Database(demo.database.url))


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automated stock trading bot (education/research)")
    p.add_argument("--config", "-c", help="YAML config (default config/config.yaml or the example)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_data_args(sp, symbols: bool = True) -> None:
        if symbols:
            sp.add_argument("--symbols", nargs="*", help="override watchlist symbols")
        sp.add_argument("--interval", help="bar interval (1m 5m 15m 1h 1d)")
        sp.add_argument("--provider", choices=["yahoo", "alpha_vantage", "synthetic"], help="override data provider")

    sub.add_parser("init-db", help="create tables and register watchlist stocks").set_defaults(func=cmd_init_db)

    sp = sub.add_parser("backfill", help="download historical prices (default 2y)")
    add_data_args(sp)
    sp.add_argument("--period", help="e.g. 1y 2y 5y max")
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("fetch", help="incremental price update + show latest change")
    add_data_args(sp)
    sp.set_defaults(func=cmd_fetch)

    for name, func, helptext in (("backtest", cmd_backtest, "backtest one strategy"),
                                 ("compare", cmd_compare, "backtest all strategies")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--symbol", required=True)
        add_data_args(sp, symbols=False)
        sp.add_argument("--period", default="2y")
        sp.add_argument("--start", help="YYYY-MM-DD")
        sp.add_argument("--end", help="YYYY-MM-DD")
        if name == "backtest":
            sp.add_argument("--strategy", help="ma_crossover rsi bollinger breakout momentum")
            sp.add_argument("--param", action="append", help="strategy param key=value (repeatable)")
            sp.add_argument("--trades", action="store_true", help="print trade list")
            sp.add_argument("--save", action="store_true", help="store result in backtest_runs")
            sp.add_argument("--csv", help="write trades CSV to this path")
        sp.set_defaults(func=func)

    sp = sub.add_parser("run-once", help="run one trading cycle now")
    add_data_args(sp, symbols=False)
    sp.add_argument("--no-fetch", action="store_true", help="use stored prices only")
    sp.set_defaults(func=cmd_run_once)

    sp = sub.add_parser("schedule", help="run continuously on the configured schedule")
    sp.add_argument("--provider", choices=["yahoo", "alpha_vantage", "synthetic"])
    sp.add_argument("--run-now", action="store_true", help="run the end-of-day job once at start")
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("dashboard", help="start the web dashboard")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("summary", help="daily up/down summary")
    sp.add_argument("--date", help="YYYY-MM-DD (default: latest trading day)")
    sp.set_defaults(func=cmd_summary)

    sub.add_parser("status", help="show portfolio and open positions").set_defaults(func=cmd_status)

    sp = sub.add_parser("close", help="manually close a position at the last stored price")
    sp.add_argument("symbol")
    sp.set_defaults(func=cmd_close)

    sub.add_parser("test-alert", help="send a test notification").set_defaults(func=cmd_test_alert)

    sp = sub.add_parser("demo", help="full demo in one command (real data if online, synthetic if offline)")
    sp.add_argument("--offline", action="store_true", help="skip the internet check and use synthetic data")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("demo-dashboard", help="dashboard for the demo database")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--online", action="store_true", help="(default)")
    mode.add_argument("--offline", action="store_true")
    sp.set_defaults(func=cmd_demo_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    setup_logging(settings.logging)
    if settings.is_live:
        logger.warning("Configuration is in LIVE mode - real orders may be sent.")
    db = Database(settings.database.url, echo=settings.database.echo)
    try:
        args.func(args, settings, db)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("Command failed: {}", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
