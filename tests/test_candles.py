"""Tests for 1m candle aggregation from parquet ticks."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

pyarrow = pytest.importorskip("pyarrow")

from src.market.candles import load_xauusd_1m_candles, ticks_to_1m_candles
from src.market.price_logger import XauusdPriceParquetLogger


def test_ticks_to_1m_candles_ohlc():
    base = datetime(2026, 6, 18, 10, 5, 0, tzinfo=timezone.utc)
    rows = [
        {"utc_timestamp": base.replace(second=10), "mid": 4240.0, "bid": 4239.9, "ask": 4240.1},
        {"utc_timestamp": base.replace(second=30), "mid": 4242.0, "bid": 4241.9, "ask": 4242.1},
        {"utc_timestamp": base.replace(second=50), "mid": 4241.0, "bid": 4240.9, "ask": 4241.1},
        {"utc_timestamp": base.replace(minute=6, second=5), "mid": 4243.0, "bid": 4242.9, "ask": 4243.1},
    ]
    candles = ticks_to_1m_candles(rows)
    assert len(candles) == 2
    assert candles[0]["o"] == 4240.0
    assert candles[0]["h"] == 4242.0
    assert candles[0]["l"] == 4240.0
    assert candles[0]["c"] == 4241.0
    assert candles[1]["c"] == 4243.0


def test_load_xauusd_1m_candles_from_parquet(tmp_path: Path):
    logger = XauusdPriceParquetLogger(output_dir=tmp_path, flush_every_rows=1)
    t0 = datetime.now(timezone.utc).replace(second=10, microsecond=0)
    logger.record_tick(4244.0, 4244.2, utc_now=t0)
    logger.record_tick(4245.0, 4245.2, utc_now=t0.replace(second=40))

    result = load_xauusd_1m_candles(lookback_minutes=60, parquet_dir=tmp_path)
    assert result["timeframe"] == "1m"
    assert len(result["candles"]) >= 1
    assert result["last_mid"] == pytest.approx(4245.1)
