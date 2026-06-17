"""Tests for XAUUSD Parquet price logger."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

pyarrow = pytest.importorskip("pyarrow")

from src.market.price_logger import XauusdPriceParquetLogger


def test_parquet_logger_writes_and_appends(tmp_path: Path):
    logger = XauusdPriceParquetLogger(
        output_dir=tmp_path,
        flush_every_rows=1,
    )
    t0 = datetime(2026, 6, 17, 19, 6, 12, tzinfo=timezone.utc)
    logger.record_tick(bid=4277.0, ask=4277.2, utc_now=t0)
    logger.record_tick(bid=4278.0, ask=4278.1, utc_now=t0.replace(second=13))

    path = tmp_path / "xauusd_ticks_2026-06-17.parquet"
    assert path.exists()

    table = XauusdPriceParquetLogger.read_log(path)
    assert table is not None
    assert table.num_rows == 2
    assert table.column_names == [
        "utc_timestamp", "symbol", "bid", "ask", "mid", "spread", "source",
    ]
    assert table.column("bid").to_pylist() == pytest.approx([4277.0, 4278.0])
    assert table.column("ask").to_pylist() == pytest.approx([4277.2, 4278.1])
    assert table.column("symbol").to_pylist() == ["XAUUSD", "XAUUSD"]


def test_parquet_logger_daily_rotation(tmp_path: Path):
    logger = XauusdPriceParquetLogger(output_dir=tmp_path, flush_every_rows=1)
    logger.record_tick(
        4300.0, 4300.2,
        utc_now=datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone.utc),
    )
    logger.record_tick(
        4301.0, 4301.2,
        utc_now=datetime(2026, 6, 18, 0, 0, 1, tzinfo=timezone.utc),
    )
    assert (tmp_path / "xauusd_ticks_2026-06-17.parquet").exists()
    assert (tmp_path / "xauusd_ticks_2026-06-18.parquet").exists()
