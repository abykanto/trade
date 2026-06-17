from datetime import datetime, timezone
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

_engine = None
_SessionLocal = None


def utcnow():
    """Timezone-aware UTC now, replacing deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalize SQLite datetimes (naive) to timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def init_db(db_url="sqlite:///trade_ideas.db"):
    """Initialize the database engine (singleton). Returns the engine.

    Calling this multiple times with the same URL returns the cached engine.
    Pass a different URL (e.g. sqlite:///:memory: for tests) to force a new engine.
    """
    global _engine, _SessionLocal

    # Allow re-init with a different URL (tests use :memory:)
    if _engine is not None and str(_engine.url) == db_url:
        return _engine

    _engine = create_engine(db_url, pool_pre_ping=True)

    # Enable WAL mode and busy timeout for SQLite crash safety
    if db_url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session_local(engine=None):
    """Return the shared session factory. Must call init_db() first."""
    global _SessionLocal
    if _SessionLocal is None:
        if engine is not None:
            _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        else:
            raise RuntimeError("Database not initialized. Call init_db() first.")
    return _SessionLocal


def reset_db():
    """Reset the singleton state. Used in tests."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
