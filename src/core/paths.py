"""Runtime artifact paths — resolved from .env (single source of configuration)."""

from __future__ import annotations

import os
from pathlib import Path

from src.core.env import load_project_env

load_project_env()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _runtime_relative() -> str:
    return os.environ.get("RUNTIME_DIR", "tmp")


def resolve_env_path(env_key: str, default_relative: str) -> Path:
    return _resolve_path(os.environ.get(env_key) or default_relative)


RUNTIME_DIR = resolve_env_path("RUNTIME_DIR", "tmp")
TMP_DIR = RUNTIME_DIR  # backward-compatible alias
LOGS_DIR = resolve_env_path("LOGS_DIR", f"{_runtime_relative()}/logs")
RUN_DIR = resolve_env_path("RUN_DIR", f"{_runtime_relative()}/run")
PRICE_LOGS_DIR = resolve_env_path("PRICE_LOG_DIR", f"{_runtime_relative()}/data/price_logs")
DEFAULT_DB_PATH = resolve_env_path("DATABASE_PATH", "trade_ideas.db")


def sqlite_url(path: Path) -> str:
    """Build a SQLAlchemy SQLite URL for an absolute filesystem path."""
    return f"sqlite:///{path.resolve().as_posix()}"


def default_db_url() -> str:
    if url := os.environ.get("DATABASE_URL"):
        return url
    return sqlite_url(DEFAULT_DB_PATH)


DEFAULT_DB_URL = default_db_url()


def ensure_runtime_dirs() -> None:
    for directory in (LOGS_DIR, RUN_DIR, PRICE_LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
