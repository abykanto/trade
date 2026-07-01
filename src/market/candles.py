"""Aggregate XAUUSD parquet ticks into 1-minute OHLC candles for the signal tool."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.paths import resolve_env_path, _runtime_relative
from src.market.price_logger import XauusdPriceParquetLogger


def _minute_bucket(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    return ts.replace(second=0, microsecond=0)


def _iter_tick_rows(parquet_dir: Path, since: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not parquet_dir.is_dir():
        return rows

    dates = {since.strftime("%Y-%m-%d"), datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    for date_key in sorted(dates):
        path = parquet_dir / f"xauusd_ticks_{date_key}.parquet"
        table = XauusdPriceParquetLogger.read_log(path)
        if table is None:
            continue
        data = table.to_pydict()
        timestamps = data.get("utc_timestamp") or []
        mids = data.get("mid") or []
        bids = data.get("bid") or []
        asks = data.get("ask") or []
        for i, ts in enumerate(timestamps):
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            if ts < since:
                continue
            rows.append({
                "utc_timestamp": ts,
                "mid": float(mids[i]) if i < len(mids) else 0.0,
                "bid": float(bids[i]) if i < len(bids) else 0.0,
                "ask": float(asks[i]) if i < len(asks) else 0.0,
            })
    rows.sort(key=lambda r: r["utc_timestamp"])
    return rows


def ticks_to_1m_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build 1-minute OHLC candles from tick rows (mid price)."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for row in rows:
        mid = row.get("mid") or 0.0
        if mid <= 0:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        if mid <= 0:
            continue
        buckets[_minute_bucket(row["utc_timestamp"])].append(mid)

    candles: list[dict[str, Any]] = []
    for minute in sorted(buckets.keys()):
        prices = buckets[minute]
        if not prices:
            continue
        candles.append({
            "t": minute.isoformat().replace("+00:00", "Z"),
            "o": round(prices[0], 2),
            "h": round(max(prices), 2),
            "l": round(min(prices), 2),
            "c": round(prices[-1], 2),
        })
    return candles


def load_xauusd_1m_candles(
    lookback_minutes: int = 240,
    parquet_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Load recent 1m candles from parquet tick logs."""
    lookback_minutes = max(5, min(int(lookback_minutes), 24 * 60))
    directory = (
        Path(parquet_dir)
        if parquet_dir is not None
        else resolve_env_path("PRICE_LOG_DIR", f"{_runtime_relative()}/data/price_logs")
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    rows = _iter_tick_rows(directory, since)
    candles = ticks_to_1m_candles(rows)

    last_bid = last_ask = last_mid = None
    if rows:
        last = rows[-1]
        last_bid = round(float(last["bid"]), 2)
        last_ask = round(float(last["ask"]), 2)
        last_mid = round(float(last["mid"]), 2) if last["mid"] else round((last_bid + last_ask) / 2, 2)

    return {
        "symbol": "XAUUSD",
        "timeframe": "1m",
        "lookback_minutes": lookback_minutes,
        "candles": candles,
        "last_bid": last_bid,
        "last_ask": last_ask,
        "last_mid": last_mid,
        "tick_count": len(rows),
    }
