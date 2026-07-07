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
    # Mark the new-defaults backfill as already applied so this prune-focused test
    # isn't perturbed by it (the backfill has its own dedicated tests).
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('new_default_rules_version', ?)",
        (str(categories._NEW_DEFAULTS_VERSION),),
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


def test_backfill_adds_new_default_into_already_seeded_archive(tmp_path):
    # An archive seeded before the income rules existed must receive them, even
    # though seed_default_rules early-returns once seeded. Simulate a legacy
    # archive: mark it seeded but strip the new income defaults and the version
    # sentinel that records the backfill as applied.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    conn.execute(
        "DELETE FROM category_rules WHERE pattern IN ('unemployment','social security')"
    )
    conn.execute("DELETE FROM meta WHERE key='new_default_rules_version'")
    conn.commit()
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert "unemployment" not in patterns

    inserted = categories.seed_default_rules(conn)  # already seeded -> early return path
    # The early-return path now honestly reports the rules the backfill inserted.
    assert inserted == 2
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert "unemployment" in patterns
    assert "social security" in patterns
    # A benefit deposit now resolves to income and is excluded from spending.
    txns = [_txn("t1", payee="Unemployment Insurance", amount=685.1)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Income"
    assert txns[0]["is_income"] is True


def test_backfill_does_not_duplicate_on_fresh_archive(tmp_path):
    # A fresh archive's normal seed already inserts the income rules; the backfill
    # must not add a second copy regardless of ordering.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM category_rules WHERE pattern='unemployment'"
        ).fetchone()[0]
        == 1
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key='new_default_rules_version'"
    ).fetchone()
    assert int(row["value"]) == categories._NEW_DEFAULTS_VERSION


def test_backfill_preserves_user_custom_same_pattern_rule(tmp_path):
    # A user who already curated their own "unemployment" rule (different
    # category) must not get a duplicate default appended.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    conn.execute(
        "DELETE FROM category_rules WHERE pattern IN ('unemployment','social security')"
    )
    conn.execute("DELETE FROM meta WHERE key='new_default_rules_version'")
    conn.commit()
    categories.add_rule(conn, "unemployment", "Side Gig")
    categories.seed_default_rules(conn)
    rules = [r for r in categories.list_rules(conn) if r["pattern"] == "unemployment"]
    assert len(rules) == 1
    assert rules[0]["category"] == "Side Gig"


def test_backfill_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    categories.seed_default_rules(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM category_rules WHERE pattern='unemployment'"
        ).fetchone()[0]
        == 1
    )


def test_fresh_seed_records_version_so_later_deletion_is_preserved(tmp_path):
    # A fresh seed by this version records new_default_rules_version, so a user who
    # then deletes a backfilled default does NOT get it resurrected on the next read.
    # This closes the forward-going window of the v1 backfill's known limitation.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='new_default_rules_version'"
    ).fetchone()
    assert int(row["value"]) == categories._NEW_DEFAULTS_VERSION
    rid = next(
        r["rule_id"]
        for r in categories.list_rules(conn)
        if r["pattern"] == "unemployment"
    )
    categories.remove_rule(conn, rid)
    categories.seed_default_rules(conn)
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert "unemployment" not in patterns


def test_backfill_force_reseed_counts_backfilled_rules(tmp_path):
    # force=True on a legacy seeded archive that predates the income defaults must
    # report the backfilled rows in its returned count (the documented contract is
    # "the number of default rules actually inserted").
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)
    conn.execute(
        "DELETE FROM category_rules WHERE pattern IN ('unemployment','social security')"
    )
    conn.execute("DELETE FROM meta WHERE key='new_default_rules_version'")
    conn.commit()
    inserted = categories.seed_default_rules(conn, force=True)
    # The two income defaults were backfilled; nothing else was missing.
    assert inserted == 2
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert patterns.count("unemployment") == 1
    assert patterns.count("social security") == 1


