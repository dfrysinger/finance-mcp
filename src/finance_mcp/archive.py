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
import time
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

-- User-editable rules: a case-insensitive substring match -> category.
CREATE TABLE IF NOT EXISTS category_rules (
    rule_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,
    field       TEXT NOT NULL DEFAULT 'any',   -- description | payee | any
    category    TEXT NOT NULL,
    is_transfer INTEGER NOT NULL DEFAULT 0,     -- internal transfer, not real spend
    priority    INTEGER NOT NULL DEFAULT 100,   -- lower wins
    created_at  TEXT
);

-- Per-transaction manual overrides; kept separate from `transactions` so a sync
-- (which upserts transactions) can never clobber a hand-assigned category.
CREATE TABLE IF NOT EXISTS transaction_categories (
    txn_id      TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    is_transfer INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual',
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Reconstructed internal-transfer pairings. Schwab's feed names only the
-- product *type* a transfer went to ("...to Investor Checking"), never which of
-- the user's ~19 named envelope accounts received it. A row here links one
-- outgoing leg (`debit_txn_id`) to the incoming leg (`credit_txn_id`) that
-- received the same money on a different account, recovering the hidden
-- counterparty by amount+date matching. Exactly one leg may be NULL while a
-- transfer is still unmatched. The explanation columns record WHY a link was
-- drawn so "Main -> Groceries?" stays auditable.
CREATE TABLE IF NOT EXISTS transfer_links (
    link_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    debit_txn_id      TEXT,
    credit_txn_id     TEXT,
    amount_cents      INTEGER,
    status            TEXT NOT NULL,   -- confirmed | inferred | unconfirmed | unmatched
    method            TEXT,            -- mutual-unique | forced-perfect | envelope-set | keyword-type | manual ...
    confidence        TEXT,            -- taxonomy label (see transfer-tracking design)
    date_rule         TEXT,            -- same-day | plus-minus-1-day
    keyword           TEXT,            -- destination product-type keyword used, if any
    type_source       TEXT,            -- confirmed | inferred | heuristic | none
    candidates_before INTEGER,         -- audit: candidate legs before narrowing
    candidates_after  INTEGER,         -- audit: candidate legs after narrowing
    explanation       TEXT,            -- human-readable "why"
    reconcile_run_id  TEXT,            -- the reconcile pass that last wrote this inferred row
    created_at        TEXT,
    updated_at        TEXT,
    -- A link must reference at least one real leg; a row pointing at nothing is
    -- meaningless. (The UNIQUE indexes below cannot catch all-NULL rows because
    -- SQLite treats each NULL as distinct.) A debit can never equal its own
    -- credit: a transaction is money out or money in, never both.
    CHECK (debit_txn_id IS NOT NULL OR credit_txn_id IS NOT NULL),
    CHECK (debit_txn_id IS NULL OR credit_txn_id IS NULL
           OR debit_txn_id <> credit_txn_id)
);
-- A transaction may belong to at most one link, in exactly one role. The UNIQUE
-- indexes enforce same-role uniqueness (a txn used twice as a debit, or twice as
-- a credit). NULLs are exempt (SQLite treats each NULL as distinct), so any
-- number of still-unmatched legs can coexist.
CREATE UNIQUE INDEX IF NOT EXISTS idx_link_debit  ON transfer_links(debit_txn_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_link_credit ON transfer_links(credit_txn_id);
-- The UNIQUE indexes only constrain each column independently, so a transaction
-- could still be claimed as a debit in one link and a credit in another. These
-- triggers close that cross-role gap atomically (an app-level check-then-insert
-- would race), so a transaction id appears at most once across BOTH columns.
CREATE TRIGGER IF NOT EXISTS trg_link_no_cross_claim_ins
BEFORE INSERT ON transfer_links
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'transfer leg already linked in the opposite role')
    WHERE EXISTS (
        SELECT 1 FROM transfer_links
        WHERE (NEW.debit_txn_id  IS NOT NULL AND credit_txn_id = NEW.debit_txn_id)
           OR (NEW.credit_txn_id IS NOT NULL AND debit_txn_id  = NEW.credit_txn_id)
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_link_no_cross_claim_upd
BEFORE UPDATE ON transfer_links
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'transfer leg already linked in the opposite role')
    WHERE EXISTS (
        SELECT 1 FROM transfer_links
        WHERE link_id <> NEW.link_id
          AND ((NEW.debit_txn_id  IS NOT NULL AND credit_txn_id = NEW.debit_txn_id)
            OR (NEW.credit_txn_id IS NOT NULL AND debit_txn_id  = NEW.credit_txn_id))
    );
END;

-- Static, user-authoritative map of an account to its Schwab product type
-- (e.g. "Investor Checking" vs "Investor Savings"). The product type is
-- permanent per account number, so it is confirmed once and used only as a
-- tie-breaker / guard during matching -- never re-learned from the matches it
-- helps produce (which would be circular).
CREATE TABLE IF NOT EXISTS account_types (
    account_id   TEXT NOT NULL PRIMARY KEY,
    product_type TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'inferred',  -- confirmed | inferred | heuristic
    updated_at   TEXT
);
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


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to WAL mode, retrying on a transient lock.

    Upgrading a brand-new database to WAL needs an exclusive lock, and SQLite
    deliberately does NOT invoke the busy handler for that lock upgrade (it
    returns SQLITE_BUSY immediately to avoid deadlock), so ``busy_timeout`` does
    not cover it. When two processes first-create the archive at once (e.g. the
    CLI and the MCP server), one would otherwise crash with "database is locked".
    Retry briefly; once the other connection has set WAL the pragma is a no-op.
    """
    deadline = time.monotonic() + 5.0
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the archive database with the schema applied."""
    path = path or (config.home_dir() / "archive.db")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    _enable_wal(conn)
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


# --- Transfer reconciliation: storage foundation -------------------------------
#
# These are low-level accessors over `transfer_links` and `account_types`. They
# enforce only the schema's at-most-one-link-per-leg UNIQUE constraints. The
# matching logic (which legs to pair), the static type-map seeding/guard, and the
# idempotent reconcile policy (confirmed preserved, inferred recomputed,
# promote/downgrade) are layered on top in later pieces.

_LINK_COLUMNS = [
    "debit_txn_id", "credit_txn_id", "amount_cents", "status", "method",
    "confidence", "date_rule", "keyword", "type_source", "candidates_before",
    "candidates_after", "explanation", "reconcile_run_id",
]


def insert_transfer_link(
    conn: sqlite3.Connection,
    *,
    status: str,
    debit_txn_id: str | None = None,
    credit_txn_id: str | None = None,
    amount_cents: int | None = None,
    method: str | None = None,
    confidence: str | None = None,
    date_rule: str | None = None,
    keyword: str | None = None,
    type_source: str | None = None,
    candidates_before: int | None = None,
    candidates_after: int | None = None,
    explanation: str | None = None,
    reconcile_run_id: str | None = None,
) -> int:
    """Insert one transfer-link row and return its ``link_id``.

    Raises ``sqlite3.IntegrityError`` if either leg is already claimed by another
    link (the UNIQUE indexes on ``debit_txn_id`` / ``credit_txn_id``). A leg left
    ``None`` represents a still-unmatched transfer.
    """
    now = _now()
    values = {
        "debit_txn_id": debit_txn_id, "credit_txn_id": credit_txn_id,
        "amount_cents": amount_cents, "status": status, "method": method,
        "confidence": confidence, "date_rule": date_rule, "keyword": keyword,
        "type_source": type_source, "candidates_before": candidates_before,
        "candidates_after": candidates_after, "explanation": explanation,
        "reconcile_run_id": reconcile_run_id,
    }
    placeholders = ", ".join(["?"] * (len(_LINK_COLUMNS) + 2))
    cur = conn.execute(
        f"INSERT INTO transfer_links ({', '.join(_LINK_COLUMNS)}, created_at, updated_at) "
        f"VALUES ({placeholders})",
        tuple(values[c] for c in _LINK_COLUMNS) + (now, now),
    )
    conn.commit()
    return cur.lastrowid


def load_transfer_links(conn: sqlite3.Connection) -> list[dict]:
    """Return all transfer links (insertion order) as plain dicts."""
    rows = conn.execute("SELECT * FROM transfer_links ORDER BY link_id").fetchall()
    return [dict(r) for r in rows]


def replace_machine_links(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Atomically replace every machine-written link with ``rows``.

    Confirmed links (``status='confirmed'``) are user-authoritative and left
    untouched; every other link (inferred / unconfirmed / unmatched) is deleted
    and re-created from ``rows`` inside a single transaction, so a failed write
    can never leave a half-reconciled set. Each row is a dict keyed by the
    ``transfer_links`` data columns (see ``_LINK_COLUMNS``); a missing key writes
    NULL. Returns the number of rows written.

    A row must not reference a leg already claimed by a confirmed link: the
    schema's cross-role trigger and per-leg UNIQUE indexes abort such a write,
    and the whole batch is rolled back rather than a row being silently dropped.
    The reconcile policy excludes confirmed legs from the matcher's input so this
    abort is a guard against a logic error, not an expected path.
    """
    now = _now()
    placeholders = ", ".join(["?"] * (len(_LINK_COLUMNS) + 2))
    insert_sql = (
        f"INSERT INTO transfer_links ({', '.join(_LINK_COLUMNS)}, created_at, updated_at) "
        f"VALUES ({placeholders})"
    )
    try:
        conn.execute("DELETE FROM transfer_links WHERE status <> 'confirmed'")
        for r in rows:
            conn.execute(insert_sql, tuple(r.get(c) for c in _LINK_COLUMNS) + (now, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def set_account_type(
    conn: sqlite3.Connection,
    account_id: str,
    product_type: str,
    *,
    source: str = "inferred",
) -> None:
    """Upsert one account's product type.

    ``source`` is one of ``confirmed`` (user-authoritative), ``inferred``
    (derived from certain matches), or ``heuristic`` (name hint). This primitive
    overwrites unconditionally; the confirmed-wins seeding policy lives in the
    type-map piece that calls it.
    """
    conn.execute(
        "INSERT INTO account_types (account_id, product_type, source, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        "product_type=excluded.product_type, source=excluded.source, "
        "updated_at=excluded.updated_at",
        (account_id, product_type, source, _now()),
    )
    conn.commit()


def delete_account_type(conn: sqlite3.Connection, account_id: str) -> None:
    """Remove one account's product-type row (no-op if absent).

    Used to clear a stale lower-trust guess once structural evidence turns
    contradictory; callers are responsible for protecting ``confirmed`` rows.
    """
    conn.execute("DELETE FROM account_types WHERE account_id = ?", (account_id,))
    conn.commit()


def load_account_types(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return the product-type map keyed by ``account_id``."""
    rows = conn.execute("SELECT * FROM account_types").fetchall()
    return {r["account_id"]: dict(r) for r in rows}
