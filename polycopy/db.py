"""SQLite persistence.

Everything the web terminal shows comes from here: traders, leader trades,
signals and their decisions, orders, positions (with per-leader lots), realized
PnL events and equity snapshots. Paper and live data live side by side and are
separated by the ``mode`` column.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS traders (
    wallet TEXT PRIMARY KEY,
    name TEXT,
    profile_image TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    manual INTEGER NOT NULL DEFAULT 0,
    score REAL,
    metrics TEXT,
    curve TEXT,
    leaderboard TEXT,
    note TEXT,
    last_scored_at REAL,
    followed_at REAL,
    benched_until REAL,
    first_seen_at REAL
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS token_map (
    asset_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leader_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    condition_id TEXT,
    side TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'TRADE',
    price REAL,
    size REAL,
    usdc REAL,
    ts REAL,
    tx_hash TEXT,
    title TEXT,
    outcome TEXT,
    slug TEXT,
    source TEXT,
    detected_at REAL,
    fill_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_leader_trades_wallet ON leader_trades(wallet, ts);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    wallet TEXT NOT NULL,
    leader_name TEXT,
    asset_id TEXT NOT NULL,
    condition_id TEXT,
    side TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'TRADE',
    leader_price REAL,
    leader_size REAL,
    leader_usdc REAL,
    leader_ts REAL,
    detected_at REAL,
    latency_sec REAL,
    title TEXT,
    outcome TEXT,
    slug TEXT,
    decision TEXT,
    reason TEXT,
    stake REAL,
    order_id INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_mode ON signals(mode, detected_at);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    signal_id INTEGER,
    position_id INTEGER,
    wallet TEXT,
    asset_id TEXT NOT NULL,
    condition_id TEXT,
    side TEXT NOT NULL,
    purpose TEXT NOT NULL,
    requested_usdc REAL,
    requested_shares REAL,
    limit_price REAL,
    status TEXT NOT NULL,
    shares REAL DEFAULT 0,
    usdc REAL DEFAULT 0,
    fee REAL DEFAULT 0,
    avg_price REAL,
    leader_price REAL,
    slippage REAL,
    ext_order_id TEXT,
    error TEXT,
    title TEXT,
    outcome TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_mode ON orders(mode, created_at);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    condition_id TEXT,
    title TEXT,
    outcome TEXT,
    slug TEXT,
    event_slug TEXT,
    icon TEXT,
    category TEXT,
    shares REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    invested REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    result TEXT,
    opened_at REAL,
    closed_at REAL,
    end_ts REAL,
    last_price REAL,
    last_price_ts REAL,
    payout REAL,
    redeemed INTEGER NOT NULL DEFAULT 0,
    redeem_error TEXT,
    exit_pending INTEGER NOT NULL DEFAULT 0,
    leaders TEXT
);
CREATE INDEX IF NOT EXISTS ix_positions_mode ON positions(mode, status);

CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    invested REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    opened_at REAL,
    closed_at REAL,
    UNIQUE(position_id, wallet)
);

CREATE TABLE IF NOT EXISTS pnl_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts REAL NOT NULL,
    position_id INTEGER,
    wallet TEXT,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    category TEXT
);
CREATE INDEX IF NOT EXISTS ix_pnl_mode ON pnl_events(mode, ts);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts REAL NOT NULL,
    cash REAL,
    positions_value REAL,
    equity REAL,
    exposure REAL,
    realized_total REAL,
    unrealized_total REAL
);
CREATE INDEX IF NOT EXISTS ix_equity_mode ON equity(mode, ts);

CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    starting_balance REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    category TEXT,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS ix_logs_ts ON logs(ts);
"""

JSON_COLUMNS = {"metrics", "curve", "leaderboard", "data", "leaders"}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        value = out[key]
        if isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except ValueError:
                pass
    return out


class Database:
    """Thin synchronous wrapper around one SQLite connection.

    All calls run on the asyncio thread; individual statements take well under a
    millisecond, so there is no need for a thread pool.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return _row_to_dict(row)

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = list(values)
        encoded = [_encode(values[c]) for c in cols]
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self._lock:
            cur = self._conn.execute(sql, encoded)
            return int(cur.lastrowid or 0)

    def update(self, table: str, row_id: int, values: dict[str, Any], key: str = "id") -> None:
        if not values:
            return
        cols = list(values)
        sql = f"UPDATE {table} SET {','.join(f'{c}=?' for c in cols)} WHERE {key}=?"
        with self._lock:
            self._conn.execute(sql, [_encode(values[c]) for c in cols] + [row_id])

    def transaction(self) -> "_Transaction":
        return _Transaction(self)

    # ----------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        value = self.scalar("SELECT value FROM kv WHERE key=?", (key,))
        if value is None:
            return default
        try:
            return json.loads(value)
        except ValueError:
            return value

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ------------------------------------------------------------------- logs
    def log(self, level: str, category: str, message: str, data: Any = None) -> dict[str, Any]:
        entry = {
            "ts": time.time(),
            "level": level,
            "category": category,
            "message": message,
            "data": data,
        }
        entry["id"] = self.insert("logs", entry)
        return entry

    def prune_logs(self, keep: int = 20000) -> None:
        self.execute(
            "DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id), 0) - ? FROM logs)", (keep,)
        )

    # ------------------------------------------------------------------ traders
    def upsert_trader(self, wallet: str, values: dict[str, Any]) -> None:
        wallet = wallet.lower()
        existing = self.one("SELECT wallet FROM traders WHERE wallet=?", (wallet,))
        if existing is None:
            self.insert("traders", {"wallet": wallet, "first_seen_at": time.time(), **values})
        else:
            cols = list(values)
            if not cols:
                return
            sql = f"UPDATE traders SET {','.join(f'{c}=?' for c in cols)} WHERE wallet=?"
            self.execute(sql, [_encode(values[c]) for c in cols] + [wallet])

    def get_trader(self, wallet: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM traders WHERE wallet=?", (wallet.lower(),))

    def traders(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.query(
                "SELECT * FROM traders WHERE status=? ORDER BY score DESC NULLS LAST", (status,)
            )
        return self.query("SELECT * FROM traders ORDER BY score DESC NULLS LAST")

    # ------------------------------------------------------------------ markets
    def cache_market(self, condition_id: str, data: dict[str, Any], closed: bool) -> None:
        self.execute(
            "INSERT INTO markets(condition_id, data, closed, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(condition_id) DO UPDATE SET data=excluded.data, "
            "closed=excluded.closed, fetched_at=excluded.fetched_at",
            (condition_id, json.dumps(data), int(closed), time.time()),
        )
        for key in ("yes_token", "no_token", "yes_alt", "no_alt"):
            token = data.get(key)
            if token:
                self.execute(
                    "INSERT OR REPLACE INTO token_map(asset_id, condition_id) VALUES(?, ?)",
                    (token, condition_id),
                )

    def cached_market(self, condition_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM markets WHERE condition_id=?", (condition_id,))

    def condition_for_token(self, asset_id: str) -> str | None:
        return self.scalar("SELECT condition_id FROM token_map WHERE asset_id=?", (asset_id,))


class _Transaction:
    def __init__(self, db: Database) -> None:
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db._conn.execute("COMMIT")
            else:
                self.db._conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value