def test_backfill_version_bump_does_not_resurrect_deleted_rule(tmp_path, monkeypatch):
    # After v1 is applied and the user deletes a v1 backfilled default, a later
    # version bump that adds a brand-new default must insert ONLY the new rule and
    # must NOT resurrect the deleted one.
    conn = _conn(tmp_path)
    categories.seed_default_rules(conn)  # applies v1
    categories.remove_rule(
        conn,
        next(
            r["rule_id"]
            for r in categories.list_rules(conn)
            if r["pattern"] == "unemployment"
        ),
    )
    # Simulate a future release: bump the version and append a v2 rule.
    monkeypatch.setattr(
        categories,
        "_NEW_DEFAULT_RULES",
        [
            ("unemployment", "any", "Income", 0, 20, 1),
            ("social security", "any", "Income", 0, 20, 1),
            ("child support", "any", "Income", 0, 20, 2),
        ],
    )
    monkeypatch.setattr(categories, "_NEW_DEFAULTS_VERSION", 2)
    inserted = categories.seed_default_rules(conn)
    patterns = [r["pattern"] for r in categories.list_rules(conn)]
    assert "child support" in patterns  # v2 rule backfilled
    assert "unemployment" not in patterns  # deleted v1 rule stays gone
    assert inserted == 1


# --- Per-transaction predicates: amount magnitude, day-of-month, regex --------

from datetime import datetime, timezone

import pytest

from finance_mcp import archive as _archive


def _ts(year, month, day):
    """Epoch seconds at midnight UTC, matching how the importer stores posted_ts."""
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _dated_txn(tid, *, desc="", payee="", amount=-10.0, day=None, ts=None):
    txn = {"id": tid, "description": desc, "payee": payee, "amount_float": amount}
    if ts is not None:
        txn["posted_ts"] = ts
    elif day is not None:
        txn["posted_ts"] = _ts(2026, 4, day)
    return txn


