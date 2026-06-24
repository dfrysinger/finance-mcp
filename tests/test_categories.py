from finance_mcp import archive, categories


def _conn(tmp_path):
    return archive.connect(tmp_path / "a.db")


def _seed_txn(conn, tid):
    conn.execute("INSERT OR IGNORE INTO transactions (id) VALUES (?)", (tid,))
    conn.commit()


def _txn(tid, desc="", payee="", amount=-10.0):
    return {"id": tid, "description": desc, "payee": payee, "amount_float": amount}


def test_seed_default_rules_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    n1 = categories.seed_default_rules(conn)
    assert n1 > 0
    n2 = categories.seed_default_rules(conn)
    assert n2 == 0
    assert len(categories.list_rules(conn)) == n1


def test_force_reseed_restores_deleted_defaults_without_duplicating(tmp_path):
    conn = _conn(tmp_path)
    n1 = categories.seed_default_rules(conn)
    victim = categories.list_rules(conn)[0]["rule_id"]
    categories.remove_rule(conn, victim)
    assert len(categories.list_rules(conn)) == n1 - 1
    restored = categories.seed_default_rules(conn, force=True)
    assert restored == 1
    assert len(categories.list_rules(conn)) == n1  # no duplicates


def test_deleting_all_rules_is_durable(tmp_path):
    # Regression: an intentional empty rule set must survive subsequent reads.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    for r in categories.list_rules(conn):
        categories.remove_rule(conn, r["rule_id"])
    assert categories.list_rules(conn) == []
    # A later read calls seed again — the meta sentinel must prevent re-seeding.
    assert categories.seed_default_rules(conn) == 0
    assert categories.list_rules(conn) == []


def test_custom_rule_first_still_loads_defaults(tmp_path):
    # Regression: adding a custom rule before the first seeding read must NOT
    # suppress the defaults (otherwise transfer rules go missing and transfers
    # get counted as spending).
    conn = _conn(tmp_path)
    conn.execute("DELETE FROM meta WHERE key='rules_seeded'")
    conn.commit()
    categories.add_rule(conn, "mycustommerchant", "Custom")
    inserted = categories.seed_default_rules(conn)
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert inserted == len(categories.DEFAULT_RULES)  # all defaults loaded
    assert "mycustommerchant" in patterns  # custom rule preserved
    # a transfer default is present, so transfers will be excluded from spend
    assert any(r["is_transfer"] for r in categories.list_rules(conn))


