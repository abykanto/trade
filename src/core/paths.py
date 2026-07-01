"""Runtime artifact paths — logs, PID files, parquet ticks, etc."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TMP_DIR = PROJECT_ROOT / "tmp"
LOGS_DIR = TMP_DIR / "logs"
RUN_DIR = TMP_DIR / "run"
DATA_DIR = TMP_DIR / "data"
PRICE_LOGS_DIR = DATA_DIR / "price_logs"
DEFAULT_DB_PATH = PROJECT_ROOT / "trade_ideas.db"


def sqlite_url(path: Path) -> str:
    """Build a SQLAlchemy SQLite URL for an absolute filesystem path."""
    return f"sqlite:///{path.resolve().as_posix()}"


DEFAULT_DB_URL = sqlite_url(DEFAULT_DB_PATH)


def ensure_runtime_dirs() -> None:
    for directory in (LOGS_DIR, RUN_DIR, PRICE_LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