def test_amount_band_matches_magnitude_not_sign(tmp_path):
    # A 200-350 band matches a $304 *charge* (negative amount) by magnitude.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla, inc", "Insurance",
                        amount_min=200, amount_max=350)
    txns = [_dated_txn("t1", desc="TESLA, INC. 45500 FREMO", amount=-304.76)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Insurance"


def test_amount_band_excludes_out_of_range(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla, inc", "Insurance",
                        amount_min=200, amount_max=350)
    # A $900 Supercharge/parts charge at the same merchant is left alone.
    txns = [_dated_txn("t1", desc="TESLA, INC. 45500 FREMO", amount=-900.00)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_amount_band_is_inclusive_at_bounds(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", amount_min=200, amount_max=350)
    lo = _dated_txn("lo", desc="TESLA", amount=-200.0)
    hi = _dated_txn("hi", desc="TESLA", amount=-350.0)
    categories.apply_categories(conn, [lo, hi])
    assert lo["category"] == "Insurance"
    assert hi["category"] == "Insurance"


def test_amount_min_only_and_max_only(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "floor", "Big", amount_min=100)
    categories.add_rule(conn, "ceil", "Small", amount_max=50)
    txns = [
        _dated_txn("a", desc="FLOOR", amount=-150.0),   # >= 100 -> Big
        _dated_txn("b", desc="FLOOR", amount=-40.0),    # < 100 -> not Big
        _dated_txn("c", desc="CEIL", amount=-40.0),     # <= 50 -> Small
        _dated_txn("d", desc="CEIL", amount=-90.0),     # > 50 -> not Small
    ]
    categories.apply_categories(conn, txns)
    assert [t["category"] for t in txns] == [
        "Big", categories.UNCATEGORIZED, "Small", categories.UNCATEGORIZED,
    ]


def test_amount_predicate_fails_closed_when_amount_missing(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", amount_min=200, amount_max=350)
    txn = {"id": "t1", "description": "TESLA", "payee": "", "amount_float": None}
    categories.apply_categories(conn, [txn])
    assert txn["category"] == categories.UNCATEGORIZED


def test_day_of_month_band_matches(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", day_min=12, day_max=17)
    inside = _dated_txn("in", desc="TESLA", day=15)
    below = _dated_txn("lo", desc="TESLA", day=3)
    above = _dated_txn("hi", desc="TESLA", day=28)
    categories.apply_categories(conn, [inside, below, above])
    assert inside["category"] == "Insurance"
    assert below["category"] == categories.UNCATEGORIZED
    assert above["category"] == categories.UNCATEGORIZED


def test_day_band_falls_back_to_posted_string(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", day_min=12, day_max=17)
    txn = {"id": "t1", "description": "TESLA", "payee": "",
           "amount_float": -300.0, "posted": "2026-05-15"}
    categories.apply_categories(conn, [txn])
    assert txn["category"] == "Insurance"


def test_day_predicate_fails_closed_when_date_missing(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", day_min=12, day_max=17)
    txn = {"id": "t1", "description": "TESLA", "payee": "", "amount_float": -300.0}
    categories.apply_categories(conn, [txn])
    assert txn["category"] == categories.UNCATEGORIZED


def test_tesla_insurance_discriminator_end_to_end(tmp_path):
    # The motivating case: isolate the monthly mid-month $200-350 Tesla insurance
    # premium from other Tesla charges using merchant + amount + day together.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance",
                        amount_min=200, amount_max=350, day_min=12, day_max=17,
                        priority=50)
    categories.add_rule(conn, "tesla", "Auto", priority=60)
    premium = _dated_txn("p", desc="TESLA, INC. 45500 FREMO", amount=-304.76, day=15)
    supercharge = _dated_txn("s", desc="TESLA, INC. 45500 FREMO", amount=-22.10, day=8)
    parts = _dated_txn("r", desc="TESLA, INC. 45500 FREMO", amount=-512.00, day=20)
    categories.apply_categories(conn, [premium, supercharge, parts])
    assert premium["category"] == "Insurance"
    assert supercharge["category"] == "Auto"
    assert parts["category"] == "Auto"


def test_regex_match_survives_inserted_store_number(tmp_path):
    # Substring "rei sandy" fails against "REI #81 SANDY SANDY"; a regex spanning
    # the inserted store number matches.
    conn = _conn(tmp_path)
    categories.add_rule(conn, r"rei .*sandy", "Shopping", match_mode="regex")
    txns = [_dated_txn("t1", desc="REI #81 SANDY SANDY")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Shopping"


def test_regex_is_case_insensitive(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, r"AMZN\s+Mktp", "Shopping", match_mode="regex")
    txns = [_dated_txn("t1", desc="amzn mktp us*1a2b3")]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Shopping"


def test_regex_anchors_and_alternation(tmp_path):
    conn = _conn(tmp_path)
    categories.add_rule(conn, r"^(uber|lyft)\b", "Travel", match_mode="regex")
    ride = _dated_txn("a", desc="UBER TRIP 123")
    grub = _dated_txn("b", desc="GRUBHUB UBER EATS")  # not anchored at start
    categories.apply_categories(conn, [ride, grub])
    assert ride["category"] == "Travel"
    assert grub["category"] == categories.UNCATEGORIZED


def test_add_rule_rejects_invalid_regex(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, r"tesla(", "Insurance", match_mode="regex")


def test_add_rule_rejects_bad_match_mode(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", match_mode="fuzzy")


def test_add_rule_rejects_inverted_amount_band(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", amount_min=350, amount_max=200)


def test_add_rule_rejects_negative_amount_bound(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", amount_min=-5)


def test_add_rule_rejects_non_finite_amount_bound(tmp_path):
    # NaN/inf slip past the >=0 and min<=max checks (NaN comparisons are False),
    # so they must be rejected explicitly or they silently neuter the predicate.
    conn = _conn(tmp_path)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            categories.add_rule(conn, "x", "Y", amount_min=bad)
        with pytest.raises(ValueError):
            categories.add_rule(conn, "x", "Y", amount_max=bad)


def test_amount_predicate_fails_closed_on_non_finite_amount(tmp_path):
    # A transaction whose amount is NaN/inf (malformed upstream/cache data) must
    # not satisfy a bounded rule — every comparison against NaN is False.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", amount_min=200, amount_max=350)
    for bad in (float("nan"), float("inf")):
        txn = {"id": "t1", "description": "TESLA", "payee": "", "amount_float": bad}
        categories.apply_categories(conn, [txn])
        assert txn["category"] == categories.UNCATEGORIZED


def test_add_rule_rejects_inverted_day_band(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_min=20, day_max=10)


def test_add_rule_rejects_out_of_range_day(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_min=0)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_max=32)


def test_add_rule_rejects_non_finite_or_fractional_day_bound(tmp_path):
    # int(inf) raises OverflowError (not ValueError), so without normalization a
    # programmatic caller would get an uncaught crash instead of a clean reject.
    # A fractional float must be rejected, not silently truncated.
    conn = _conn(tmp_path)
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            categories.add_rule(conn, "x", "Y", day_min=bad)
        with pytest.raises(ValueError):
            categories.add_rule(conn, "x", "Y", day_max=bad)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_min=15.7)


def test_amount_predicate_fails_closed_on_non_finite_stored_bound(tmp_path):
    # A row written while the validation gap was open (or hand-edited) can carry
    # an infinite bound; at match time that must fail closed, not match everything.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, amount_max) VALUES ('tesla', 'any', 'Insurance', 0, 50, ?)",
        (float("inf"),),
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA", amount=-999999999.0)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_day_predicate_fails_closed_on_non_finite_stored_bound(tmp_path):
    # Symmetric to the amount case: a hand-edited inf in the day column must fail
    # closed, not silently un-bound the day predicate.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, day_max) VALUES ('tesla', 'any', 'Insurance', 0, 50, ?)",
        (float("inf"),),
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA", amount=-300.0, day=15)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_match_fails_closed_on_non_numeric_stored_bound(tmp_path):
    # A hand-edited row putting TEXT into the REAL/INTEGER bound columns must not
    # crash categorization; the rule fails closed instead.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, amount_min) VALUES ('tesla', 'any', 'Insurance', 0, 50, 'abc')"
    )
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, day_min) VALUES ('costco', 'any', 'Groceries', 0, 50, 'xyz')"
    )
    conn.commit()
    txns = [
        _dated_txn("a", desc="TESLA", amount=-300.0, day=15),
        _dated_txn("b", desc="COSTCO", amount=-80.0, day=15),
    ]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED
    assert txns[1]["category"] == categories.UNCATEGORIZED


def test_add_rule_rejects_non_integral_decimal_day_bound(tmp_path):
    # A Decimal (or numeric string) caller must not bypass the float-only guard
    # and get silently truncated.
    from decimal import Decimal

    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_min=Decimal("15.7"))
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_max="20.5")
    with pytest.raises(ValueError):
        categories.add_rule(conn, "x", "Y", day_min=True)
    # An integral Decimal / numeric string is still accepted and normalized.
    rid = categories.add_rule(conn, "z", "W", day_min=Decimal("12"), day_max="17")
    stored = [r for r in categories.list_rules(conn) if r["rule_id"] == rid][0]
    assert stored["day_min"] == 12 and stored["day_max"] == 17


def test_regex_pattern_preserves_case_in_storage(tmp_path):
    # Substring rules are lowercased on store; a regex must NOT be (would corrupt
    # tokens like \D). Verify the stored pattern keeps its original case.
    conn = _conn(tmp_path)
    categories.add_rule(conn, r"Tesla\D+Inc", "Insurance", match_mode="regex")
    stored = [r for r in categories.list_rules(conn) if r["category"] == "Insurance"][0]
    assert stored["pattern"] == r"Tesla\D+Inc"
    assert stored["match_mode"] == "regex"


def test_malformed_regex_in_db_matches_nothing(tmp_path):
    # A regex hand-edited straight into the DB (bypassing add_rule validation)
    # must never crash categorization — it matches nothing instead.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, match_mode) VALUES ('tesla(', 'any', 'Insurance', 0, 50, 'regex')"
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA")]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_predicates_default_to_match_anything(tmp_path):
    # A plain rule (no predicates) still matches regardless of amount/date.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    txns = [_dated_txn("t1", desc="HARMONS #12", amount=-9999.0, day=1)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"


def test_legacy_archive_migrates_predicate_columns(tmp_path):
    # An archive whose category_rules predates the predicate columns gains them
    # on connect; the existing row keeps working and defaults to substring/NULL.
    import sqlite3 as _sqlite3

    db = tmp_path / "legacy.db"
    raw = _sqlite3.connect(db)
    # Old schema: base columns + account_id only (pre-predicate era). Because
    # archive.connect uses CREATE TABLE IF NOT EXISTS, this pre-existing shape
    # survives and only the additive migration fills the gap.
    raw.execute(
        "CREATE TABLE category_rules ("
        "rule_id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL, "
        "field TEXT NOT NULL DEFAULT 'any', category TEXT NOT NULL, "
        "is_transfer INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100, "
        "account_id TEXT, created_at TEXT)"
    )
    raw.execute(
        "INSERT INTO category_rules (pattern, field, category) "
        "VALUES ('harmons', 'any', 'Groceries')"
    )
    raw.commit()
    raw.close()

    conn = _archive.connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(category_rules)")}
    for c in ("amount_min", "amount_max", "day_min", "day_max", "match_mode"):
        assert c in cols
    # The legacy row defaults to substring matching and still classifies.
    rule = categories.list_rules(conn)[0]
    assert rule["match_mode"] == "substring"
    assert rule["amount_min"] is None and rule["day_max"] is None
    txns = [_dated_txn("t1", desc="HARMONS #12", amount=-50.0, day=9)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == "Groceries"
    conn.close()


def test_add_rule_rejects_non_numeric_amount_bound_type(tmp_path):
    # A non-numeric, non-string amount bound (e.g. a JSON array from an MCP
    # caller) must raise ValueError, not TypeError, so the entrypoint's
    # `except ValueError` envelope catches it. Mirrors the day-bound contract.
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", amount_min=[1, 2])
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", amount_max={"x": 1})


def test_add_rule_rejects_bool_amount_bound(tmp_path):
    # True/False as a money magnitude is a caller bug; reject outright instead
    # of silently storing 1.0/0.0.
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", amount_min=True)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", amount_max=False)


def test_categorization_survives_malformed_transaction_amount(tmp_path):
    # A non-numeric amount_float on one transaction must not crash the whole
    # pass. The amount-agnostic rule still classifies a sibling transaction, and
    # the malformed one falls through to Uncategorized.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "harmons", "Groceries")
    txns = [
        {"id": "bad", "description": "HARMONS #1", "payee": "", "amount_float": "abc"},
        _dated_txn("good", desc="HARMONS #2", amount=-12.0),
    ]
    categories.apply_categories(conn, txns)  # must not raise
    # The amount-agnostic rule does not depend on magnitude, so even the
    # malformed row matches; the key assertion is that nothing raised.
    assert txns[1]["category"] == "Groceries"


def test_amount_rule_fails_closed_on_malformed_transaction_amount(tmp_path):
    # An amount-constrained rule cannot verify a malformed amount, so it fails
    # closed (no match) rather than crashing.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", amount_min=200, amount_max=350)
    txns = [{"id": "bad", "description": "TESLA", "payee": "", "amount_float": "abc"}]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_day_predicate_fails_closed_on_fractional_stored_bound(tmp_path):
    # A fractional day bound can only reach the matcher via a hand-edited row
    # (input validation rejects it). It is not a whole day-of-month, so the day
    # predicate fails closed instead of matching on a partial day.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, day_min, day_max) VALUES ('tesla', 'any', 'Insurance', 0, 50, 15.7, 20.0)"
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA", day=16)]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_day_predicate_fails_closed_on_out_of_range_stored_bound(tmp_path):
    # A day bound outside 1..31 (hand-edited row) is impossible, so the day
    # predicate fails closed rather than matching nothing-or-everything.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, day_max) VALUES ('tesla', 'any', 'Insurance', 0, 50, 99)"
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA", day=16)]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_amount_rule_fails_closed_on_boolean_transaction_amount(tmp_path):
    # bool is an int subclass; a stored amount_float of True must not coerce to
    # 1.0 and satisfy an amount band. The predicate fails closed.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", amount_min=1, amount_max=1)
    txns = [{"id": "b", "description": "TESLA", "payee": "", "amount_float": True}]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_unknown_stored_match_mode_fails_closed(tmp_path):
    # A match_mode that is neither 'substring' nor 'regex' (hand-edited /
    # affinity-corrupted row) is not evaluable and must match nothing rather
    # than silently degrade to a substring match.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, match_mode) VALUES ('tesla', 'any', 'Insurance', 0, 50, 'bogus')"
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA")]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_day_rule_fails_closed_on_boolean_posted_ts(tmp_path):
    # A boolean posted_ts must not resolve to 1970-01-01 (day 1) and satisfy a
    # day-of-month predicate. With no other usable date, the day predicate fails
    # closed.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", day_min=1, day_max=1)
    txns = [{"id": "b", "description": "TESLA", "payee": "", "posted_ts": True}]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_add_rule_normalizes_overflow_amount_bound_to_value_error(tmp_path):
    # An over-large Python int makes float() raise OverflowError (not
    # ValueError); the validator must normalize it so the MCP/CLI entrypoint's
    # `except ValueError` envelope catches it instead of crashing.
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", amount_min=10**400)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", amount_max=10**400)


def test_add_rule_normalizes_overflow_day_bound_to_value_error(tmp_path):
    # Symmetric with the amount path: an over-large day bound normalizes to
    # ValueError rather than leaking OverflowError.
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "tesla", "Insurance", day_min=10**400)


def test_empty_stored_match_mode_fails_closed(tmp_path):
    # An empty-string match_mode is falsy but invalid; it must NOT default to
    # substring matching. Only a NULL/missing mode (legacy) defaults to substring.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, match_mode) VALUES ('tesla', 'any', 'Insurance', 0, 50, '')"
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA")]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_non_string_stored_pattern_fails_closed(tmp_path):
    # A BLOB pattern (hand-edited / affinity-corrupted) must not crash the pass
    # in either substring or regex mode — it fails closed (matches nothing).
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, match_mode) VALUES (?, 'any', 'Insurance', 0, 50, 'substring')",
        (b"tesla",),
    )
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, match_mode) VALUES (?, 'any', 'Fee', 0, 51, 'regex')",
        (b"tesla", ),
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA")]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_amount_rule_fails_closed_on_negative_stored_bound(tmp_path):
    # A negative stored amount bound is out of domain (magnitudes are >= 0) and
    # only reachable via a hand-edited row. It must fail closed, not silently
    # drop the lower bound and match everything.
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, "
        "priority, amount_min) VALUES ('tesla', 'any', 'Insurance', 0, 50, -5.0)"
    )
    conn.commit()
    txns = [_dated_txn("t1", desc="TESLA", amount=-0.5)]
    categories.apply_categories(conn, txns)
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_categorization_survives_overflowing_transaction_amount(tmp_path):
    # An over-large amount_float (float() raises OverflowError) must not crash
    # the pass; the amount-constrained rule fails closed for that transaction.
    conn = _conn(tmp_path)
    categories.add_rule(conn, "tesla", "Insurance", amount_min=200, amount_max=350)
    txns = [{"id": "big", "description": "TESLA", "payee": "", "amount_float": 10**400}]
    categories.apply_categories(conn, txns)  # must not raise
    assert txns[0]["category"] == categories.UNCATEGORIZED


