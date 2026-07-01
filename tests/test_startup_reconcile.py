"""Startup reconciliation: orphan MT5 orders and missing-position PnL."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import get_session_local, init_db, reset_db
from src.core.models import (
    ExecutionState,
    OpenPosition,
    TradeAttempt,
    TradeIdea,
    TradeState,
)
from src.execution.bridge import ConnectionState
from src.main import TradeManager
from src.market.deal_history import PositionCloseDetails


@pytest.fixture(autouse=True)
def setup_db():
    reset_db()
    engine = init_db("sqlite:///:memory:")
    yield engine
    reset_db()


@pytest.fixture
def db_session(setup_db):
    session = get_session_local(setup_db)()
    yield session
    session.close()


@pytest.fixture
def manager(setup_db):
    mgr = TradeManager(db_url="sqlite:///:memory:")
    mgr.bridge.connection_state = ConnectionState.ACTIVE
    return mgr


def _seed_submitting_idea(session, *, entry=4360.0, volume=0.05):
    idea = TradeIdea(
        symbol="XAUUSD",
        direction="BUY",
        source="test",
        original_entry=entry,
        original_hard_stop=4345.0,
        hard_stop=4359.0,
        take_profit=4370.0,
        entry_zone_low=4350.0,
        entry_zone_high=4370.0,
        max_idea_risk=10.0,
        max_retries=5,
        state=TradeState.SUBMITTING_ORDER.value,
    )
    session.add(idea)
    session.flush()
    attempt = TradeAttempt(
        trade_idea_id=idea.id,
        attempt_number=1,
        execution_state=ExecutionState.PENDING.value,
        entry_price=entry,
        volume=volume,
    )
    session.add(attempt)
    session.commit()
    return idea, attempt


def _seed_open_trade(session, *, ticket=9001):
    idea = TradeIdea(
        symbol="XAUUSD",
        direction="BUY",
        source="test",
        original_entry=4360.0,
        original_hard_stop=4345.0,
        hard_stop=4359.0,
        take_profit=4370.0,
        entry_zone_low=4350.0,
        entry_zone_high=4370.0,
        max_idea_risk=10.0,
        max_retries=5,
        state=TradeState.TRADE_OPEN.value,
    )
    session.add(idea)
    session.flush()
    attempt = TradeAttempt(
        trade_idea_id=idea.id,
        attempt_number=1,
        execution_state=ExecutionState.FILLED.value,
        entry_price=4360.0,
        volume=0.05,
    )
    session.add(attempt)
    session.flush()
    open_pos = OpenPosition(
        trade_idea_id=idea.id,
        mt5_ticket=ticket,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.05,
        entry_price=4360.0,
        current_stop=4359.0,
        current_tp=4370.0,
    )
    session.add(open_pos)
    session.commit()
    return idea, attempt, open_pos


def test_reconcile_orphan_order_links_pending_attempt(manager, db_session):
    idea, attempt = _seed_submitting_idea(db_session)
    orphan = SimpleNamespace(
        ticket=55501,
        symbol="XAUUSD",
        type=4,
        volume_current=0.05,
        price_open=4360.0,
    )

    manager.bridge.get_orders = AsyncMock(return_value=[orphan])
    manager.bridge.cancel_pending_order = AsyncMock()

    asyncio.run(manager._reconcile_orphan_orders_on_startup(db_session))
    db_session.commit()
    db_session.refresh(idea)
    db_session.refresh(attempt)

    assert attempt.mt5_ticket == 55501
    assert attempt.execution_state == ExecutionState.SUBMITTED.value
    assert idea.state == TradeState.PENDING_ORDER_PLACED.value
    manager.bridge.cancel_pending_order.assert_not_called()


def test_reconcile_orphan_order_cancels_unmatched(manager, db_session):
    _seed_submitting_idea(db_session)
    orphan = SimpleNamespace(
        ticket=55502,
        symbol="EURUSD",
        type=4,
        volume_current=0.05,
        price_open=1.10,
    )

    manager.bridge.get_orders = AsyncMock(return_value=[orphan])
    manager.bridge.cancel_pending_order = AsyncMock(return_value=MagicMock())

    asyncio.run(manager._reconcile_orphan_orders_on_startup(db_session))
    manager.bridge.cancel_pending_order.assert_called_once_with(55502)


def test_reconcile_missing_position_uses_deal_history_pnl(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(db_session, ticket=9001)

    manager.bridge.get_orders = AsyncMock(return_value=[])
    manager.bridge.get_positions = AsyncMock(return_value=[])
    manager.bridge.get_position_close_details = AsyncMock(
        return_value=PositionCloseDetails(
            close_price=4355.0,
            profit=-8.0,
            commission=-0.5,
            swap=0.0,
        )
    )

    asyncio.run(manager._reconcile_positions_on_startup())

    db_session.expire_all()
    idea = db_session.get(TradeIdea, idea.id)
    attempt = db_session.query(TradeAttempt).filter_by(trade_idea_id=idea.id).first()
    open_count = db_session.query(OpenPosition).count()

    assert open_count == 0
    assert attempt.execution_state == ExecutionState.CLOSED.value
    assert attempt.exit_price == 4355.0
    assert attempt.pnl == pytest.approx(-8.5)
    assert idea.realized_pnl == pytest.approx(-8.5)
    assert idea.state == TradeState.WAITING_FOR_REENTRY.value
