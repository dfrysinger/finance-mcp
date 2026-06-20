"""SQLite-backed durable archive for multi-year transaction history.

Why SQLite and not the JSON cache: the archive must accumulate years of data,
stay fast to search, and never lose a transaction once seen — even after it ages
out of SimpleFIN's rolling window. Transactions are upserted by their stable
SimpleFIN id (so a pending row can later be promoted to posted without
duplicating), ``first_seen`` is preserved across upserts, and per-sync balance
snapshots are recorded so net worth can be tracked over time.

The stored rows use the same field names as :mod:`finance_mcp.normalize`, so
``load_transactions``/``load_accounts`` return data shaped exactly like the JSON
cache and the existing query helpers work unchanged on top of the archive.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    account_id    TEXT,
    account_name  TEXT,
    org           TEXT,
    posted_ts     INTEGER,
    posted        TEXT,
    transacted_at TEXT,
    amount        TEXT,
    amount_float  REAL,
    description   TEXT,
    payee         TEXT,
    memo          TEXT,
    pending       INTEGER,
    currency      TEXT,
    first_seen    TEXT,
    last_updated  TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_posted   ON transactions(posted_ts);
CREATE INDEX IF NOT EXISTS idx_txn_account  ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_amount   ON transactions(amount_float);

CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    account_name      TEXT,
    org               TEXT,
    org_domain        TEXT,
    currency          TEXT,
    balance           TEXT,
    balance_float     REAL,
    available_balance TEXT,
    balance_date      TEXT,
    balance_date_ts   INTEGER,
    last_updated      TEXT
);

-- One row per (account, as-of balance date): the history behind net worth.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id      TEXT,
    account_name    TEXT,
    org             TEXT,
    balance         TEXT,
    balance_float   REAL,
    balance_date    TEXT,
    balance_date_ts INTEGER,
    recorded_at     TEXT,
    PRIMARY KEY (account_id, balance_date_ts)
);
CREATE INDEX IF NOT EXISTS idx_snap_date ON balance_snapshots(balance_date_ts);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_TXN_COLUMNS = [
    "id", "account_id", "account_name", "org", "posted_ts", "posted",
    "transacted_at", "amount", "amount_float", "description", "payee",
    "memo", "pending", "currency",
]

_ACCOUNT_COLUMNS = [
    "account_id", "account_name", "org", "org_domain", "currency", "balance",
    "balance_float", "available_balance", "balance_date", "balance_date_ts",
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the archive database with the schema applied."""
    path = path or (config.home_dir() / "archive.db")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def upsert(conn: sqlite3.Connection, normalized: dict) -> dict:
    """Upsert normalized accounts + transactions and record balance snapshots.

    Returns counts of rows inserted/updated. ``first_seen`` is set once on a
    transaction's first insert and never overwritten; ``last_updated`` always
    advances so re-syncs are visible.
    """
    now = _now()
    txn_before = _count(conn, "transactions")
    snap_before = _count(conn, "balance_snapshots")

    txn_rows = [
        tuple(t.get(c) for c in _TXN_COLUMNS) + (now, now)
        for t in normalized.get("transactions", [])
        if t.get("id") is not None
    ]
    placeholders = ", ".join(["?"] * (len(_TXN_COLUMNS) + 2))
    conn.executemany(
        f"INSERT INTO transactions ({', '.join(_TXN_COLUMNS)}, first_seen, last_updated) "
        f"VALUES ({placeholders}) "
        "ON CONFLICT(id) DO UPDATE SET "
        "account_id=excluded.account_id, account_name=excluded.account_name, "
        "org=excluded.org, posted_ts=excluded.posted_ts, posted=excluded.posted, "
        "transacted_at=excluded.transacted_at, amount=excluded.amount, "
        "amount_float=excluded.amount_float, description=excluded.description, "
        "payee=excluded.payee, memo=excluded.memo, pending=excluded.pending, "
        "currency=excluded.currency, last_updated=excluded.last_updated",
        txn_rows,
    )

    acct_rows = [
        tuple(a.get(c) for c in _ACCOUNT_COLUMNS) + (now,)
        for a in normalized.get("accounts", [])
        if a.get("account_id") is not None
    ]
    aplace = ", ".join(["?"] * (len(_ACCOUNT_COLUMNS) + 1))
    conn.executemany(
        f"INSERT INTO accounts ({', '.join(_ACCOUNT_COLUMNS)}, last_updated) "
        f"VALUES ({aplace}) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        "account_name=excluded.account_name, org=excluded.org, "
        "org_domain=excluded.org_domain, currency=excluded.currency, "
        "balance=excluded.balance, balance_float=excluded.balance_float, "
        "available_balance=excluded.available_balance, "
        "balance_date=excluded.balance_date, balance_date_ts=excluded.balance_date_ts, "
        "last_updated=excluded.last_updated",
        acct_rows,
    )

    snap_rows = [
        (
            a.get("account_id"), a.get("account_name"), a.get("org"),
            a.get("balance"), a.get("balance_float"), a.get("balance_date"),
            a.get("balance_date_ts"), now,
        )
        for a in normalized.get("accounts", [])
        if a.get("account_id") is not None and a.get("balance_date_ts") is not None
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO balance_snapshots "
        "(account_id, account_name, org, balance, balance_float, balance_date, "
        "balance_date_ts, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        snap_rows,
    )

    conn.commit()
    return {
        "transactions_seen": len(txn_rows),
        "transactions_added": _count(conn, "transactions") - txn_before,
        "accounts_seen": len(acct_rows),
        "balance_snapshots_added": _count(conn, "balance_snapshots") - snap_before,
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def load_transactions(conn: sqlite3.Connection) -> list[dict]:
    """Return all archived transactions (newest first) as cache-shaped dicts."""
    rows = conn.execute(
        "SELECT * FROM transactions ORDER BY COALESCE(posted_ts, 0) DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pending"] = bool(d.get("pending"))
        out.append(d)
    return out


def load_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Return all archived accounts (latest known balance) as cache-shaped dicts."""
    rows = conn.execute("SELECT * FROM accounts ORDER BY org, account_name").fetchall()
    return [dict(r) for r in rows]


def net_worth_history(conn: sqlite3.Connection) -> list[dict]:
    """Total balance across all accounts per as-of date (for net-worth trends).

    Uses the latest snapshot per account on each date, summed. Returns rows
    ``{date, total, account_count}`` ordered oldest-first.
    """
    rows = conn.execute(
        "SELECT substr(balance_date, 1, 10) AS date, "
        "ROUND(SUM(balance_float), 2) AS total, COUNT(DISTINCT account_id) AS account_count "
        "FROM balance_snapshots WHERE balance_float IS NOT NULL "
        "GROUP BY substr(balance_date, 1, 10) ORDER BY date"
    ).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    """Summarize archive size and coverage."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(posted) AS earliest, MAX(posted) AS latest "
        "FROM transactions"
    ).fetchone()
    return {
        "transactions": row["n"],
        "accounts": _count(conn, "accounts"),
        "balance_snapshots": _count(conn, "balance_snapshots"),
        "earliest_transaction": row["earliest"],
        "latest_transaction": row["latest"],
        "db_path": str((config.home_dir() / "archive.db")),
    }


def is_empty(conn: sqlite3.Connection) -> bool:
    return _count(conn, "transactions") == 0