def test_add_rule_rejects_nested_quantifier_regex(tmp_path):
    # A regex with a quantified group inside another quantifier can backtrack
    # catastrophically. It compiles fine, so it must be rejected explicitly at
    # creation time rather than silently hanging a later categorization pass.
    conn = _conn(tmp_path)
    for bad in ["(a+)+$", "(a*)*", "(.*)+", "([a-z]+)+x", "(.{1,9})+"]:
        with pytest.raises(ValueError):
            categories.add_rule(conn, bad, "Insurance", match_mode="regex")


def test_add_rule_rejects_overlong_regex(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "a" * 201, "Insurance", match_mode="regex")


def test_add_rule_accepts_safe_regex(tmp_path):
    # Legitimate merchant regexes (quantifiers, alternation, char classes, and a
    # quantified group whose body has no inner quantifier) are still accepted.
    conn = _conn(tmp_path)
    for good in [
        r"tesla.*insurance",
        r"^tesla, inc\. \d+ fremo",
        r"amazon|amzn",
        r"chase\s+card\s+\d{4}",
        r"[a-z]{2,4} #\d+",
        r"(amazon|amzn)\s+\d+",
        r"(abc)+",
    ]:
        rid = categories.add_rule(conn, good, "Insurance", match_mode="regex")
        assert rid > 0


