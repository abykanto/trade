from enum import Enum
from datetime import datetime, timezone
from dataclasses import dataclass, field
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, 
    Index, CheckConstraint
)
from sqlalchemy.orm import relationship

from src.core.database import Base


def utcnow():
    """Timezone-aware UTC now for SQLAlchemy defaults."""
    return datetime.now(timezone.utc)


class TradeState(str, Enum):
    WAITING_FOR_SETUP = "WAITING_FOR_SETUP"
    PENDING_ORDER_PLACED = "PENDING_ORDER_PLACED"
    ENTRY_ZONE_REACHED = "ENTRY_ZONE_REACHED"
    TRADE_OPEN = "TRADE_OPEN"
    EARLY_EXIT = "EARLY_EXIT"
    WAITING_FOR_REENTRY = "WAITING_FOR_REENTRY"
    TP_REACHED = "TP_REACHED"
    RISK_EXHAUSTED = "RISK_EXHAUSTED"
    IDEA_EXPIRED = "IDEA_EXPIRED"
    IDEA_INVALIDATED = "IDEA_INVALIDATED"


class ExecutionState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeIdea(Base):
    __tablename__ = "trade_ideas"
    __table_args__ = (
        Index('idx_ideas_symbol_state', 'symbol', 'state'),
        Index('idx_ideas_updated_pnl', 'updated_at', 'realized_pnl'),
        CheckConstraint("direction IN ('BUY', 'SELL')", name='check_direction'),
        CheckConstraint("max_idea_risk > 0", name='check_positive_risk'),
        CheckConstraint("max_retries > 0", name='check_positive_retries'),
        CheckConstraint("consumed_risk >= 0", name='check_positive_consumed'),
        CheckConstraint("entry_zone_low < entry_zone_high", name='check_zone_order'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    source = Column(String, nullable=False)
    external_reference = Column(String)
    signal_fingerprint = Column(String, unique=True)  # Persistent duplicate check
    
    original_entry = Column(Float, nullable=False)
    original_hard_stop = Column(Float, nullable=False)  # Safety anchor
    hard_stop = Column(Float, nullable=False)  # Mutated by trailing stop
    take_profit = Column(Float, nullable=False)
    entry_zone_low = Column(Float, nullable=False)
    entry_zone_high = Column(Float, nullable=False)
    lot_size = Column(Float)  # Persistent position size record
    
    max_retries = Column(Integer, nullable=False)
    retries_used = Column(Integer, default=0)
    max_idea_risk = Column(Float, nullable=False)
    consumed_risk = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    
    state = Column(String, nullable=False, default=TradeState.WAITING_FOR_SETUP.value)
    
    created_at = Column(DateTime, default=utcnow, nullable=False)
    expires_at = Column(DateTime)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    version = Column(Integer, default=1, nullable=False)

    attempts = relationship("TradeAttempt", back_populates="idea")
    events = relationship("TradeEvent", back_populates="idea")


class TradeAttempt(Base):
    __tablename__ = "trade_attempts"
    __table_args__ = (
        Index('idx_attempts_idea_id', 'trade_idea_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_idea_id = Column(Integer, ForeignKey("trade_ideas.id"), nullable=False)
    mt5_ticket = Column(Integer)  # Ticket usually int
    attempt_number = Column(Integer, nullable=False)
    
    execution_state = Column(String, default=ExecutionState.PENDING.value)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    volume = Column(Float)
    slippage = Column(Float)  # Abs distance between requested and filled
    pnl = Column(Float)
    exit_reason = Column(String)
    
    opened_at = Column(DateTime, default=utcnow, nullable=False)
    closed_at = Column(DateTime)
    version = Column(Integer, default=1, nullable=False)
    
    idea = relationship("TradeIdea", back_populates="attempts")


class TradeEvent(Base):
    __tablename__ = "trade_events"
    __table_args__ = (
        Index('idx_events_idea_id', 'trade_idea_id'),
        Index('idx_events_type', 'event_type'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_idea_id = Column(Integer, ForeignKey("trade_ideas.id"), nullable=False)
    event_type = Column(String, nullable=False)
    event_data = Column(String)  # JSON serialized data
    created_at = Column(DateTime, default=utcnow, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    
    idea = relationship("TradeIdea", back_populates="events")


class OpenPosition(Base):
    __tablename__ = "open_positions"
    __table_args__ = (
        Index('idx_open_positions_idea_id', 'trade_idea_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_idea_id = Column(Integer, ForeignKey("trade_ideas.id"), nullable=False)
    mt5_ticket = Column(Integer, nullable=False, unique=True)
    symbol = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    volume = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    
    current_stop = Column(Float)
    current_tp = Column(Float)
    trailing_stop = Column(Float)
    
    opened_at = Column(DateTime, default=utcnow, nullable=False)
    version = Column(Integer, default=1, nullable=False)


@dataclass
class SymbolState:
    symbol: str
    latest_bid: float = 0.0
    latest_ask: float = 0.0
    active_ideas: list = field(default_factory=list)
    open_positions: list = field(default_factory=list)
    last_tick_time: float = 0.0


@dataclass
class ExecutionRequest:
    idea_id: int
    symbol: str
    direction: str
    volume: float
    price: float
    sl: float
    tp: float
    attempt_number: int
    current_price: float = 0.0
