import json
from pathlib import Path

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
