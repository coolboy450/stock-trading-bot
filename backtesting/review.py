"""Weekly strategy review: walk-forward backtest of every strategy on every watchlist symbol (paper only).

Walk-forward (no peeking at the exam):
  * test window  = the latest ``test_months`` (default 6) of daily bars  -> the ONLY basis for decisions
  * train window = the ``train_months`` (default 12) before that          -> reported for reference
  * the train backtest only ever sees bars *before* ``test_start`` (see :func:`split_walk_forward`)
  * bars before a window serve as indicator warm-up only; return / drawdown / trade count are measured
    inside the window

Score = return / (1 + max_drawdown), return after commission + slippage (same CostModel/RiskManager as live).
If the return is negative we use ``return * (1 + max_drawdown)`` so a deeper drawdown never *improves* a loss.

Limitations: fixed parameters (nothing is optimised), a few trades per window = noisy, costs are modelled,
past results do not predict the future.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select

from backtesting.runner import run_backtest
from config.settings import Settings, StrategyReviewSettings, WatchItem
from database import repository as repo
from database.models import OrderSide, Portfolio
from database.session import Database
from monitoring.alerts import AlertManager
from monitoring.review_store import ReviewStore, effective_strategy

SCORE_FORMULA = (
    "score = return / (1 + max_drawdown)  [return หลังหักค่าคอม+slippage; ถ้า return < 0 ใช้ return × (1 + max_drawdown)]"
)


def score_of(ret: float, max_dd: float) -> float:
    return ret / (1 + max_dd) if ret >= 0 else ret * (1 + max_dd)


@dataclass
class WindowScore:
    strategy: str
    ret: float | None = None  # fraction (0.05 = +5%)
    max_dd: float | None = None  # fraction, >= 0
    trades: int = 0
    score: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalkForwardSplit:
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    end: pd.Timestamp
    train_df: pd.DataFrame  # every bar strictly before test_start (older bars = warm-up)
    full_df: pd.DataFrame  # everything (used for the test window)


def split_walk_forward(df: pd.DataFrame, train_months: int, test_months: int) -> WalkForwardSplit:
    df = df.sort_index()
    end = df.index[-1]
    test_start = end - pd.DateOffset(months=test_months)
    train_start = test_start - pd.DateOffset(months=train_months)
    return WalkForwardSplit(train_start, test_start, end, df[df.index < test_start], df)


def evaluate_window(df: pd.DataFrame, strategy: str, symbol: str, settings: Settings, market: str,
                    window_start: pd.Timestamp) -> WindowScore:
    """Backtest ``strategy`` on ``df`` and measure return / drawdown / trades from ``window_start`` on."""
    out = WindowScore(strategy)
    try:
        res = run_backtest(df, symbol, strategy, settings, market=market, interval="1d")
    except ValueError as exc:  # not enough bars for this strategy
        out.error = str(exc)
        return out
    eq = res.equity_curve
    inside = eq[eq.index >= window_start]
    if len(inside) < 2:
        out.error = "ข้อมูลในช่วงนี้ไม่พอ"
        return out
    before = eq[eq.index < window_start]
    base = float(before.iloc[-1]) if len(before) else float(eq.iloc[0])
    path = pd.concat([pd.Series([base]), inside.reset_index(drop=True)], ignore_index=True)
    out.ret = float(path.iloc[-1] / base - 1)
    out.max_dd = float(((path.cummax() - path) / path.cummax()).max())
    out.trades = sum(1 for t in res.trades if pd.Timestamp(t.entry_time) >= window_start)
    out.score = score_of(out.ret, out.max_dd)
    return out


# ====================================================================== decision rules
@dataclass
class Decision:
    symbol: str
    current: str
    candidate: str | None
    action: str  # "change" | "pending" | "keep"
    code: str  # machine-readable reason
    reason: str  # Thai text for logs / Discord / dashboard

    @property
    def changed(self) -> bool:
        return self.action == "change"


def beats_by(candidate: float, current: float, margin: float) -> bool:
    """candidate is better than current by at least ``margin`` (0.2 = 20% of |current|)."""
    return candidate > current and (candidate - current) >= margin * abs(current) - 1e-12


def decide_change(
    symbol: str, current: str, scores: dict[str, WindowScore], cfg: StrategyReviewSettings, *,
    has_position: bool, last_changed: datetime | None, now: datetime,
) -> Decision:
    """Apply every rule; all must pass for a change (position open => pending instead)."""
    cur = scores.get(current)
    if cur is None or cur.score is None:
        return Decision(symbol, current, None, "keep", "no_current_score", "ประเมินกลยุทธ์ปัจจุบันไม่ได้ (ข้อมูลไม่พอ)")
    others = [s for n, s in scores.items() if n != current and s.score is not None]
    if not others:
        return Decision(symbol, current, None, "keep", "no_candidates", "ไม่มีกลยุทธ์อื่นให้เทียบ")
    eligible = [s for s in others if s.trades >= cfg.min_test_trades]
    if not eligible:
        top = max(others, key=lambda s: s.score or 0.0)
        return Decision(symbol, current, top.strategy, "keep", "too_few_trades",
                        f"ไม่เปลี่ยน: ไม่มีตัวไหนมีเทรด ≥ {cfg.min_test_trades} ครั้งในช่วงสอบ "
                        f"(ดีสุด {top.strategy} มี {top.trades} ครั้ง)")
    best = max(eligible, key=lambda s: s.score or 0.0)
    assert best.score is not None and cur.score is not None
    if not beats_by(best.score, cur.score, cfg.min_improvement_pct):
        return Decision(symbol, current, best.strategy, "keep", "not_better_enough",
                        f"ไม่เปลี่ยน: {best.strategy} ({best.score * 100:.2f}) ยังไม่ดีกว่า {current} "
                        f"({cur.score * 100:.2f}) ถึง {cfg.min_improvement_pct:.0%}")
    if last_changed is not None and cfg.cooldown_weeks > 0 and now - last_changed < timedelta(weeks=cfg.cooldown_weeks):
        return Decision(symbol, current, best.strategy, "keep", "cooldown",
                        f"ไม่เปลี่ยน: เพิ่งเปลี่ยนเมื่อ {last_changed:%Y-%m-%d} (ต้องรอ {cfg.cooldown_weeks} สัปดาห์)")
    if has_position:
        return Decision(symbol, current, best.strategy, "pending", "position_open",
                        f"รอเปลี่ยน → {best.strategy}: ผ่านเกณฑ์แล้วแต่ยังถือ position อยู่ (เปลี่ยนหลังปิด position)")
    return Decision(symbol, current, best.strategy, "change", "passed",
                    f"เปลี่ยน {current} → {best.strategy}: คะแนน {best.score * 100:.2f} vs {cur.score * 100:.2f} "
                    f"(ดีกว่า ≥ {cfg.min_improvement_pct:.0%}, เทรด {best.trades} ครั้งในช่วงสอบ)")


# ====================================================================== orchestration
@dataclass
class SymbolReview:
    symbol: str
    current: str
    decision: Decision | None = None
    test: dict[str, WindowScore] = field(default_factory=dict)
    train: dict[str, WindowScore] = field(default_factory=dict)
    skipped: str | None = None
    windows: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.decision
        return {
            "symbol": self.symbol, "current": self.current, "skipped": self.skipped, "windows": self.windows,
            "action": d.action if d else "skip", "candidate": d.candidate if d else None,
            "code": d.code if d else "skipped", "reason": d.reason if d else self.skipped,
            "test": {k: v.to_dict() for k, v in self.test.items()},
            "train": {k: v.to_dict() for k, v in self.train.items()},
        }


def has_open_exposure(db: Database, settings: Settings, symbol: str) -> bool:
    """True while the symbol has an open trade or a pending BUY order in the paper portfolio."""
    with db.session() as s:
        stock = repo.get_stock(s, symbol)
        pf = s.scalar(select(Portfolio).where(Portfolio.name == settings.execution.portfolio_name))
        if stock is None or pf is None:
            return False
        if repo.open_trade_for(s, pf.id, stock.id) is not None:
            return True
        return any(o.stock_id == stock.id and o.side == OrderSide.BUY for o in repo.pending_orders(s, pf.id))


def review_symbol(item: WatchItem, df: pd.DataFrame, settings: Settings, store: ReviewStore, *,
                  has_position: bool, now: datetime) -> SymbolReview:
    cfg = settings.strategy_review
    current = effective_strategy(settings, store, item)
    rev = SymbolReview(item.symbol, current)
    if df is None or len(df) < 30:
        rev.skipped = "ข้อมูลรายวันใน DB ไม่พอ (รัน backfill ก่อน)"
        return rev
    split = split_walk_forward(df, cfg.train_months, cfg.test_months)
    rev.windows = {"train": f"{split.train_start:%Y-%m-%d} → {split.test_start:%Y-%m-%d}",
                   "test": f"{split.test_start:%Y-%m-%d} → {split.end:%Y-%m-%d}"}
    names = list(dict.fromkeys([current, *cfg.strategies]))
    for name in names:
        rev.test[name] = evaluate_window(split.full_df, name, item.symbol, settings, item.market, split.test_start)
        rev.train[name] = evaluate_window(split.train_df, name, item.symbol, settings, item.market, split.train_start) \
            if len(split.train_df) else WindowScore(name, error="ไม่มีข้อมูลช่วงฝึก")
    rev.decision = decide_change(item.symbol, current, rev.test, cfg, has_position=has_position,
                                 last_changed=store.last_changed_at(item.symbol), now=now)
    return rev


@dataclass
class ReviewResult:
    at: datetime
    reviews: list[SymbolReview]
    trigger: str = "schedule"

    @property
    def changed(self) -> list[SymbolReview]:
        return [r for r in self.reviews if r.decision and r.decision.changed]


def next_review_at(cfg: StrategyReviewSettings, now: datetime | None = None) -> datetime | None:
    if not cfg.enabled:
        return None
    trigger = CronTrigger(day_of_week=cfg.day_of_week, hour=cfg.hour, minute=cfg.minute, timezone=cfg.timezone)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return trigger.get_next_fire_time(None, now)


def run_strategy_review(
    settings: Settings, db: Database, alerts: AlertManager | None = None, store: ReviewStore | None = None, *,
    now: datetime | None = None, trigger: str = "schedule",
) -> ReviewResult | None:
    """Review every watchlist symbol, apply allowed changes, save history, notify Discord.

    Returns ``None`` if another review is already running. Paper only: it only writes JSON overrides.
    """
    store = store or ReviewStore.from_settings(settings)
    now = now or datetime.now(timezone.utc)
    if not store.try_start(now):
        logger.warning("Strategy review already running - skipped")
        return None
    cfg = settings.strategy_review
    try:
        logger.info("=== Strategy review start ({} train / {} test months) ===", cfg.train_months, cfg.test_months)
        logger.info("Score formula: {}", SCORE_FORMULA)
        reviews: list[SymbolReview] = []
        for item in settings.watchlist:
            try:
                with db.session() as s:
                    stock = repo.get_stock(s, item.symbol)
                    df = repo.get_price_df(s, stock.id, "1d") if stock else pd.DataFrame()
                rev = review_symbol(item, df, settings, store, now=now,
                                    has_position=has_open_exposure(db, settings, item.symbol))
            except Exception as exc:  # one bad symbol must not kill the whole review
                logger.exception("Review failed for {}", item.symbol)
                rev = SymbolReview(item.symbol, effective_strategy(settings, store, item), skipped=f"error: {exc}")
            _apply(rev, store, now)
            logger.info("Review {}: {}", rev.symbol, rev.decision.reason if rev.decision else rev.skipped)
            reviews.append(rev)
        result = ReviewResult(now, reviews, trigger)
        nxt = next_review_at(cfg, now)
        store.save_last_review({
            "at": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "trigger": trigger,
            "formula": SCORE_FORMULA, "train_months": cfg.train_months, "test_months": cfg.test_months,
            "next_at": nxt.isoformat(timespec="seconds") if nxt else None,
            "symbols": [r.to_dict() for r in reviews],
        })
        if alerts is not None:
            alerts.report("สอบกลยุทธ์รายสัปดาห์ (PAPER)", format_review(result, cfg))
        return result
    finally:
        store.finish()


def _apply(rev: SymbolReview, store: ReviewStore, now: datetime) -> None:
    d = rev.decision
    if d is None:
        return
    if d.action == "change" and d.candidate:
        store.set_override(rev.symbol, d.candidate, previous=rev.current, changed_at=now)
        store.append_history({
            "date": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "symbol": rev.symbol,
            "from": rev.current, "to": d.candidate, "reason": d.reason, "windows": rev.windows,
            "scores": {k: v.to_dict() for k, v in rev.test.items()},
            "train_scores": {k: v.to_dict() for k, v in rev.train.items()},
        })
    elif d.action == "pending" and d.candidate:
        store.set_pending(rev.symbol, {"to": d.candidate, "since": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                       "reason": d.reason})
    else:
        store.clear_pending(rev.symbol)


def format_review(result: ReviewResult, cfg: StrategyReviewSettings) -> str:
    """Discord-sized summary (the notifier cuts at ~1900 chars): score table + decision per symbol."""
    names = cfg.strategies
    short = {"ma_crossover": "ma", "rsi": "rsi", "bollinger": "bb", "breakout": "brk", "momentum": "mom"}
    first = next((r for r in result.reviews if r.windows), None)
    lines = [f"ช่วงสอบ {first.windows['test']}" if first else "ไม่มีข้อมูลพอสอบ",
             "คะแนน (×100, ในวงเล็บ=จำนวนเทรด; * = ที่ใช้อยู่)", "```"]
    lines.append("symbol " + " ".join(f"{short.get(n, n[:4]):>9}" for n in names))
    for r in result.reviews:
        cells = []
        for n in names:
            sc = r.test.get(n)
            txt = "-" if sc is None or sc.score is None else f"{sc.score * 100:.1f}({sc.trades})"
            cells.append(f"{('*' if n == r.current else ' ') + txt:>9}")
        lines.append(f"{r.symbol:<6} " + " ".join(cells))
    lines.append("```")
    for r in result.reviews:
        mark = "[เปลี่ยน]" if r.decision and r.decision.changed else "[รอเปลี่ยน]" if r.decision and r.decision.action == "pending" else "[คงเดิม]"
        lines.append(f"{mark} {r.symbol}: {r.decision.reason if r.decision else r.skipped}")
    lines.append(f"สูตร: {SCORE_FORMULA}")
    return "\n".join(lines)
