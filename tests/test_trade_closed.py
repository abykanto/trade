"""State-machine tests for TradeManager._handle_trade_closed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.core.database import get_session_local, init_db, reset_db
from src.core.models import (
    ExecutionState,
    OpenPosition,
    TradeAttempt,
    TradeEvent,
    TradeIdea,
    TradeState,
)
from src.main import TradeManager
from src.market.contract import SymbolContract


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
    return TradeManager(db_url="sqlite:///:memory:")


def _seed_open_trade(
    session,
    *,
    consumed_risk: float = 0.0,
    retries_used: int = 0,
    realized_pnl: float = 0.0,
    max_idea_risk: float = 10.0,
    max_retries: int = 5,
) -> tuple[TradeIdea, TradeAttempt, OpenPosition]:
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
        max_idea_risk=max_idea_risk,
        max_retries=max_retries,
        consumed_risk=consumed_risk,
        retries_used=retries_used,
        realized_pnl=realized_pnl,
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
        mt5_ticket=9001,
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


async def _close(
    manager: TradeManager,
    session,
    idea: TradeIdea,
    attempt: TradeAttempt,
    open_pos: OpenPosition,
    *,
    exit_reason: str,
    close_price: float,
    chop_sl: float = 4359.0,
    elapsed_time=None,
):
    contract = SymbolContract.for_symbol("XAUUSD")
    with (
        patch.object(manager, "_arm_reentry_pending", new_callable=AsyncMock) as arm,
        patch("src.main.asyncio.sleep", new_callable=AsyncMock),
    ):
        await manager._handle_trade_closed(
            session,
            idea,
            attempt,
            open_pos,
            "XAUUSD",
            close_price,
            chop_sl,
            exit_reason,
            contract,
            elapsed_time=elapsed_time,
        )
        session.refresh(idea)
        session.refresh(attempt)
        return idea, attempt, arm


def test_take_profit_marks_tp_reached(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(db_session)

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="TAKE_PROFIT", close_price=4370.0,
        )
    )

    assert idea.state == TradeState.TP_REACHED.value
    assert attempt.exit_reason == "TAKE_PROFIT"
    assert idea.realized_pnl > 0
    arm.assert_not_called()


def test_chop_exit_requeues_when_budget_remains(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(db_session)

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="CHOP_EXIT", close_price=4359.0,
        )
    )

    assert idea.state == TradeState.WAITING_FOR_REENTRY.value
    assert idea.consumed_risk == pytest.approx(5.0)
    assert idea.retries_used == 1
    assert idea.hard_stop == idea.original_hard_stop
    arm.assert_called_once()


def test_chop_exit_invalidates_when_risk_exhausted(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(
        db_session, consumed_risk=9.0, max_idea_risk=10.0,
    )

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="CHOP_EXIT", close_price=4359.0,
        )
    )

    assert idea.state == TradeState.IDEA_INVALIDATED.value
    assert idea.consumed_risk >= 10.0
    arm.assert_not_called()


def test_trailing_recovery_target_completes_idea(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(
        db_session, consumed_risk=3.0, realized_pnl=-3.0,
    )

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="TRAILING_STOP", close_price=4420.0,
        )
    )

    assert idea.state == TradeState.TP_REACHED.value
    assert idea.realized_pnl >= 0.10
    arm.assert_not_called()
    events = db_session.query(TradeEvent).filter_by(
        trade_idea_id=idea.id, event_type="RECOVERY_TARGET",
    ).all()
    assert len(events) == 1


def test_trailing_stop_requeues_when_recovery_not_met(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(
        db_session, consumed_risk=3.0, realized_pnl=-3.0,
    )

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="TRAILING_STOP", close_price=4360.04,
        )
    )

    assert idea.state == TradeState.WAITING_FOR_REENTRY.value
    assert idea.retries_used == 1
    assert idea.realized_pnl < 0.10
    arm.assert_called_once()


def test_trailing_stop_first_profit_still_requeues(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(db_session)

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="TRAILING_STOP", close_price=4370.0,
        )
    )

    assert idea.state == TradeState.WAITING_FOR_REENTRY.value
    assert idea.retries_used == 1
    assert idea.realized_pnl > 0
    arm.assert_called_once()


def test_trailing_stop_invalidates_at_max_retries(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(
        db_session, retries_used=4, max_retries=5,
    )

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="TRAILING_STOP", close_price=4359.0,
        )
    )

    assert idea.state == TradeState.IDEA_INVALIDATED.value
    assert idea.retries_used == 5
    arm.assert_not_called()


def test_max_hold_exceeded_invalidates(manager, db_session):
    from datetime import timedelta

    idea, attempt, open_pos = _seed_open_trade(db_session)

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="MAX_HOLD_EXCEEDED",
            close_price=4360.0,
            elapsed_time=timedelta(hours=9),
        )
    )

    assert idea.state == TradeState.IDEA_INVALIDATED.value
    arm.assert_not_called()


def test_mt5_stop_classified_as_chop_exit(manager, db_session):
    idea, attempt, open_pos = _seed_open_trade(db_session)

    idea, attempt, arm = asyncio.run(
        _close(
            manager, db_session, idea, attempt, open_pos,
            exit_reason="MT5_STOP", close_price=4359.0,
        )
    )

    assert attempt.exit_reason == "CHOP_EXIT"
    assert idea.state == TradeState.WAITING_FOR_REENTRY.value
    arm.assert_called_once()


def test_register_trade_open_sets_opened_at_on_fill(manager, db_session):
    from datetime import timedelta

    from src.core.database import as_utc
    from src.core.models import utcnow

    idea, attempt, open_pos = _seed_open_trade(db_session)
    stale = utcnow() - timedelta(hours=10)
    attempt.opened_at = stale
    db_session.commit()

    contract = SymbolContract.for_symbol("XAUUSD")
    with (
        patch.object(manager, "_ensure_chop_stop_on_position", new_callable=AsyncMock, return_value=True),
        patch.object(manager, "_arm_reentry_pending", new_callable=AsyncMock),
        patch("src.main.asyncio.sleep", new_callable=AsyncMock),
    ):
        asyncio.run(
            manager._register_trade_open(
                db_session, idea, attempt, "XAUUSD", 9001, contract,
            )
        )
    db_session.refresh(attempt)
    assert as_utc(attempt.opened_at) > stale
