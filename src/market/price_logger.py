"""Append-only XAUUSD tick logger for validating trade logic against live prices."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.paths import PRICE_LOGS_DIR, resolve_env_path, _runtime_relative

logger = logging.getLogger(__name__)

DEFAULT_PARQUET_DIR = PRICE_LOGS_DIR
DEFAULT_SYMBOL = "XAUUSD"


class XauusdPriceParquetLogger:
    """Record bid/ask once per second to a Parquet file (UTC timestamps).

    Rows are buffered and flushed frequently so each tick second is persisted
    with an accurate ``utc_timestamp``. The active file rotates daily:
    ``xauusd_ticks_YYYY-MM-DD.parquet``.
    """

    def __init__(
        self,
        output_dir: Path | str | None = None,
        symbol: str = DEFAULT_SYMBOL,
        flush_every_rows: int = 30,
    ):
        self.symbol = symbol.upper()
        self.output_dir = (
            Path(output_dir)
            if output_dir is not None
            else resolve_env_path("PRICE_LOG_DIR", f"{_runtime_relative()}/data/price_logs")
        )
        self.flush_every_rows = max(1, flush_every_rows)
        self._buffer: list[dict[str, Any]] = []
        self._active_date: str | None = None
        self._active_path: Path | None = None

    def _path_for_date(self, utc_date: str) -> Path:
        return self.output_dir / f"xauusd_ticks_{utc_date}.parquet"

    def _ensure_path(self, utc_now: datetime) -> Path:
        date_key = utc_now.strftime("%Y-%m-%d")
        if self._active_date != date_key:
            self.flush()
            self._active_date = date_key
            self._active_path = self._path_for_date(date_key)
        return self._active_path  # type: ignore[return-value]

    def record_tick(
        self,
        bid: float,
        ask: float,
        utc_now: datetime | None = None,
        source: str = "mt5",
    ) -> None:
        """Queue one price row. ``utc_now`` must be timezone-aware UTC."""
        now = utc_now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        spread = ask - bid if bid > 0 and ask > 0 else 0.0
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0

        self._ensure_path(now)
        self._buffer.append({
            "utc_timestamp": now,
            "symbol": self.symbol,
            "bid": float(bid),
            "ask": float(ask),
            "mid": float(mid),
            "spread": float(spread),
            "source": source,
        })

        if len(self._buffer) >= self.flush_every_rows:
            self.flush()

    def flush(self) -> None:
        """Append buffered rows to the active daily Parquet file."""
        if not self._buffer:
            return

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            logger.error(
                "pyarrow is required for price logging. Install with: pip install pyarrow"
            )
            raise exc

        if self._active_path is None:
            self._active_path = self._path_for_date(
                self._buffer[0]["utc_timestamp"].strftime("%Y-%m-%d")
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        new_table = pa.Table.from_pylist(self._buffer)

        path = self._active_path
        if path.exists():
            existing = pq.read_table(path)
            combined = pa.concat_tables([existing, new_table])
        else:
            combined = new_table

        pq.write_table(combined, path, compression="snappy")
        logger.debug(
            "Flushed %d price row(s) to %s (total rows ~%d)",
            len(self._buffer), path, combined.num_rows,
        )
        self._buffer.clear()

    async def run_loop(self, bridge, interval_sec: float = 1.0, running_flag=None) -> None:
        """Poll MT5 every ``interval_sec`` and append ticks for ``self.symbol``."""
        import asyncio

        logger.info(
            "XAUUSD price Parquet logger started (dir=%s, interval=%.1fs)",
            self.output_dir, interval_sec,
        )
        try:
            while True:
                if running_flag is not None and not running_flag():
                    break
                try:
                    tick = await bridge.get_tick(self.symbol)
                    if tick is not None:
                        bid = float(getattr(tick, "bid", 0.0) or 0.0)
                        ask = float(getattr(tick, "ask", 0.0) or 0.0)
                        if bid > 0 and ask > 0:
                            self.record_tick(bid=bid, ask=ask)
                        else:
                            logger.warning("Price logger: invalid tick for %s", self.symbol)
                    else:
                        logger.warning("Price logger: no tick for %s", self.symbol)
                except Exception as exc:
                    logger.error("Price logger error: %s", exc)
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            pass
        finally:
            self.flush()
            logger.info("XAUUSD price Parquet logger stopped.")

    @staticmethod
    def read_log(path: Path | str) -> Optional[Any]:
        """Load a Parquet log file (returns pyarrow Table). For notebooks / validation."""
        import pyarrow.parquet as pq
        path = Path(path)
        if not path.exists():
            return None
        return pq.read_table(path)
