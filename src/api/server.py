from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
import hashlib
from datetime import datetime, timedelta
import logging
from pathlib import Path

from src.core.models import TradeIdea, TradeEvent, TradeState, utcnow
from src.core.database import init_db, get_session_local
from src.core.paths import LOGS_DIR, ensure_runtime_dirs
from src.market.candles import load_xauusd_1m_candles

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"

ensure_runtime_dirs()
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                    handlers=[logging.FileHandler(LOGS_DIR / "api_server.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

app = FastAPI(title="Trade Idea Management API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database using the singleton
init_db()

_ACTIVE_STATES = frozenset({
    TradeState.WAITING_FOR_SETUP.value,
    TradeState.SUBMITTING_ORDER.value,
    TradeState.PENDING_ORDER_PLACED.value,
    TradeState.ENTRY_ZONE_REACHED.value,
    TradeState.TRADE_OPEN.value,
    TradeState.WAITING_FOR_REENTRY.value,
})

_TERMINAL_STATES = frozenset({
    TradeState.TP_REACHED.value,
    TradeState.RISK_EXHAUSTED.value,
    TradeState.IDEA_EXPIRED.value,
    TradeState.IDEA_INVALIDATED.value,
    TradeState.EARLY_EXIT.value,
})

class SignalPayload(BaseModel):
    symbol: str
    direction: str
    entry_price: float
    hard_stop: float
    take_profit: float
    max_idea_risk: float = Field(..., gt=0.0)
    max_retries: int = Field(default=25, gt=0)
    lot_size: float | None = Field(default=None, gt=0.0)
    source: str = "API"
    entry_zone_size: float = Field(default=0.001, gt=0.0)  # Fraction around entry (0.001 = 0.1%)
    expires_in_days: int = Field(default=5, ge=1)
    external_reference: str | None = None


class TradeIdeaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    direction: str
    source: str
    state: str
    original_entry: float
    original_hard_stop: float
    hard_stop: float
    take_profit: float
    entry_zone_low: float
    entry_zone_high: float
    lot_size: float | None
    max_retries: int
    retries_used: int
    max_idea_risk: float
    consumed_risk: float
    realized_pnl: float
    version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TradeIdeaListResponse(BaseModel):
    ideas: list[TradeIdeaResponse]
    total: int


class PanelSummaryResponse(BaseModel):
    active_count: int
    open_trades: int
    waiting_count: int
    completed_today: int
    total_realized_pnl: float


class SignalAcceptedResponse(BaseModel):
    status: str
    idea_id: int
    idea: TradeIdeaResponse


def get_db():
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _validate_signal_levels(direction: str, entry: float, hard_stop: float, take_profit: float):
    """Validate entry / hard_stop / take_profit geometry for BUY and SELL."""
    if direction == "BUY":
        if hard_stop >= entry:
            raise HTTPException(
                status_code=400,
                detail=(
                    "BUY: hard_stop must be below entry_price "
                    f"(got entry={entry}, hard_stop={hard_stop})"
                ),
            )
        if take_profit <= entry:
            raise HTTPException(
                status_code=400,
                detail=(
                    "BUY: take_profit must be above entry_price "
                    f"(got entry={entry}, take_profit={take_profit})"
                ),
            )
        return

    # SELL
    if hard_stop < entry and take_profit > entry:
        raise HTTPException(
            status_code=400,
            detail=(
                "Your levels match a BUY signal (hard_stop below entry, take_profit above entry) "
                f"but direction is SELL. Use direction BUY, or for SELL set hard_stop above entry "
                f"(e.g. {entry + 10}) and take_profit below entry (e.g. {entry - 10})."
            ),
        )
    if hard_stop <= entry:
        raise HTTPException(
            status_code=400,
            detail=(
                "SELL: hard_stop must be above entry_price "
                f"(got entry={entry}, hard_stop={hard_stop}). "
                f"Example: entry 4250 → hard_stop 4260, take_profit 4230."
            ),
        )
    if take_profit >= entry:
        raise HTTPException(
            status_code=400,
            detail=(
                "SELL: take_profit must be below entry_price "
                f"(got entry={entry}, take_profit={take_profit})"
            ),
        )

@app.post("/signals")
def receive_signal(payload: SignalPayload, db = Depends(get_db)):
    direction = payload.direction.upper()
    if direction not in ["BUY", "SELL"]:
        raise HTTPException(status_code=400, detail="Invalid direction")

    symbol = payload.symbol.upper()

    _validate_signal_levels(
        direction, payload.entry_price, payload.hard_stop, payload.take_profit
    )

    # Generate persistent fingerprint
    raw = f"{symbol}_{direction}_{payload.entry_price}_{payload.hard_stop}_{payload.take_profit}_{payload.source}"
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()

    # Check for duplicate
    existing = db.query(TradeIdea).filter(TradeIdea.signal_fingerprint == fingerprint).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate signal ignored")

    zone_high = payload.entry_price * (1 + payload.entry_zone_size)
    zone_low = payload.entry_price * (1 - payload.entry_zone_size)

    expires_at = utcnow() + timedelta(days=payload.expires_in_days)

    new_idea = TradeIdea(
        symbol=symbol,
        direction=direction,
        source=payload.source,
        external_reference=payload.external_reference,
        signal_fingerprint=fingerprint,
        
        original_entry=payload.entry_price,
        original_hard_stop=payload.hard_stop,
        hard_stop=payload.hard_stop,
        take_profit=payload.take_profit,
        
        entry_zone_low=zone_low,
        entry_zone_high=zone_high,
        
        max_idea_risk=payload.max_idea_risk,
        max_retries=payload.max_retries,
        lot_size=payload.lot_size,
        
        expires_at=expires_at
    )
    
    db.add(new_idea)
    db.commit()
    db.refresh(new_idea)
    
    # Log ingestion event
    event = TradeEvent(
        trade_idea_id=new_idea.id,
        event_type="IDEA_CREATED",
        event_data=payload.model_dump_json()
    )
    db.add(event)
    db.commit()

    logger.info(f"Accepted new trade idea for {payload.symbol}")
    return SignalAcceptedResponse(
        status="accepted",
        idea_id=new_idea.id,
        idea=TradeIdeaResponse.model_validate(new_idea),
    )


@app.get("/ideas", response_model=TradeIdeaListResponse)
def list_ideas(
    status: str = Query(default="active", pattern="^(active|terminal|all)$"),
    symbol: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
):
    q = db.query(TradeIdea)
    if symbol:
        q = q.filter(TradeIdea.symbol == symbol.upper())
    if status == "active":
        q = q.filter(TradeIdea.state.in_(_ACTIVE_STATES))
    elif status == "terminal":
        q = q.filter(TradeIdea.state.in_(_TERMINAL_STATES))
    total = q.count()
    ideas = q.order_by(TradeIdea.updated_at.desc()).limit(limit).all()
    return TradeIdeaListResponse(
        ideas=[TradeIdeaResponse.model_validate(i) for i in ideas],
        total=total,
    )


@app.get("/panel/summary", response_model=PanelSummaryResponse)
def panel_summary(db=Depends(get_db)):
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    active = db.query(TradeIdea).filter(TradeIdea.state.in_(_ACTIVE_STATES)).all()
    open_trades = sum(1 for i in active if i.state == TradeState.TRADE_OPEN.value)
    waiting = sum(
        1 for i in active
        if i.state in (
            TradeState.WAITING_FOR_SETUP.value,
            TradeState.WAITING_FOR_REENTRY.value,
        )
    )
    completed_today = db.query(TradeIdea).filter(
        TradeIdea.state.in_(_TERMINAL_STATES),
        TradeIdea.updated_at >= today,
    ).count()
    total_pnl = sum(i.realized_pnl for i in active)
    return PanelSummaryResponse(
        active_count=len(active),
        open_trades=open_trades,
        waiting_count=waiting,
        completed_today=completed_today,
        total_realized_pnl=round(total_pnl, 2),
    )


@app.get("/tools/xauusd-signal")
def xauusd_signal_tool():
    """XAUUSD control panel — chart, order placement, active ideas."""
    path = TOOLS_DIR / "xauusd_signal.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Tool page not found")
    return FileResponse(path, media_type="text/html")


@app.get("/panel")
def control_panel():
    """Alias for the XAUUSD trading control panel."""
    return xauusd_signal_tool()


@app.get("/tools/xauusd-candles")
def xauusd_candles(minutes: int = Query(default=180, ge=5, le=1440)):
    """1-minute OHLC from parquet tick logs for the signal chart tool."""
    return load_xauusd_1m_candles(lookback_minutes=minutes)


@app.get("/ideas/{idea_id}", response_model=TradeIdeaResponse)
def get_idea(idea_id: int, db = Depends(get_db)):
    idea = db.get(TradeIdea, idea_id)
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea
