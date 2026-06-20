import json
import sqlite3
from pathlib import Path

import pytest

from finance_mcp import archive

FIXTURE = Path(__file__).parent / "fixtures" / "sample_accounts.json"


def _normalized():
    from finance_mcp import normalize

    return normalize.normalize(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_upsert_inserts_then_counts(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    norm = _normalized()
    stats = archive.upsert(conn, norm)
    assert stats["transactions_added"] == 5
    assert stats["accounts_seen"] == 2
    assert archive.stats(conn)["transactions"] == 5


def test_upsert_is_idempotent_no_duplicates(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    norm = _normalized()
    archive.upsert(conn, norm)
    stats2 = archive.upsert(conn, norm)
    assert stats2["transactions_added"] == 0
    assert archive.stats(conn)["transactions"] == 5


def test_archive_accumulates_beyond_a_window(tmp_path):
    # Simulate two syncs whose windows do not overlap: archive must retain both.
    conn = archive.connect(tmp_path / "a.db")
    old = {
        "accounts": [{"account_id": "A", "account_name": "A", "org": "O",
                      "balance_date_ts": 1, "balance": "1", "balance_float": 1.0,
                      "balance_date": "2024-01-01T00:00:00+00:00"}],
        "transactions": [{"id": "old1", "account_id": "A", "posted_ts": 100,
                          "amount": "-1", "amount_float": -1.0, "pending": False}],
    }
    new = {
        "accounts": old["accounts"],
        "transactions": [{"id": "new1", "account_id": "A", "posted_ts": 999,
                          "amount": "-2", "amount_float": -2.0, "pending": False}],
    }
    archive.upsert(conn, old)
    archive.upsert(conn, new)  # window no longer contains old1
    ids = {t["id"] for t in archive.load_transactions(conn)}
    assert ids == {"old1", "new1"}  # old survived even though it left the window


def test_pending_promoted_without_duplicate_and_first_seen_kept(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    archive.upsert(conn, {"accounts": [], "transactions": [
        {"id": "t", "posted_ts": 1, "amount": "-5", "amount_float": -5.0, "pending": True}
    ]})
    first_seen = conn.execute("SELECT first_seen FROM transactions WHERE id='t'").fetchone()[0]
    archive.upsert(conn, {"accounts": [], "transactions": [
        {"id": "t", "posted_ts": 1, "amount": "-5", "amount_float": -5.0, "pending": False}
    ]})
    rows = archive.load_transactions(conn)
    assert len(rows) == 1
    assert rows[0]["pending"] is False
    assert rows[0]["first_seen"] == first_seen  # preserved across upsert


def test_balance_snapshots_dedupe_by_date(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    acct = {"account_id": "A", "account_name": "A", "org": "O", "balance": "10",
            "balance_float": 10.0, "balance_date": "2024-01-01T00:00:00+00:00",
            "balance_date_ts": 1704067200}
    archive.upsert(conn, {"accounts": [acct], "transactions": []})
    archive.upsert(conn, {"accounts": [acct], "transactions": []})  # same date
    assert archive.stats(conn)["balance_snapshots"] == 1
    acct2 = dict(acct, balance="20", balance_float=20.0,
                 balance_date="2024-02-01T00:00:00+00:00", balance_date_ts=1706745600)
    archive.upsert(conn, {"accounts": [acct2], "transactions": []})
    assert archive.stats(conn)["balance_snapshots"] == 2


def test_net_worth_history_sums_per_date(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    accts = [
        {"account_id": "A", "account_name": "A", "org": "O", "balance": "100",
         "balance_float": 100.0, "balance_date": "2024-01-01T00:00:00+00:00",
         "balance_date_ts": 1704067200},
        {"account_id": "B", "account_name": "B", "org": "O", "balance": "-30",
         "balance_float": -30.0, "balance_date": "2024-01-01T00:00:00+00:00",
         "balance_date_ts": 1704067200},
    ]
    archive.upsert(conn, {"accounts": accts, "transactions": []})
    hist = archive.net_worth_history(conn)
    assert hist == [{"date": "2024-01-01", "total": 70.0, "account_count": 2}]


def test_load_transactions_sorted_desc(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    archive.upsert(conn, _normalized())
    ts = [t["posted_ts"] for t in archive.load_transactions(conn)]
    assert ts == sorted(ts, reverse=True)


def test_concurrent_first_connect_does_not_lock(tmp_path):
    # Regression: two processes first-creating the archive at once must not crash
    # with "database is locked". The WAL-mode upgrade needs an exclusive lock that
    # SQLite's busy handler skips, so connect() retries it explicitly.
    import threading

    db = tmp_path / "fresh.db"
    barrier = threading.Barrier(4)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            conn = archive.connect(db)
            conn.close()
        except Exception as exc:  # surface, don't swallow
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    # WAL mode is actually in effect.
    conn = archive.connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


# --- Transfer reconciliation storage foundation -------------------------------


def test_transfer_link_insert_and_load(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    link_id = archive.insert_transfer_link(
        conn, debit_txn_id="d1", credit_txn_id="c1", amount_cents=-10000,
        status="inferred", method="mutual-unique", confidence="inferred-structurally-forced",
        date_rule="same-day", explanation="sole candidate each way",
        candidates_before=1, candidates_after=1, reconcile_run_id="run-1",
    )
    rows = archive.load_transfer_links(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["link_id"] == link_id
    assert (r["debit_txn_id"], r["credit_txn_id"]) == ("d1", "c1")
    assert r["amount_cents"] == -10000
    assert r["status"] == "inferred"
    assert r["method"] == "mutual-unique"
    assert r["created_at"] and r["updated_at"]


def test_transfer_link_debit_leg_is_unique(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    archive.insert_transfer_link(conn, debit_txn_id="d1", credit_txn_id="c1", status="inferred")
    # A second link must not be able to claim the same debit leg.
    with pytest.raises(sqlite3.IntegrityError):
        archive.insert_transfer_link(conn, debit_txn_id="d1", credit_txn_id="c2", status="inferred")


def test_transfer_link_credit_leg_is_unique(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    archive.insert_transfer_link(conn, debit_txn_id="d1", credit_txn_id="c1", status="inferred")
    with pytest.raises(sqlite3.IntegrityError):
        archive.insert_transfer_link(conn, debit_txn_id="d2", credit_txn_id="c1", status="inferred")


def test_unmatched_legs_with_null_partner_coexist(tmp_path):
    # Several still-unmatched debits (credit_txn_id NULL) must not collide on the
    # UNIQUE(credit_txn_id) index -- SQLite exempts NULLs.
    conn = archive.connect(tmp_path / "a.db")
    archive.insert_transfer_link(conn, debit_txn_id="d1", status="unmatched")
    archive.insert_transfer_link(conn, debit_txn_id="d2", status="unmatched")
    archive.insert_transfer_link(conn, credit_txn_id="c1", status="unmatched")
    archive.insert_transfer_link(conn, credit_txn_id="c2", status="unmatched")
    assert len(archive.load_transfer_links(conn)) == 4


def test_account_type_upsert_and_load(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    archive.set_account_type(conn, "acct-617", "Investor Checking", source="inferred")
    archive.set_account_type(conn, "acct-307", "Investor Savings", source="heuristic")
    types = archive.load_account_types(conn)
    assert types["acct-617"]["product_type"] == "Investor Checking"
    assert types["acct-617"]["source"] == "inferred"
    assert types["acct-307"]["product_type"] == "Investor Savings"

    # Upsert overwrites in place (no duplicate row), e.g. a later user confirmation.
    archive.set_account_type(conn, "acct-617", "Investor Checking", source="confirmed")
    types = archive.load_account_types(conn)
    assert len(types) == 2
    assert types["acct-617"]["source"] == "confirmed"


def test_transfer_schema_added_to_existing_archive(tmp_path):
    # The new tables must appear via the IF NOT EXISTS migration on reconnect to a
    # pre-existing archive, not only on fresh creation.
    db = tmp_path / "a.db"
    conn = archive.connect(db)
    archive.upsert(conn, _normalized())
    conn.close()
    conn2 = archive.connect(db)
    names = {
        r[0] for r in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"transfer_links", "account_types"}.issubset(names)


def test_transfer_link_requires_at_least_one_leg(tmp_path):
    # A link that references neither a debit nor a credit leg is meaningless and
    # must be rejected by the CHECK constraint.
    conn = archive.connect(tmp_path / "a.db")
    with pytest.raises(sqlite3.IntegrityError):
        archive.insert_transfer_link(conn, status="unmatched")


def test_account_type_rejects_null_account_id(tmp_path):
    # A NULL account_id in a TEXT PRIMARY KEY would silently allow duplicate rows
    # that collapse to one key on load; NOT NULL forbids it.
    conn = archive.connect(tmp_path / "a.db")
    with pytest.raises(sqlite3.IntegrityError):
        archive.set_account_type(conn, None, "Investor Checking")


def test_transfer_link_txn_cannot_be_claimed_in_both_roles(tmp_path):
    # The per-column UNIQUE indexes alone would let the same txn be a debit in
    # one link and a credit in another; the cross-claim trigger forbids it.
    conn = archive.connect(tmp_path / "a.db")
    archive.insert_transfer_link(conn, debit_txn_id="txn-1", credit_txn_id="c1", status="inferred")
    with pytest.raises(sqlite3.IntegrityError):
        archive.insert_transfer_link(conn, debit_txn_id="d2", credit_txn_id="txn-1", status="inferred")


def test_transfer_link_rejects_self_link(tmp_path):
    # A transaction is money out or money in, never both legs of one link.
    conn = archive.connect(tmp_path / "a.db")
    with pytest.raises(sqlite3.IntegrityError):
        archive.insert_transfer_link(conn, debit_txn_id="x", credit_txn_id="x", status="inferred")
