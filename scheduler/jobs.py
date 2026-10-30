"""APScheduler jobs: intraday cycles during market hours + end-of-day run."""

from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from analysis.daily_summary import build_daily_summary, format_report
from config.settings import Settings
from execution.engine import TradingEngine
from utils.market_hours import is_market_open


def any_market_open(settings: Settings) -> bool:
    markets = {item.market for item in settings.watchlist}
    return any(is_market_open(settings.markets[m]) for m in markets if m in settings.markets)


def intraday_job(engine: TradingEngine) -> None:
    cfg = engine.settings.scheduler.intraday
    if cfg.only_market_hours and not any_market_open(engine.settings):
        logger.debug("Intraday job skipped: market closed")
        return
    try:
        report = engine.run_cycle(interval=cfg.interval)
        logger.info("\n{}", report.format())
    except Exception as exc:
        logger.exception("Intraday job failed")
        engine.alerts.error("intraday_job", exc)


def end_of_day_job(engine: TradingEngine) -> None:
    """Fetch daily bars, run the daily strategy cycle, store + send the daily summary."""
    try:
        report = engine.run_cycle(interval="1d")
        logger.info("\n{}", report.format())
        with engine.db.session() as s:
            rows = build_daily_summary(s, engine.settings)
        summary = format_report(rows)
        engine.alerts.report("End-of-day report", f"{summary}\n\n{report.format()}")
    except Exception as exc:
        logger.exception("End-of-day job failed")
        engine.alerts.error("end_of_day_job", exc)


def build_scheduler(engine: TradingEngine) -> BlockingScheduler:
    cfg = engine.settings.scheduler
    scheduler = BlockingScheduler(
        timezone=cfg.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    if cfg.intraday.enabled:
        scheduler.add_job(
            intraday_job, IntervalTrigger(minutes=cfg.intraday.every_minutes, timezone=cfg.timezone),
            args=[engine], id="intraday", name=f"intraday every {cfg.intraday.every_minutes}m",
        )
    if cfg.end_of_day.enabled:
        eod = cfg.end_of_day
        scheduler.add_job(
            end_of_day_job,
            CronTrigger(hour=eod.hour, minute=eod.minute, day_of_week=eod.day_of_week, timezone=cfg.timezone),
            args=[engine], id="end_of_day", name=f"end of day {eod.hour:02d}:{eod.minute:02d} {eod.day_of_week}",
        )
    if not scheduler.get_jobs():
        raise RuntimeError("No scheduler jobs enabled (scheduler.intraday / scheduler.end_of_day)")
    return scheduler
