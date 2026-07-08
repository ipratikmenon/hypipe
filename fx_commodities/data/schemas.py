"""PyArrow schemas for the Parquet data lake (REQUIREMENTS.md §4.5).

Partition layout: data_root/{table}/symbol={key}/date={YYYY-MM-DD}/part-*.parquet
Dates are UTC.
"""

from __future__ import annotations

import pyarrow as pa

TICKS = pa.schema([
    ("ts_ms", pa.int64()),
    ("symbol_key", pa.string()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("last", pa.float64()),        # NaN when quotes-only feed
    ("volume", pa.float64()),      # NaN when no trade volume
    ("side", pa.int8()),
])

DOM = pa.schema([
    ("ts_ms", pa.int64()),
    ("symbol_key", pa.string()),
    ("side", pa.int8()),           # +1 bid / -1 ask
    ("level", pa.int8()),
    ("price", pa.float64()),
    ("volume", pa.float64()),
])

OHLCV = pa.schema([
    ("ts", pa.int64()),            # bar open, epoch seconds
    ("symbol_key", pa.string()),
    ("tf_min", pa.int16()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("tick_volume", pa.int64()),
    ("spread_points", pa.int32()),
    ("real_volume", pa.int64()),
])

TABLES = {"ticks": TICKS, "dom": DOM, "ohlcv": OHLCV}