def test_seed_fills_missing_defaults_without_duplicating_existing(tmp_path):
    # A pre-sentinel archive that already holds a rule matching a default pattern:
    # seeding fills the rest without duplicating that pattern.
    conn = _conn(tmp_path)
    conn.execute("DELETE FROM meta WHERE key='rules_seeded'")
    conn.commit()
    categories.add_rule(conn, "harmons", "MyGroceries")
    inserted = categories.seed_default_rules(conn)
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert patterns.count("harmons") == 1  # not duplicated
    assert inserted == len(categories.DEFAULT_RULES) - 1
    # the user's own mapping wins, the default 'harmons' rule was not added
    txns = [_txn("t1", desc="HARMONS #12")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "MyGroceries"


def test_rule_match_assigns_category(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    txns = [_txn("t1", desc="HARMONS #12 OREM UT")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"
    assert txns[0]["category_source"] == "rule"
    assert txns[0]["is_transfer"] is False


def test_unmatched_is_uncategorized(tmp_path):
    conn = _conn(tmp_path)
    txns = [_txn("t1", desc="SOME UNKNOWN MERCHANT")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED
    assert txns[0]["category_source"] == "none"


def test_priority_lower_wins(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "pizza", "Dining", priority=70)
    categories.add_rule(conn, "marcos pizza", "FastFood", priority=50)
    txns = [_txn("t1", desc="MARCOS PIZZA 123")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "FastFood"


def test_manual_override_wins_over_rule(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    _seed_txn(conn, "t1")
    categories.set_manual_category(conn, "t1", "Gifts")
    txns = [_txn("t1", desc="HARMONS #12")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Gifts"
    assert txns[0]["category_source"] == "manual"


def test_manual_override_upsert_replaces(tmp_path):
    conn = _conn(tmp_path)
    _seed_txn(conn, "t1")
    categories.set_manual_category(conn, "t1", "Gifts")
    categories.set_manual_category(conn, "t1", "Travel", is_transfer=False)
    txns = [_txn("t1")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Travel"


def test_clear_manual_falls_back_to_rule(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    _seed_txn(conn, "t1")
    categories.set_manual_category(conn, "t1", "Gifts")
    assert categories.clear_manual_category(conn, "t1") is True
    txns = [_txn("t1", desc="HARMONS")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"


def test_field_scoping_payee_only(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "amazon", "Shopping", field="payee")
    in_desc = [_txn("t1", desc="AMAZON.COM")]
    in_payee = [_txn("t2", payee="Amazon")]
    categories.apply_categories(conn, in_desc)
    categories.apply_categories(conn, in_payee)
    assert in_desc[0]["category"] == categories.UNCATEGORIZED
    assert in_payee[0]["category"] == "Shopping"


def test_transfer_flag_propagates(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "transfer to", "Transfer", is_transfer=True)
    txns = [_txn("t1", desc="TRANSFER TO SAVINGS")]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_transfer"] is True


def _txn_on(tid, account_id, desc="", payee="", amount=-10.0):
    t = _txn(tid, desc=desc, payee=payee, amount=amount)
    t["account_id"] = account_id
    return t


def test_account_scoped_rule_matches_only_its_account(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "funds tran", "Loan Payment", account_id="ACT-A")
    on_a = [_txn_on("t1", "ACT-A", desc="DANIEL FUNDS TRAN")]
    on_b = [_txn_on("t2", "ACT-B", desc="DANIEL FUNDS TRAN")]
    categories.apply_categories(conn, on_a)
    categories.apply_categories(conn, on_b)
    assert on_a[0]["category"] == "Loan Payment"
    assert on_b[0]["category"] == categories.UNCATEGORIZED


def test_account_scoped_rule_beats_global_pattern_on_its_account(tmp_path):
    conn = _conn(tmp_path)
    # A generic descriptor flagged as a transfer everywhere...
    categories.add_rule(conn, "funds tran", "Transfer", is_transfer=True, priority=12)
    # ...but on one account it is really a loan payment (real spend).
    categories.add_rule(
        conn, "funds tran", "Loan Payment",
        is_transfer=False, priority=8, account_id="ACT-LOANS",
    )
    on_loans = [_txn_on("t1", "ACT-LOANS", desc="DANIEL FUNDS TRAN")]
    elsewhere = [_txn_on("t2", "ACT-OTHER", desc="DANIEL FUNDS TRAN")]
    categories.apply_categories(conn, on_loans)
    categories.apply_categories(conn, elsewhere)
    assert on_loans[0]["category"] == "Loan Payment"
    assert on_loans[0]["is_transfer"] is False
    assert elsewhere[0]["category"] == "Transfer"
    assert elsewhere[0]["is_transfer"] is True


def test_unscoped_rule_still_matches_any_account(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    txns = [_txn_on("t1", "ACT-ANY", desc="HARMONS #4")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"


def test_account_scoped_rule_no_match_when_txn_has_no_account(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "funds tran", "Loan Payment", account_id="ACT-A")
    txns = [_txn("t1", desc="DANIEL FUNDS TRAN")]  # no account_id key
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_blank_account_id_stored_as_null_applies_everywhere(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries", account_id="")
    rid_rule = categories.list_rules(conn)[0]
    assert rid_rule["account_id"] is None
    txns = [_txn_on("t1", "ACT-ANY", desc="HARMONS")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"


def test_account_scoped_rule_applies_regardless_of_sign(tmp_path):
    # An account-scoped rule deliberately matches on description, not amount
    # sign: on a loan-payment account a debit is the payment (real spend) and a
    # credit with the same descriptor is a *returned* payment. Both should be
    # reclassified out of the generic-transfer bucket so the credit surfaces as
    # a refund inflow rather than a hidden transfer. Headline spend is
    # outflow-only, so the credit can never inflate spend.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "funds tran", "Transfer", is_transfer=True, priority=12)
    categories.add_rule(
        conn, "funds tran", "Loan Payment",
        is_transfer=False, priority=8, account_id="ACT-LOANS",
    )
    debit = [_txn_on("t1", "ACT-LOANS", desc="FUNDS TRAN", amount=-500.0)]
    credit = [_txn_on("t2", "ACT-LOANS", desc="FUNDS TRAN", amount=500.0)]
    categories.apply_categories(conn, debit)
    categories.apply_categories(conn, credit)
    for t in (debit[0], credit[0]):
        assert t["category"] == "Loan Payment"
        assert t["is_transfer"] is False


def test_additive_migration_adds_account_id_to_legacy_table(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    raw.execute(
        "CREATE TABLE category_rules ("
        " rule_id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL,"
        " field TEXT NOT NULL DEFAULT 'any', category TEXT NOT NULL,"
        " is_transfer INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,"
        " created_at TEXT)"
    )
    # A pre-existing rule written before the column existed must survive the
    # migration with account_id NULL and keep categorizing every account.
    raw.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, priority)"
        " VALUES ('harmons', 'any', 'Groceries', 0, 50)"
    )
    raw.commit()
    raw.close()

    conn = archive.connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(category_rules)").fetchall()}
    assert "account_id" in cols
    legacy = categories.list_rules(conn)
    assert len(legacy) == 1 and legacy[0]["account_id"] is None
    txns = [_txn_on("t1", "ACT-WHATEVER", desc="HARMONS #9")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"
    rid = categories.add_rule(conn, "funds tran", "Loan Payment", account_id="ACT-A")
    assert any(r["account_id"] == "ACT-A" for r in categories.list_rules(conn))
    conn.close()

    # Re-opening an already-migrated archive must be a clean no-op (no
    # duplicate-column crash, rows intact).
    conn2 = archive.connect(db)
    assert len(categories.list_rules(conn2)) == 2
    conn2.close()


def test_remove_rule(tmp_path):
    conn = _conn(tmp_path)
    rid = categories.add_rule(conn, "harmons", "Groceries")
    assert categories.remove_rule(conn, rid) is True
    assert categories.remove_rule(conn, rid) is False


def test_add_rule_rejects_bad_field(tmp_path):
    conn = _conn(tmp_path)
    try:
        categories.add_rule(conn, "x", "Y", field="bogus")
    except ValueError:
        return
    raise AssertionError("expected ValueError for bad field")


def test_add_rule_rejects_blank_pattern(tmp_path):
    conn = _conn(tmp_path)
    for bad in ("", "   "):
        try:
            categories.add_rule(conn, bad, "Groceries")
        except ValueError:
            continue
        raise AssertionError("expected ValueError for blank pattern")


def test_add_rule_rejects_blank_category(tmp_path):
    conn = _conn(tmp_path)
    try:
        categories.add_rule(conn, "harmons", "   ")
    except ValueError:
        return
    raise AssertionError("expected ValueError for blank category")


def test_set_manual_category_rejects_unknown_txn(tmp_path):
    conn = _conn(tmp_path)
    try:
        categories.set_manual_category(conn, "nope", "Gifts")
    except LookupError:
        assert conn.execute("SELECT COUNT(*) FROM transaction_categories").fetchone()[0] == 0
        return
    raise AssertionError("expected LookupError for unknown txn_id")


def test_coverage_report(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    txns = [
        _txn("t1", desc="HARMONS"),
        _txn("t2", desc="HARMONS"),
        _txn("t3", desc="MYSTERY"),
    ]
    report = categories.coverage(conn, txns)
    assert report["total"] == 3
    assert report["categorized"] == 2
    assert report["uncategorized"] == 1
    assert report["coverage_pct"] == 66.7
    assert report["categories"]["Groceries"] == 2


def test_p2p_payments_count_as_spending_not_transfer(tmp_path):
    # Regression: Venmo/Zelle/Cash App are dual-use; default-flagging them as
    # transfers would hide real outflow (rent, contractors) from the budget.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    for desc in ("VENMO PAYMENT 1234", "ZELLE TO JOHN", "CASH APP *RENT",
                 "ZELLE TRANSFER TO JOHN", "VENMO TRANSFER TO JANE"):
        txns = [_txn("t1", desc=desc, amount=-500.0)]
        categories.apply_categories(conn, txns)
        assert txns[0]["is_transfer"] is False, desc
        assert txns[0]["category"] == "P2P Payment", desc


def test_mobile_deposit_is_not_a_transfer(tmp_path):
    # Regression: a mobile check deposit is real inbound money, not an internal
    # move; flagging it is_transfer would hide income from inflow totals.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [_txn("t1", desc="DEPOSIT MOBILE BANKING", amount=1200.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_transfer"] is False
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_autopay_does_not_hide_real_bills(tmp_path):
    # Regression: a generic "autopay"/"e-payment" transfer rule must not shadow
    # specific merchant rules and hide real utility/insurance bills from the budget.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    categories.add_rule(conn, "rocky mountain power", "Utilities", field="any")
    txns = [_txn("t1", desc="ROCKY MOUNTAIN POWER AUTOPAY", amount=-150.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_transfer"] is False
    assert txns[0]["category"] == "Utilities"
    # "AUTOMATIC PAYMENT" is the same defect class — also must not hide the bill.
    spelled = [_txn("t3", desc="ROCKY MOUNTAIN POWER AUTOMATIC PAYMENT", amount=-150.0)]
    categories.apply_categories(conn, spelled)
    assert spelled[0]["is_transfer"] is False
    assert spelled[0]["category"] == "Utilities"
    # An unrecognized autopay stays visible spend, not a hidden transfer.
    other = [_txn("t2", desc="GEICO AUTOPAY", amount=-90.0)]
    categories.apply_categories(conn, other)
    assert other[0]["is_transfer"] is False


def test_atm_withdrawal_counts_as_spending_not_transfer(tmp_path):
    # Regression: bare "withdrawal" must NOT be a default transfer rule, or real
    # ATM/cash withdrawals would be hidden from the budget.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [_txn("t1", desc="ATM WITHDRAWAL 1234 MAIN ST", amount=-100.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_transfer"] is False
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_default_rules_cover_real_merchants(tmp_path):
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    cases = {
        "HARMONS #12": "Groceries",
        "MCDONALD'S F123": "Dining",
        "AMAZON.COM*ABC": "Shopping",
        "APPLE.COM/BILL": "Subscriptions",
        "Transfer To Share 5": "Transfer",
    }
    txns = [_txn(f"t{i}", desc=d) for i, d in enumerate(cases)]
    categories.apply_categories(conn, txns)
    got = {t["description"]: t["category"] for t in txns}
    for desc, expected in cases.items():
        assert got[desc] == expected, f"{desc} -> {got[desc]} != {expected}"


def test_concurrent_seeding_does_not_duplicate_rules(tmp_path):
    # Regression: two connections seeding an existing archive at the same time must
    # not double-insert the defaults (which would survive a single remove_rule and
    # keep hiding spend). BEGIN IMMEDIATE serializes them; the loser sees the
    # sentinel and inserts nothing.
    import threading

    db = tmp_path / "race.db"
    # Establish the archive (schema + WAL) first, as a prior sync would, then race
    # only the seeding — the concurrency the fix actually guards.
    archive.connect(db).close()

    barrier = threading.Barrier(2)
    errors = []

    def worker():
        conn = archive.connect(db)
        try:
            barrier.wait(timeout=5)
            categories.seed_default_rules(conn)
        except Exception as exc:  # surface, don't swallow
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    conn = archive.connect(db)
    try:
        patterns = [r["pattern"] for r in categories.list_rules(conn)]
    finally:
        conn.close()
    assert len(patterns) == len(set(patterns))
    assert len(patterns) == len(categories.DEFAULT_RULES)


def test_seed_refuses_to_commit_callers_transaction(tmp_path):
    # Regression: seed_default_rules must not commit a transaction it did not open
    # (that would make a caller's pending writes durable past their own rollback).
    import pytest

    conn = _conn(tmp_path)
    conn.execute("INSERT INTO meta (key, value) VALUES ('caller', 'pending')")
    assert conn.in_transaction
    with pytest.raises(RuntimeError):
        categories.seed_default_rules(conn)
    # The caller can still roll back their own work.
    conn.rollback()
    assert conn.execute(
        "SELECT value FROM meta WHERE key='caller'"
    ).fetchone() is None
    conn.close()


class _AlterProxy:
    """Minimal connection proxy: intercepts the additive ALTER, delegates rest.

    ``sqlite3.Connection`` is a C type and rejects attribute assignment, so the
    migration's exception branches can't be exercised by monkeypatching the
    connection directly. This proxy forwards ``execute``/``commit`` to a real
    connection but lets a test inject a failure on the column-add ALTER.
    """

    def __init__(self, real, on_alter):
        self._real = real
        self._on_alter = on_alter

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("ALTER TABLE CATEGORY_RULES ADD COLUMN"):
            return self._on_alter(sql, args)
        return self._real.execute(sql, *args)

    def commit(self):
        return self._real.commit()


def _legacy_conn(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    raw.execute(
        "CREATE TABLE category_rules ("
        " rule_id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL,"
        " field TEXT NOT NULL DEFAULT 'any', category TEXT NOT NULL,"
        " is_transfer INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,"
        " created_at TEXT)"
    )
    raw.commit()
    raw.row_factory = sqlite3.Row
    return raw


def test_migration_swallows_duplicate_column_race(tmp_path):
    # The loser of a concurrent first-connect ALTER hits "duplicate column
    # name" after the winner committed; the migration must treat that as benign.
    import sqlite3

    raw = _legacy_conn(tmp_path)

    def raise_duplicate(sql, args):
        raise sqlite3.OperationalError("duplicate column name: account_id")

    proxy = _AlterProxy(raw, raise_duplicate)
    archive._apply_additive_migrations(proxy)  # must not raise
    raw.close()


def test_migration_reraises_unrelated_operational_error(tmp_path):
    # Any OperationalError that is NOT a duplicate-column race must propagate so
    # a real failure (e.g. a locked database) is never silently swallowed.
    import sqlite3

    import pytest

    raw = _legacy_conn(tmp_path)

    def raise_locked(sql, args):
        raise sqlite3.OperationalError("database is locked")

    proxy = _AlterProxy(raw, raise_locked)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        archive._apply_additive_migrations(proxy)
    raw.close()


DEBT = frozenset({"ACT-LOAN-1", "ACT-LOAN-2"})


def test_debt_account_posting_pinned_to_loan_payment_transfer(tmp_path):
    # A lender labels a loan account's own payment posting "Principal Interest".
    # On a debt account that is NOT income — it must be pinned to Loan Payment and
    # excluded from summaries, even though the seeded income rule would match.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [_txn_on("t1", "ACT-LOAN-1", desc="PRINCIPAL INTEREST", amount=752.24)]
    categories.apply_categories(conn, txns, debt_account_ids=DEBT)
    assert txns[0]["category"] == categories.LOAN_PAYMENT
    assert txns[0]["is_transfer"] is True
    assert txns[0]["category_source"] == "debt_account"


def test_same_descriptor_on_non_debt_account_still_investment_income(tmp_path):
    # The exact descriptor on a real brokerage account (not a debt) keeps its
    # income rule: the debt pin is account-scoped, not a blanket suppression.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [_txn_on("t1", "ACT-BROKERAGE", desc="PRINCIPAL INTEREST", amount=120.0)]
    categories.apply_categories(conn, txns, debt_account_ids=DEBT)
    assert txns[0]["category"] == "Investment Income"
    assert txns[0]["is_transfer"] is False


def test_debt_pin_applies_regardless_of_sign(tmp_path):
    # Both a payment (here a credit posting on the loan) and a returned payment
    # (a debit reversal) on a debt account are debt activity, never income/spend.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    credit = [_txn_on("t1", "ACT-LOAN-2", desc="PRINCIPAL INTEREST", amount=500.0)]
    debit = [_txn_on("t2", "ACT-LOAN-2", desc="PAYMENT REVERSAL", amount=-500.0)]
    categories.apply_categories(conn, credit, debt_account_ids=DEBT)
    categories.apply_categories(conn, debit, debt_account_ids=DEBT)
    for t in (credit[0], debit[0]):
        assert t["category"] == categories.LOAN_PAYMENT
        assert t["is_transfer"] is True


def test_manual_override_beats_debt_pin(tmp_path):
    # An explicit manual category still wins over the debt-account pin.
    conn = _conn(tmp_path)
    _seed_txn(conn, "t1")
    categories.set_manual_category(conn, "t1", "Investment Income")
    txns = [_txn_on("t1", "ACT-LOAN-1", desc="PRINCIPAL INTEREST", amount=100.0)]
    categories.apply_categories(conn, txns, debt_account_ids=DEBT)
    assert txns[0]["category"] == "Investment Income"
    assert txns[0]["category_source"] == "manual"


def test_no_debt_accounts_is_backward_compatible(tmp_path):
    # Default (no debt set): the loan descriptor resolves to its income rule, the
    # prior behavior, so existing callers are unaffected.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [_txn_on("t1", "ACT-LOAN-1", desc="PRINCIPAL INTEREST", amount=100.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Investment Income"


def test_income_rule_sets_is_income(tmp_path):
    # A payroll deposit resolves to Income and is flagged is_income so spending
    # views can exclude it. Investment income is likewise flagged.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [
        _txn("t1", desc="GITHUB INC PAYROLL", amount=5000.0),
        _txn("t2", desc="DIVIDEND RECEIVED", amount=12.0),
        _txn("t3", desc="UNEMPLOYMENT BENEFIT", amount=685.0),
    ]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Income"
    assert txns[0]["is_income"] is True
    assert txns[1]["category"] == "Investment Income"
    assert txns[1]["is_income"] is True
    assert txns[2]["category"] == "Income"
    assert txns[2]["is_income"] is True


def test_spending_is_not_income(tmp_path):
    # A regular purchase and an uncategorized row are never flagged income.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    txns = [
        _txn("t1", desc="HARMONS #12", amount=-40.0),
        _txn("t2", desc="SOME UNKNOWN MERCHANT", amount=-9.0),
    ]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_income"] is False
    assert txns[1]["is_income"] is False


def test_transfer_dominates_income_flag(tmp_path):
    # A manual override marking a row as a transfer is never income, even if the
    # category name looks income-like.
    conn = _conn(tmp_path)
    _seed_txn(conn, "t1")
    categories.set_manual_category(conn, "t1", "Investment Income", is_transfer=True)
    txns = [_txn("t1", desc="WHATEVER", amount=100.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_transfer"] is True
    assert txns[0]["is_income"] is False


def test_debt_pin_is_not_income(tmp_path):
    # A debt-account posting is pinned to Loan Payment / transfer, never income.
    conn = _conn(tmp_path)
    txns = [_txn_on("t1", "ACT-LOAN-1", desc="PRINCIPAL INTEREST", amount=100.0)]
    categories.apply_categories(conn, txns, debt_account_ids=DEBT)
    assert txns[0]["category"] == categories.LOAN_PAYMENT
    assert txns[0]["is_income"] is False


def test_income_patterns_do_not_match_unrelated_spend(tmp_path):
    # Income substrings must not collide with real merchant spend. Regression:
    # "pension" once matched "SUSPENSION" and buried auto-repair outflow as income.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    txns = [_txn("t1", desc="PEP BOYS BRAKE SUSPENSION SERVICE", amount=-220.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] != "Income"
    assert txns[0]["is_income"] is False


def test_prune_removes_stale_pension_default_from_seeded_archive(tmp_path):
    # An archive seeded before "pension" was removed still holds the stale default.
    # seed_default_rules early-returns once seeded, so the prune must delete it.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, priority, created_at) "
        "VALUES ('pension','any','Income',0,20,?)",
        (categories._now(),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('rules_seeded', ?)",
        (categories._now(),),
    )
    conn.commit()
    inserted = categories.seed_default_rules(conn)  # already seeded -> early return path
    assert inserted == 0
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert "pension" not in patterns
    # A SUSPENSION purchase is no longer income.
    txns = [_txn("t1", desc="PEP BOYS BRAKE SUSPENSION SERVICE", amount=-220.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["is_income"] is False


def test_prune_preserves_user_custom_pension_rule(tmp_path):
    # Only the exact default signature is removed. A user's own "pension" rule
    # (different category) must survive the prune.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "pension", "Retirement Spending")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('rules_seeded', ?)",
        (categories._now(),),
    )
    conn.commit()
    categories.seed_default_rules(conn)
    rules = {r["pattern"]: r["category"] for r in categories.list_rules(conn)}
    assert rules.get("pension") == "Retirement Spending"


def test_prune_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    # Re-running must not error and must leave the version sentinel set.
    categories.seed_default_rules(conn)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='obsolete_default_rules_version'"
    ).fetchone()
    assert int(row["value"]) == categories._OBSOLETE_DEFAULTS_VERSION
