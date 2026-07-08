"""SQLite trade journal — the single source of truth for orders, positions,
risk state, and operational events (REQUIREMENTS.md §11.1).

Design notes:
- One file, WAL mode, a process-wide lock per Journal instance. Callers from
  multiple threads share one instance.
- `correlation_id` is the idempotency key: inserting a duplicate raises,
  which the executor treats as "this order was already attempted".
- Halt state is keyed on UTC session date and must survive restarts — that is
  the whole point of persisting it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS orders (
  correlation_id TEXT PRIMARY KEY,
  ticket         INTEGER,
  deal_id        INTEGER,
  ts_created     TEXT NOT NULL,
  module         TEXT NOT NULL,
  symbol_key     TEXT NOT NULL,
  broker_symbol  TEXT NOT NULL,
  side           TEXT NOT NULL,
  lots           REAL NOT NULL,
  order_type     TEXT NOT NULL,
  limit_price    REAL,
  sl             REAL,
  tp             REAL,
  status         TEXT NOT NULL,
  fill_price     REAL,
  commission     REAL,
  ts_terminal    TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket        INTEGER,
  magic         INTEGER,
  symbol_key    TEXT NOT NULL,
  broker_symbol TEXT NOT NULL,
  direction     TEXT NOT NULL,
  lots          REAL NOT NULL,
  entry_price   REAL,
  stop_loss     REAL,
  target        REAL,
  ts_open       TEXT NOT NULL,
  ts_close      TEXT,
  exit_price    REAL,
  swap          REAL DEFAULT 0,
  commission    REAL DEFAULT 0,
  realized_pnl  REAL,
  exit_reason   TEXT,
  signal_json   TEXT
);
CREATE TABLE IF NOT EXISTS risk_state (
  session_date TEXT PRIMARY KEY,
  daily_pnl    REAL DEFAULT 0,
  trade_count  INTEGER DEFAULT 0,
  halted       INTEGER DEFAULT 0,
  halt_reason  TEXT,
  updated_ts   TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  level        TEXT NOT NULL,
  kind         TEXT NOT NULL,
  payload_json TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DuplicateCorrelationId(RuntimeError):
    pass


class Journal:
    def __init__(self, db_path: str | Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Orders ────────────────────────────────────────────────────────────

    def record_order(self, correlation_id: str, *, module: str, symbol_key: str,
                     broker_symbol: str, side: str, lots: float, order_type: str,
                     limit_price: float | None = None, sl: float | None = None,
                     tp: float | None = None) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO orders (correlation_id, ts_created, module, symbol_key,"
                    " broker_symbol, side, lots, order_type, limit_price, sl, tp, status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,'LOCAL_NEW')",
                    (correlation_id, _utcnow(), module, symbol_key, broker_symbol,
                     side, lots, order_type, limit_price, sl, tp),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise DuplicateCorrelationId(correlation_id) from exc

    def update_order(self, correlation_id: str, *, status: str,
                     ticket: int | None = None, deal_id: int | None = None,
                     fill_price: float | None = None,
                     commission: float | None = None,
                     terminal: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status=?,"
                " ticket=COALESCE(?, ticket), deal_id=COALESCE(?, deal_id),"
                " fill_price=COALESCE(?, fill_price),"
                " commission=COALESCE(?, commission),"
                " ts_terminal=CASE WHEN ? THEN ? ELSE ts_terminal END"
                " WHERE correlation_id=?",
                (status, ticket, deal_id, fill_price, commission,
                 int(terminal), _utcnow(), correlation_id),
            )
            self._conn.commit()

    def get_order(self, correlation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE correlation_id=?", (correlation_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── Positions ─────────────────────────────────────────────────────────

    def open_position(self, *, ticket: int, magic: int, symbol_key: str,
                      broker_symbol: str, direction: str, lots: float,
                      entry_price: float, stop_loss: float, target: float,
                      signal: dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO positions (ticket, magic, symbol_key, broker_symbol,"
                " direction, lots, entry_price, stop_loss, target, ts_open, signal_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ticket, magic, symbol_key, broker_symbol, direction, lots,
                 entry_price, stop_loss, target, _utcnow(),
                 json.dumps(signal) if signal else None),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_position(self, position_id: int, *, exit_price: float,
                       realized_pnl: float, exit_reason: str,
                       swap: float = 0.0, commission: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET ts_close=?, exit_price=?, realized_pnl=?,"
                " exit_reason=?, swap=?, commission=? WHERE id=?",
                (_utcnow(), exit_price, realized_pnl, exit_reason,
                 swap, commission, position_id),
            )
            self._conn.commit()

    def get_open_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE ts_close IS NULL"
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Risk state ────────────────────────────────────────────────────────

    def add_daily_pnl(self, session_date: str, pnl: float) -> float:
        """Accumulate realized PnL for the UTC session date; returns new total."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO risk_state (session_date, daily_pnl, trade_count, updated_ts)"
                " VALUES (?,?,1,?)"
                " ON CONFLICT(session_date) DO UPDATE SET"
                " daily_pnl = daily_pnl + excluded.daily_pnl,"
                " trade_count = trade_count + 1, updated_ts = excluded.updated_ts",
                (session_date, pnl, _utcnow()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT daily_pnl FROM risk_state WHERE session_date=?",
                (session_date,),
            ).fetchone()
            return float(row["daily_pnl"])

    def get_risk_state(self, session_date: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM risk_state WHERE session_date=?", (session_date,)
            ).fetchone()
            if row is None:
                return {"session_date": session_date, "daily_pnl": 0.0,
                        "trade_count": 0, "halted": 0, "halt_reason": None}
            return dict(row)

    def set_halt(self, session_date: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO risk_state (session_date, halted, halt_reason, updated_ts)"
                " VALUES (?,1,?,?)"
                " ON CONFLICT(session_date) DO UPDATE SET"
                " halted=1, halt_reason=excluded.halt_reason, updated_ts=excluded.updated_ts",
                (session_date, reason, _utcnow()),
            )
            self._conn.commit()

    def is_halted(self, session_date: str) -> bool:
        return bool(self.get_risk_state(session_date).get("halted"))

    # ── Events ────────────────────────────────────────────────────────────

    def log_event(self, level: str, kind: str, **payload: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, level, kind, payload_json) VALUES (?,?,?,?)",
                (_utcnow(), level, kind, json.dumps(payload, default=str)),
            )
            self._conn.commit()
