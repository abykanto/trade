"""Tests for external MT5 order cancel → idea invalidation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.core.database import get_session_local, init_db, reset_db
from src.core.models import ExecutionState, TradeAttempt, TradeEvent, TradeIdea, TradeState
from src.main import TradeManager
from src.market.contract import SymbolContract
from src.market.order_outcome import PendingOrderOutcome


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
    m = TradeManager(db_url="sqlite:///:memory:")
    m.bridge.magic = 234000
    return m


def _pending_idea(session) -> tuple[TradeIdea, TradeAttempt]:
    idea = TradeIdea(
        symbol="XAUUSD",
        direction="BUY",
        source="test",
        original_entry=4031.19,
        original_hard_stop=4026.19,
        hard_stop=4026.19,
        take_profit=4051.19,
        entry_zone_low=4027.0,
        entry_zone_high=4035.0,
        max_idea_risk=2.5,
        max_retries=25,
        state=TradeState.PENDING_ORDER_PLACED.value,
    )
    session.add(idea)
    session.flush()
    attempt = TradeAttempt(
        trade_idea_id=idea.id,
        attempt_number=1,
        execution_state=ExecutionState.SUBMITTED.value,
        entry_price=4031.19,
        volume=0.03,
        mt5_ticket=9001,
    )
    session.add(attempt)
    session.commit()
    return idea, attempt


def test_external_cancel_invalidates_idea(manager, db_session):
    idea, attempt = _pending_idea(db_session)
    outcome = PendingOrderOutcome(
        status="cancelled",
        order_comment="TradeIdeaBot_Pending",
        order_magic=234000,
    )
    manager.bridge.get_positions = AsyncMock(return_value=[])
    contract = SymbolContract.for_symbol("XAUUSD")

    handled = asyncio.run(
        manager._handle_pending_order_outcome(
            db_session, idea, attempt, "XAUUSD", outcome, contract,
        )
    )

    assert handled is True
    assert idea.state == TradeState.IDEA_INVALIDATED.value
    assert attempt.execution_state == ExecutionState.CANCELLED.value
    assert attempt.exit_reason == "EXTERNAL_CANCEL"
    events = db_session.query(TradeEvent).filter_by(trade_idea_id=idea.id).all()
    assert any(e.event_type == "EXTERNAL_ORDER_CANCELLED" for e in events)


def test_bot_cancel_requeues_idea(manager, db_session):
    idea, attempt = _pending_idea(db_session)
    manager._bot_cancelled_tickets.add(9001)
    outcome = PendingOrderOutcome(
        status="cancelled",
        order_comment="TradeIdeaBot_Pending",
        order_magic=234000,
    )
    manager.bridge.get_positions = AsyncMock(return_value=[])
    contract = SymbolContract.for_symbol("XAUUSD")

    handled = asyncio.run(
        manager._handle_pending_order_outcome(
            db_session, idea, attempt, "XAUUSD", outcome, contract,
        )
    )

    assert handled is True
    assert idea.state == TradeState.WAITING_FOR_SETUP.value
    assert attempt.execution_state == ExecutionState.CANCELLED.value
    assert attempt.exit_reason == "MT5_ORDER_CANCELLED"


def test_non_bot_comment_cancel_invalidates(manager, db_session):
    idea, attempt = _pending_idea(db_session)
    outcome = PendingOrderOutcome(
        status="cancelled",
        order_comment="manual",
        order_magic=0,
    )
    manager.bridge.get_positions = AsyncMock(return_value=[])
    contract = SymbolContract.for_symbol("XAUUSD")

    handled = asyncio.run(
        manager._handle_pending_order_outcome(
            db_session, idea, attempt, "XAUUSD", outcome, contract,
        )
    )

    assert handled is True
    assert idea.state == TradeState.IDEA_INVALIDATED.value


def test_is_external_order_cancel_helper(manager):
    outcome = PendingOrderOutcome(
        status="cancelled",
        order_comment="TradeIdeaBot_Pending",
        order_magic=234000,
    )
    assert manager._is_external_order_cancel(9001, outcome) is True
    manager._bot_cancelled_tickets.add(9001)
    assert manager._is_external_order_cancel(9001, outcome) is False
