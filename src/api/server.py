from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
import hashlib
from datetime import timedelta
import logging

from src.core.models import TradeIdea, TradeEvent, utcnow
from src.core.database import init_db, get_session_local

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                    handlers=[logging.FileHandler("api_server.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

app = FastAPI(title="Trade Idea Management API")

# Initialize database using the singleton
init_db()

class SignalPayload(BaseModel):
    symbol: str
    direction: str
    entry_price: float
    hard_stop: float
    take_profit: float
    max_idea_risk: float = Field(..., gt=0.0)
    max_retries: int = Field(default=3, gt=0)
    source: str = "API"
    entry_zone_size: float = Field(default=0.001, ge=0.0) # E.g., 0.1%
    expires_in_days: int = Field(default=5, ge=1)
    external_reference: str = None

def get_db():
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/signals")
def receive_signal(payload: SignalPayload, db = Depends(get_db)):
    direction = payload.direction.upper()
    if direction not in ["BUY", "SELL"]:
        raise HTTPException(status_code=400, detail="Invalid direction")
        
    if direction == "BUY" and payload.hard_stop >= payload.entry_price:
        raise HTTPException(status_code=400, detail="BUY stop loss must be below entry")
        
    if direction == "SELL" and payload.hard_stop <= payload.entry_price:
        raise HTTPException(status_code=400, detail="SELL stop loss must be above entry")

    # Generate persistent fingerprint
    raw = f"{payload.symbol}_{direction}_{payload.entry_price}_{payload.hard_stop}_{payload.take_profit}_{payload.source}"
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()

    # Check for duplicate
    existing = db.query(TradeIdea).filter(TradeIdea.signal_fingerprint == fingerprint).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate signal ignored")

    # Calculate zone based on configured size percentage
    if direction == "BUY":
        zone_high = payload.entry_price * (1 + payload.entry_zone_size)
        zone_low = payload.entry_price * (1 - payload.entry_zone_size)
    else:
        # For SELL, the logic is identical to BUY in terms of the range of prices considered "in zone"
        zone_high = payload.entry_price * (1 + payload.entry_zone_size)
        zone_low = payload.entry_price * (1 - payload.entry_zone_size)
        
    expires_at = utcnow() + timedelta(days=payload.expires_in_days)

    new_idea = TradeIdea(
        symbol=payload.symbol,
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
        
        expires_at=expires_at
    )
    
    db.add(new_idea)
    db.commit()
    db.refresh(new_idea)
    
    # Log ingestion event
    event = TradeEvent(
        trade_idea_id=new_idea.id,
        event_type="IDEA_CREATED",
        event_data=payload.json()
    )
    db.add(event)
    db.commit()

    logger.info(f"Accepted new trade idea for {payload.symbol}")
    return {"status": "accepted", "idea_id": new_idea.id}

@app.get("/ideas/{idea_id}")
def get_idea(idea_id: int, db = Depends(get_db)):
    idea = db.query(TradeIdea).get(idea_id)
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea
