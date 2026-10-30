"""Initial schema: stocks, price_history, signals, orders, trades, portfolios,
portfolio_snapshots, daily_summary, backtest_runs, alert_logs.

Revision ID: 0001
Revises:
Create Date: 2026-10-30 13:23:08.085748
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('alert_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('alert_type', sa.String(length=32), nullable=False),
    sa.Column('level', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('channels', sa.String(length=128), nullable=True),
    sa.Column('delivered', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('backtest_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('strategy', sa.String(length=64), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('interval', sa.String(length=8), nullable=False),
    sa.Column('start', sa.DateTime(), nullable=False),
    sa.Column('end', sa.DateTime(), nullable=False),
    sa.Column('initial_capital', sa.Float(), nullable=False),
    sa.Column('final_equity', sa.Float(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('trades', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('portfolios',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.Enum('PAPER', 'LIVE', name='trading_mode', native_enum=False, length=20), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('initial_capital', sa.Float(), nullable=False),
    sa.Column('cash', sa.Float(), nullable=False),
    sa.Column('positions_value', sa.Float(), nullable=False),
    sa.Column('equity', sa.Float(), nullable=False),
    sa.Column('peak_equity', sa.Float(), nullable=False),
    sa.Column('realized_pnl', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('stocks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('market', sa.String(length=16), nullable=False),
    sa.Column('exchange', sa.String(length=32), nullable=True),
    sa.Column('sector', sa.String(length=128), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('added_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('symbol')
    )
    op.create_table('daily_summary',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('prev_close', sa.Float(), nullable=True),
    sa.Column('open', sa.Float(), nullable=False),
    sa.Column('high', sa.Float(), nullable=False),
    sa.Column('low', sa.Float(), nullable=False),
    sa.Column('close', sa.Float(), nullable=False),
    sa.Column('volume', sa.Float(), nullable=False),
    sa.Column('change', sa.Float(), nullable=True),
    sa.Column('percent_change', sa.Float(), nullable=True),
    sa.Column('direction', sa.Enum('UP', 'DOWN', 'UNCHANGED', name='direction', native_enum=False, length=20), nullable=True),
    sa.Column('intraday_range_pct', sa.Float(), nullable=True),
    sa.Column('last_signal', sa.String(length=8), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stock_id', 'date', name='uq_daily_summary_stock_date')
    )
    with op.batch_alter_table('daily_summary', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_daily_summary_date'), ['date'], unique=False)

    op.create_table('portfolio_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('cash', sa.Float(), nullable=False),
    sa.Column('positions_value', sa.Float(), nullable=False),
    sa.Column('equity', sa.Float(), nullable=False),
    sa.Column('drawdown_pct', sa.Float(), nullable=False),
    sa.Column('open_positions', sa.Integer(), nullable=False),
    sa.Column('positions', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('portfolio_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_snapshot_portfolio_ts', ['portfolio_id', 'timestamp'], unique=False)

    op.create_table('price_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('interval', sa.String(length=8), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('open', sa.Float(), nullable=False),
    sa.Column('high', sa.Float(), nullable=False),
    sa.Column('low', sa.Float(), nullable=False),
    sa.Column('close', sa.Float(), nullable=False),
    sa.Column('adj_close', sa.Float(), nullable=True),
    sa.Column('volume', sa.Float(), nullable=False),
    sa.Column('prev_close', sa.Float(), nullable=True),
    sa.Column('change', sa.Float(), nullable=True),
    sa.Column('percent_change', sa.Float(), nullable=True),
    sa.Column('direction', sa.Enum('UP', 'DOWN', 'UNCHANGED', name='direction', native_enum=False, length=20), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stock_id', 'interval', 'timestamp', name='uq_price_stock_interval_ts')
    )
    with op.batch_alter_table('price_history', schema=None) as batch_op:
        batch_op.create_index('ix_price_stock_interval_ts', ['stock_id', 'interval', 'timestamp'], unique=False)

    op.create_table('signals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('strategy', sa.String(length=64), nullable=False),
    sa.Column('signal_type', sa.Enum('BUY', 'SELL', 'HOLD', name='signal_type', native_enum=False, length=20), nullable=False),
    sa.Column('interval', sa.String(length=8), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('indicators', sa.JSON(), nullable=True),
    sa.Column('acted_on', sa.Boolean(), nullable=False),
    sa.Column('action_note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stock_id', 'strategy', 'interval', 'timestamp', name='uq_signal_bar')
    )
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.create_index('ix_signals_stock_ts', ['stock_id', 'timestamp'], unique=False)

    op.create_table('trades',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('strategy_used', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.Enum('PAPER', 'LIVE', name='trading_mode', native_enum=False, length=20), nullable=False),
    sa.Column('status', sa.Enum('OPEN', 'CLOSED', name='trade_status', native_enum=False, length=20), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('entry_time', sa.DateTime(), nullable=False),
    sa.Column('exit_price', sa.Float(), nullable=True),
    sa.Column('exit_time', sa.DateTime(), nullable=True),
    sa.Column('stop_loss', sa.Float(), nullable=True),
    sa.Column('take_profit', sa.Float(), nullable=True),
    sa.Column('entry_commission', sa.Float(), nullable=False),
    sa.Column('exit_commission', sa.Float(), nullable=False),
    sa.Column('pnl', sa.Float(), nullable=True),
    sa.Column('pnl_pct', sa.Float(), nullable=True),
    sa.Column('exit_reason', sa.Enum('SIGNAL', 'STOP_LOSS', 'TAKE_PROFIT', 'MANUAL', name='exit_reason', native_enum=False, length=20), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.create_index('ix_trades_portfolio_status', ['portfolio_id', 'status'], unique=False)

    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('stock_id', sa.Integer(), nullable=False),
    sa.Column('trade_id', sa.Integer(), nullable=True),
    sa.Column('signal_id', sa.Integer(), nullable=True),
    sa.Column('mode', sa.Enum('PAPER', 'LIVE', name='trading_mode', native_enum=False, length=20), nullable=False),
    sa.Column('side', sa.Enum('BUY', 'SELL', name='order_side', native_enum=False, length=20), nullable=False),
    sa.Column('order_type', sa.Enum('MARKET', 'LIMIT', name='order_type', native_enum=False, length=20), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('limit_price', sa.Float(), nullable=True),
    sa.Column('reference_price', sa.Float(), nullable=True),
    sa.Column('status', sa.Enum('PENDING', 'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED', name='order_status', native_enum=False, length=20), nullable=False),
    sa.Column('filled_quantity', sa.Float(), nullable=False),
    sa.Column('filled_price', sa.Float(), nullable=True),
    sa.Column('commission', sa.Float(), nullable=False),
    sa.Column('slippage', sa.Float(), nullable=False),
    sa.Column('broker', sa.String(length=32), nullable=False),
    sa.Column('broker_order_id', sa.String(length=128), nullable=True),
    sa.Column('purpose', sa.String(length=32), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('filled_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ),
    sa.ForeignKeyConstraint(['stock_id'], ['stocks.id'], ),
    sa.ForeignKeyConstraint(['trade_id'], ['trades.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.create_index('ix_orders_portfolio_status', ['portfolio_id', 'status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_index('ix_orders_portfolio_status')

    op.drop_table('orders')
    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.drop_index('ix_trades_portfolio_status')

    op.drop_table('trades')
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.drop_index('ix_signals_stock_ts')

    op.drop_table('signals')
    with op.batch_alter_table('price_history', schema=None) as batch_op:
        batch_op.drop_index('ix_price_stock_interval_ts')

    op.drop_table('price_history')
    with op.batch_alter_table('portfolio_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_snapshot_portfolio_ts')

    op.drop_table('portfolio_snapshots')
    with op.batch_alter_table('daily_summary', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_daily_summary_date'))

    op.drop_table('daily_summary')
    op.drop_table('stocks')
    op.drop_table('portfolios')
    op.drop_table('backtest_runs')
    op.drop_table('alert_logs')