def test_nested_quantifier_guard_does_not_block_substring_rules(tmp_path):
    # The safety check only applies to regex mode; a substring pattern that
    # happens to contain '(a+)+' is a literal and must still be accepted.
    conn = _conn(tmp_path)
    rid = categories.add_rule(conn, "(a+)+", "Misc", match_mode="substring")
    assert rid > 0


def test_add_rule_rejects_quantified_alternation_regex(tmp_path):
    # A quantified alternation group can backtrack catastrophically on overlap
    # (e.g. (a|a)+). It compiles fine, so reject it explicitly at creation time.
    conn = _conn(tmp_path)
    for bad in ["(a|a)+", "(x|x)+y", "^(a|aa)+$", "(a|ab)*", "(a|a){2,}"]:
        with pytest.raises(ValueError):
            categories.add_rule(conn, bad, "Insurance", match_mode="regex")


def test_add_rule_accepts_unquantified_alternation_regex(tmp_path):
    # A non-repeated alternation group is fine — only a quantified one is the
    # footgun.
    conn = _conn(tmp_path)
    rid = categories.add_rule(conn, r"(amazon|amzn)\s+\d+", "Shopping",
                              match_mode="regex")
    assert rid > 0


def test_add_rule_accepts_atomic_and_possessive_rewrites(tmp_path):
    # Atomic groups and possessive quantifiers do not backtrack, so they are the
    # canonical safe rewrite for an overlapping alternation/nesting. The guard
    # must accept them rather than block the very fix it asks the user to make.
    conn = _conn(tmp_path)
    for good in ["(?>amazon|amzn)+", "(?>a|aa)+", "(?:(?:a|a)++)+",
                 "(?>(a+))+", "amzn++"]:
        rid = categories.add_rule(conn, good, "Shopping", match_mode="regex")
        assert rid > 0


