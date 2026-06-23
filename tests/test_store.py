import json

from finance_mcp import store


def test_load_missing_returns_empty(tmp_path):
    cache = store.load_cache(tmp_path / "nope.json")
    assert cache["transactions"] == []
    assert cache["accounts"] == []
    assert cache["synced_at"] is None


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    cache = dict(store.EMPTY_CACHE)
    cache["transactions"] = [{"id": "t1", "amount_float": -5.0}]
    store.save_cache(cache, path)
    loaded = store.load_cache(path)
    assert loaded["transactions"][0]["id"] == "t1"
    assert loaded["synced_at"] is not None


def test_save_sets_owner_only_permissions(tmp_path):
    path = tmp_path / "cache.json"
    store.save_cache(dict(store.EMPTY_CACHE), path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_corrupt_cache_falls_back_to_empty(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    cache = store.load_cache(path)
    assert cache["transactions"] == []


def test_load_backfills_missing_keys(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"transactions": []}), encoding="utf-8")
    cache = store.load_cache(path)
    assert "accounts" in cache and "errors" in cache and "errlist" in cache


def test_archive_view_seeds_rules_and_excludes_transfers(tmp_path, monkeypatch):
    # Regression: the read path must seed default rules so is_transfer is set and
    # spending_summary's default exclude_transfers actually excludes transfers.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    from finance_mcp import archive, queries

    conn = archive.connect(tmp_path / "archive.db")
    norm = {
        "accounts": [{"account_id": "A", "account_name": "A", "org": "O",
                      "balance_date_ts": 1, "balance": "1", "balance_float": 1.0,
                      "balance_date": "2024-01-01T00:00:00+00:00"}],
        "transactions": [
            {"id": "x1", "account_id": "A", "posted_ts": 100, "posted": "2024-01-01",
             "description": "AUTOMATIC PAYMENT - THANK YOU", "amount": "-5000",
             "amount_float": -5000.0, "pending": False},
            {"id": "x2", "account_id": "A", "posted_ts": 200, "posted": "2024-01-02",
             "description": "HARMONS #12", "amount": "-50", "amount_float": -50.0,
             "pending": False},
        ],
    }
    archive.upsert(conn, norm)
    conn.close()

    view = store.load_archive_view()
    txns = view["transactions"]
    by_id = {t["id"]: t for t in txns}
    assert by_id["x1"]["is_transfer"] is True
    assert by_id["x1"]["category"] == "Credit Card Payment"

    summary = queries.spending_summary(txns, group_by="category")
    assert summary["total_outflow"] == -50.0  # the 5000 payment excluded


def test_archive_view_categorizes_cache_fallback_when_no_archive(tmp_path, monkeypatch):
    # Regression: with a legacy cache.json but no archive.db, reads must still
    # categorize so a transfer-excluding summary does not count transfers as spend.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    from finance_mcp import config, queries

    cache = dict(store.EMPTY_CACHE)
    cache["transactions"] = [
        {"id": "c1", "description": "AUTOMATIC PAYMENT - THANK YOU",
         "amount": "-5000", "amount_float": -5000.0, "pending": False,
         "posted": "2024-01-01"},
        {"id": "c2", "description": "HARMONS #12", "amount": "-50",
         "amount_float": -50.0, "pending": False, "posted": "2024-01-02"},
    ]
    store.save_cache(cache, config.cache_path())
    assert not (tmp_path / "archive.db").exists()

    view = store.load_archive_view()
    by_id = {t["id"]: t for t in view["transactions"]}
    assert by_id["c1"]["is_transfer"] is True
    assert by_id["c1"]["category"] == "Credit Card Payment"

    summary = queries.spending_summary(view["transactions"], group_by="category")
    assert summary["total_outflow"] == -50.0  # transfer excluded


def test_set_manual_category_accepts_cache_fallback_txn(tmp_path, monkeypatch):
    # Regression: a transaction served only from the cache fallback (archive.db
    # empty) must be settable, since load_archive_view just returned it.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    from finance_mcp import archive, categories, config

    cache = dict(store.EMPTY_CACHE)
    cache["transactions"] = [
        {"id": "legacy1", "description": "SOME MERCHANT", "amount": "-12",
         "amount_float": -12.0, "pending": False, "posted": "2024-01-01"},
    ]
    store.save_cache(cache, config.cache_path())

    conn = archive.connect(tmp_path / "archive.db")  # empty archive
    try:
        categories.set_manual_category(conn, "legacy1", "Gifts")  # must not raise
        row = conn.execute(
            "SELECT category FROM transaction_categories WHERE txn_id='legacy1'"
        ).fetchone()
        assert row["category"] == "Gifts"
        # an unknown id still fails loudly
        import pytest
        with pytest.raises(LookupError):
            categories.set_manual_category(conn, "does-not-exist", "Gifts")
    finally:
        conn.close()


