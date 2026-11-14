"""Price guard: sanity checks run before every order is sent (paper trading).

Four checks, all always evaluated (so the UI can show every value, not just the failing one):

1. ``deviation``  - order price within ``max_deviation_pct`` of the latest market price
2. ``freshness``  - price comes from this cycle's fetch and the bar is not too old
3. ``jump``       - latest price did not jump more than ``max_daily_jump_pct`` vs the previous bar
4. ``order_value``- order value <= ``max_order_value`` (BUY only; exits must never be trapped)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from config.settings import PriceGuardSettings

CHECK_LABELS = {
    "deviation": "ห่างราคาตลาด",
    "freshness": "ข้อมูลรอบปัจจุบัน",
    "jump": "ไม่กระโดดจากวันก่อน",
    "order_value": "มูลค่าคำสั่ง",
}


@dataclass
class PriceContext:
    """What the engine knows about the market price at order time (naive-UTC datetimes)."""

    market_price: float  # latest stored price
    prev_close: float | None  # close of the bar before the latest one
    bar_time: datetime  # timestamp of the latest bar
    now: datetime  # engine clock
    interval: str = "1d"
    fetched_this_cycle: bool | None = True  # None = not applicable (e.g. manual close)


@dataclass
class CheckResult:
    key: str
    label: str
    ok: bool
    value: float | None  # measured value (pct as fraction, or money)
    limit: float | None
    detail: str  # human readable, Thai


@dataclass
class GuardDecision:
    allowed: bool
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def reason(self) -> str:
        return "; ".join(f"{c.label}: {c.detail}" for c in self.failed)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "checks": [asdict(c) for c in self.checks]}


class PriceGuard:
    def __init__(self, cfg: PriceGuardSettings) -> None:
        self.cfg = cfg

    def check(self, *, side: str, quantity: float, order_price: float, ctx: PriceContext) -> GuardDecision:
        if not self.cfg.enabled:
            return GuardDecision(True)
        checks = [
            self._deviation(order_price, ctx),
            self._freshness(ctx),
            self._jump(ctx),
            self._order_value(side, quantity, order_price),
        ]
        return GuardDecision(all(c.ok for c in checks), checks)

    # ------------------------------------------------------------------ checks
    def _deviation(self, order_price: float, ctx: PriceContext) -> CheckResult:
        limit = self.cfg.max_deviation_pct
        if ctx.market_price <= 0 or order_price <= 0:
            return CheckResult("deviation", CHECK_LABELS["deviation"], False, None, limit, "ราคาไม่ถูกต้อง (<= 0)")
        dev = abs(order_price / ctx.market_price - 1)
        # tiny epsilon so a deviation exactly at the limit is not failed by float noise
        ok = dev <= limit + 1e-12
        return CheckResult(
            "deviation", CHECK_LABELS["deviation"], ok, dev, limit,
            f"{dev:.2%} {'<=' if ok else '>'} {limit:.2%} (ตลาด {ctx.market_price:,.4f})",
        )

    def _freshness(self, ctx: PriceContext) -> CheckResult:
        label = CHECK_LABELS["freshness"]
        if ctx.fetched_this_cycle is False:
            return CheckResult("freshness", label, False, None, None, "ไม่ได้ดึงราคาใหม่ในรอบนี้ (ข้อมูลเก่าค้าง)")
        age_min = (ctx.now - ctx.bar_time).total_seconds() / 60
        if ctx.interval == "1d":
            limit, age = self.cfg.max_bar_age_days, age_min / 1440
            unit = "วัน"
        else:
            limit, age, unit = self.cfg.max_bar_age_minutes, age_min, "นาที"
        if age < -1e-9:  # bar timestamped in the future: clock or data problem
            return CheckResult("freshness", label, False, age, limit, f"bar อยู่ในอนาคต ({bar_str(ctx.bar_time)})")
        ok = age <= limit
        return CheckResult(
            "freshness", label, ok, age, limit,
            f"bar {bar_str(ctx.bar_time)} อายุ {age:.1f} {unit} {'<=' if ok else '>'} {limit:g}",
        )

    def _jump(self, ctx: PriceContext) -> CheckResult:
        limit = self.cfg.max_daily_jump_pct
        if not ctx.prev_close or ctx.prev_close <= 0:
            return CheckResult("jump", CHECK_LABELS["jump"], True, None, limit, "ไม่มีราคาวันก่อนให้เทียบ")
        jump = abs(ctx.market_price / ctx.prev_close - 1)
        ok = jump <= limit + 1e-12
        return CheckResult(
            "jump", CHECK_LABELS["jump"], ok, jump, limit,
            f"{jump:.2%} {'<=' if ok else '>'} {limit:.2%} (ก่อนหน้า {ctx.prev_close:,.4f}) {'' if ok else '- ข้อมูลน่าจะเสีย'}".strip(),
        )

    def _order_value(self, side: str, quantity: float, order_price: float) -> CheckResult:
        limit = self.cfg.max_order_value
        value = quantity * order_price
        if side.upper() != "BUY":
            return CheckResult("order_value", CHECK_LABELS["order_value"], True, value, limit,
                               f"{value:,.2f} (ขาย: ไม่จำกัดเพดาน)")
        ok = value <= limit + 1e-9
        return CheckResult(
            "order_value", CHECK_LABELS["order_value"], ok, value, limit,
            f"{value:,.2f} {'<=' if ok else '>'} {limit:,.2f}",
        )


def bar_str(ts: datetime) -> str:
    return f"{ts:%Y-%m-%d %H:%M}"
