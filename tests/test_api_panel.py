"""API tests for control panel endpoints."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.server import app
from src.core.database import init_db, get_session_local, reset_db
from src.core.models import TradeIdea, TradeState


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    reset_db()
    init_db(f"sqlite:///{tmp_path}/api_test.db")
    yield
    reset_db()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    SessionLocal = get_session_local()
    session = SessionLocal()
    yield session
    session.close()


def _seed_idea(session, *, state=TradeState.WAITING_FOR_SETUP.value, pnl=0.0):
    idea = TradeIdea(
        symbol="XAUUSD",
        direction="SELL",
        source=f"test_{state}",
        signal_fingerprint=f"fp_{state}_{pnl}",
        original_entry=4240.0,
        original_hard_stop=4245.0,
        hard_stop=4245.0,
        take_profit=4220.0,
        entry_zone_low=4235.0,
        entry_zone_high=4245.0,
        max_retries=5,
        max_idea_risk=2.5,
        lot_size=0.03,
        realized_pnl=pnl,
        state=state,
    )
    session.add(idea)
    session.commit()
    return idea


def test_post_signal_returns_idea_payload(client):
    res = client.post("/signals", json={
        "symbol": "XAUUSD",
        "direction": "SELL",
        "entry_price": 4240.0,
        "hard_stop": 4245.0,
        "take_profit": 4220.0,
        "max_idea_risk": 2.5,
        "lot_size": 0.03,
        "source": "panel_test_1",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "accepted"
    assert data["idea_id"] > 0
    assert data["idea"]["symbol"] == "XAUUSD"
    assert data["idea"]["state"] == TradeState.WAITING_FOR_SETUP.value


def test_list_active_ideas(client, db_session):
    _seed_idea(db_session, state=TradeState.TRADE_OPEN.value)
    _seed_idea(db_session, state=TradeState.TP_REACHED.value)

    res = client.get("/ideas?status=active&symbol=XAUUSD")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["ideas"]) == 1
    assert data["ideas"][0]["state"] == TradeState.TRADE_OPEN.value


def test_panel_summary(client, db_session):
    _seed_idea(db_session, state=TradeState.TRADE_OPEN.value, pnl=-1.5)
    _seed_idea(db_session, state=TradeState.WAITING_FOR_SETUP.value)

    res = client.get("/panel/summary")
    assert res.status_code == 200
    data = res.json()
    assert data["active_count"] == 2
    assert data["open_trades"] == 1
    assert data["waiting_count"] == 1


def test_panel_route_serves_html(client):
    res = client.get("/panel")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert "XAUUSD control panel" in res.text
