import pytest

from config.settings import RiskSettings
from execution.broker import OrderRequest, PaperBroker
from execution.costs import CostModel
from risk.manager import AccountState, RiskManager


def state(**kw) -> AccountState:
    base = {"equity": 100_000, "cash": 100_000, "peak_equity": 100_000, "day_start_equity": 100_000, "open_positions": 0}
    base.update(kw)
    return AccountState(**base)


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager(RiskSettings(position_size_pct=0.1, risk_per_trade_pct=0.01, stop_loss_pct=0.05,
                                    take_profit_pct=0.1, max_daily_loss_pct=0.03, max_open_positions=2,
                                    max_drawdown_pct=0.15))


def test_gates(rm):
    assert rm.can_open_position(state()).allowed
    assert not rm.can_open_position(state(open_positions=2)).allowed
    d = rm.can_open_position(state(equity=96_000, day_start_equity=100_000))
    assert not d.allowed and "daily loss" in d.reason
    d = rm.can_open_position(state(equity=84_000, peak_equity=100_000, day_start_equity=84_000))
    assert not d.allowed and "drawdown" in d.reason


def test_position_size_respects_pct_risk_cash_and_lots(rm):
    # 10% of 100k at $100 = 100 shares; risk 1% / $5 stop distance = 200 shares -> 100
    assert rm.position_size(state(), 100, stop_price=95) == 100
    # tight cash limits size
    assert rm.position_size(state(cash=2_000), 100, stop_price=95) == 20
    # wide stop: 1000 risk / $20 = 50 shares
    assert rm.position_size(state(), 100, stop_price=80) == 50
    # Thai board lot of 100
    assert rm.position_size(state(), 30, stop_price=28.5, lot_size=100) == 300
    assert rm.position_size(state(cash=1_000), 30, lot_size=100) == 0


def test_stop_and_target_levels(rm):
    assert rm.stop_loss_price(100) == pytest.approx(95)
    assert rm.take_profit_price(100) == pytest.approx(110)


def test_check_exit_rules():
    assert RiskManager.check_exit(96, 104, 95, 110) is None
    assert RiskManager.check_exit(94, 104, 95, 110) == ("STOP_LOSS", 95)
    assert RiskManager.check_exit(96, 111, 95, 110) == ("TAKE_PROFIT", 110)
    assert RiskManager.check_exit(94, 111, 95, 110)[0] == "STOP_LOSS"  # both -> stop first
    assert RiskManager.check_exit(90, 92, 95, 110, open_=91) == ("STOP_LOSS", 91)  # gap down


def test_cost_model():
    c = CostModel(commission_pct=0.0015, min_commission=50, commission_vat_pct=0.07, slippage_pct=0.001)
    assert c.fill_price(100, "BUY") == pytest.approx(100.1)
    assert c.fill_price(100, "SELL") == pytest.approx(99.9)
    assert c.commission(10_000) == pytest.approx(50 * 1.07)  # minimum applies
    assert c.commission(100_000) == pytest.approx(150 * 1.07)
    assert c.commission(0) == 0


def test_paper_broker_market_and_limit():
    broker = PaperBroker(CostModel(commission_pct=0.001, slippage_pct=0.001))
    r = broker.place_order(OrderRequest("AAA", "BUY", 10, reference_price=100))
    assert r.filled and r.filled_price == pytest.approx(100.1) and r.commission == pytest.approx(1.001)
    assert r.slippage_cost == pytest.approx(1.0)

    pending = broker.place_order(OrderRequest("AAA", "BUY", 10, "LIMIT", limit_price=99, reference_price=100))
    assert pending.status == "PENDING"
    filled = broker.check_order(pending.broker_order_id, OrderRequest("AAA", "BUY", 10, "LIMIT", limit_price=99), 98.5)
    assert filled.filled and filled.filled_price <= 99

    rejected = broker.place_order(OrderRequest("AAA", "SELL", 5))
    assert rejected.status == "REJECTED"


def test_order_request_validation():
    with pytest.raises(ValueError):
        OrderRequest("AAA", "HOLD", 1)
    with pytest.raises(ValueError):
        OrderRequest("AAA", "BUY", 0)
    with pytest.raises(ValueError):
        OrderRequest("AAA", "BUY", 1, "LIMIT")