def test_add_rule_rejects_footgun_inside_atomic_group(tmp_path):
    # The atomic boundary only stops an *outer* quantifier from driving
    # backtracking; a nested footgun that backtracks within the group before it
    # commits is still catastrophic and must still be rejected.
    conn = _conn(tmp_path)
    for bad in ["(?>(a+)+b)", "(?>(a|a)+b)"]:
        with pytest.raises(ValueError):
            categories.add_rule(conn, bad, "Insurance", match_mode="regex")


def test_add_rule_rejects_many_unbounded_quantifiers(tmp_path):
    # Several sequential unbounded quantifiers over overlapping text backtrack
    # polynomially with degree equal to the quantifier count. Such a pattern
    # compiles fine and isn't a nested/alternation footgun, but stored it would
    # stall every categorization pass on a non-matching transaction string, so
    # it must be rejected at creation time.
    conn = _conn(tmp_path)
    for bad in [".*" * 7 + "x", ".*" * 90 + "x", r"\d+\d+\d+\d+\d+x",
                r"\w+\s+\w+\s+\w+\s+\d+"]:
        with pytest.raises(ValueError):
            categories.add_rule(conn, bad, "Insurance", match_mode="regex")


def test_add_rule_accepts_few_unbounded_quantifiers(tmp_path):
    # A handful of unbounded quantifiers is fine — realistic merchant patterns
    # use a few (an optional-whitespace span, a digit run, one '.*'). Only the
    # high-degree pileups are rejected.
    conn = _conn(tmp_path)
    for good in [".*" * 4 + "x", r"tesla.*insurance", r"chase\s+card\s+\d+",
                 r"(amazon|amzn)\s+\d+"]:
        rid = categories.add_rule(conn, good, "Shopping", match_mode="regex")
        assert rid > 0



def test_add_rule_rejects_overlong_regex_without_crashing(tmp_path):
    # A pathologically long/nested pattern must be rejected with a clean
    # ValueError before re.compile can raise RecursionError.
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "(" * 500, "Misc", match_mode="regex")


def test_add_rule_rejects_wrapped_backtracking_regex(tmp_path):
    # Wrapping the footgun in an extra group must not slip past the guard: the
    # backtracking structure is the same no matter how many groups enclose it.
    conn = _conn(tmp_path)
    for bad in ["((a|a))+$", "((a+))+", "((a|ab))*", "((.*))+", "(?:(a|a))+"]:
        with pytest.raises(ValueError):
            categories.add_rule(conn, bad, "Insurance", match_mode="regex")


def test_add_rule_rejects_oversized_repetition_count(tmp_path):
    # A short pattern with a repetition count above the regex engine's limit
    # makes re.compile raise OverflowError, not re.error; it must still surface
    # as a clean ValueError rather than crashing the rule-creation call.
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        categories.add_rule(conn, "a{4294967296}", "Insurance", match_mode="regex")