def test_archive_view_excludes_debt_account_postings_from_income(tmp_path, monkeypatch):
    # Regression: a loan account's own "Principal Interest" posting must not be
    # counted as Investment Income. With the account listed as a debt in the
    # budget config, the read path pins it to a transfer category so it leaves
    # both income (inflow) and spend (outflow) totals.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    from finance_mcp import archive, config, queries

    budget = {
        "version": 1,
        "envelopes": [],
        "debt_accounts": [{"account_id": "LOAN", "label": "2nd Mortgage"}],
    }
    config.budget_config_path().write_text(json.dumps(budget), encoding="utf-8")

    conn = archive.connect(tmp_path / "archive.db")
    norm = {
        "accounts": [{"account_id": "LOAN", "account_name": "Loan", "org": "O",
                      "balance_date_ts": 1, "balance": "-1000", "balance_float": -1000.0,
                      "balance_date": "2024-01-01T00:00:00+00:00"}],
        "transactions": [
            {"id": "p1", "account_id": "LOAN", "posted_ts": 100, "posted": "2024-01-01",
             "description": "PRINCIPAL INTEREST", "amount": "752.24",
             "amount_float": 752.24, "pending": False},
        ],
    }
    archive.upsert(conn, norm)
    conn.close()

    view = store.load_archive_view()
    txn = view["transactions"][0]
    assert txn["category"] == "Loan Payment"
    assert txn["is_transfer"] is True

    summary = queries.spending_summary(view["transactions"], group_by="category")
    assert summary["total_inflow"] == 0.0  # not counted as income


def test_archive_view_warns_on_invalid_budget_config_not_silent(tmp_path, monkeypatch, capsys):
    # A present-but-invalid budget.json must NOT silently drop debt accounts (that
    # would reinstate income inflation). The read path keeps working but warns.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    from finance_mcp import archive, config

    config.budget_config_path().write_text("{ not valid json", encoding="utf-8")
    conn = archive.connect(tmp_path / "archive.db")
    norm = {
        "accounts": [{"account_id": "LOAN", "account_name": "Loan", "org": "O",
                      "balance_date_ts": 1, "balance": "-1", "balance_float": -1.0,
                      "balance_date": "2024-01-01T00:00:00+00:00"}],
        "transactions": [
            {"id": "p1", "account_id": "LOAN", "posted_ts": 100, "posted": "2024-01-01",
             "description": "PRINCIPAL INTEREST", "amount": "752.24",
             "amount_float": 752.24, "pending": False},
        ],
    }
    archive.upsert(conn, norm)
    conn.close()

    view = store.load_archive_view()
    # Degrades to no debt accounts (read path still works) ...
    assert view["transactions"][0]["category"] == "Investment Income"
    # ... but warns so the user knows their debts aren't being applied.
    assert "could not be loaded" in capsys.readouterr().err


def test_archive_view_tolerates_missing_budget_config(tmp_path, monkeypatch, capsys):
    # No budget.json: the read path must still categorize (no debt accounts), not
    # raise and not warn. The loan descriptor falls back to its income rule, as
    # before. A missing config is the legitimate "no debts" case, distinct from a
    # present-but-invalid one (which warns).
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    from finance_mcp import archive, config

    assert not config.budget_config_path().exists()
    conn = archive.connect(tmp_path / "archive.db")
    norm = {
        "accounts": [{"account_id": "A", "account_name": "A", "org": "O",
                      "balance_date_ts": 1, "balance": "1", "balance_float": 1.0,
                      "balance_date": "2024-01-01T00:00:00+00:00"}],
        "transactions": [
            {"id": "x1", "account_id": "A", "posted_ts": 100, "posted": "2024-01-01",
             "description": "PRINCIPAL INTEREST", "amount": "120",
             "amount_float": 120.0, "pending": False},
        ],
    }
    archive.upsert(conn, norm)
    conn.close()

    view = store.load_archive_view()
    assert view["transactions"][0]["category"] == "Investment Income"
    assert capsys.readouterr().err == ""  # missing config is silent
