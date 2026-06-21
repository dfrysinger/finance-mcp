"""Tests for the static account product-type map: seeding, precedence, and a
full end-to-end reconciliation pass against a real on-disk archive.
"""

from finance_mcp import archive, categories, typemap
from finance_mcp.matching import (
    CONF_KEYWORD,
    CONF_STRUCTURAL,
    CONF_UNCONFIRMED,
    PRODUCT_CHECKING,
    PRODUCT_SAVINGS,
    propose_transfer_links,
)


def _txn(tid, account, amount, date="2026-06-19", *, is_transfer=True, name=None, desc=None):
    return {
        "id": tid,
        "account_id": account,
        "account_name": name or account,
        "amount": amount,
        "posted": f"{date}T12:00:00+00:00",
        "is_transfer": is_transfer,
        "description": desc,
    }


# --- Pure suggestion logic -----------------------------------------------------


def test_structural_match_infers_counterparty_type():
    # A type-independent structural match reveals the credit account's type from
    # the debit's destination keyword.
    txns = [
        _txn("d", "Groceries", "-250.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c", "Main", "250.00", desc="Transfer from Schwab Bank"),
    ]
    suggestions = typemap.suggest_account_types(txns)
    assert suggestions["Main"] == (PRODUCT_CHECKING, "inferred")


def test_name_hint_seeds_heuristic_type():
    # No transfers at all — only the account name hints the type.
    txns = [_txn("x", "Emergency", "-1.00", name="Emergency Savings", is_transfer=False)]
    suggestions = typemap.suggest_account_types(txns)
    assert suggestions["Emergency"] == (PRODUCT_SAVINGS, "heuristic")


def test_inferred_overrides_name_hint():
    # Account named "...Checking" but a structural match proves it is Savings:
    # the inferred (high-trust) type wins over the name heuristic.
    txns = [
        _txn("d", "Groceries", "-30.00", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c", "Oddly", "30.00", name="Oddly Named Checking", desc="Transfer from Schwab Bank"),
    ]
    suggestions = typemap.suggest_account_types(txns)
    assert suggestions["Oddly"] == (PRODUCT_SAVINGS, "inferred")


def test_conflicting_inferred_types_are_left_unseeded():
    # Two structural matches imply different types for the same account (e.g. bad
    # data); rather than guess, the account is left for the user to decide — even
    # when its NAME carries a type hint that would otherwise seed a heuristic.
    txns = [
        _txn("d1", "A", "-10.00", date="2026-06-01", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c1", "Shared", "10.00", date="2026-06-01", name="Shared Checking Pool", desc="Transfer from Schwab Bank"),
        _txn("d2", "B", "-20.00", date="2026-06-02", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c2", "Shared", "20.00", date="2026-06-02", name="Shared Checking Pool", desc="Transfer from Schwab Bank"),
    ]
    suggestions = typemap.suggest_account_types(txns)
    assert "Shared" not in suggestions


# --- DB seeding precedence -----------------------------------------------------


def test_seed_writes_inferred_and_heuristic(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    txns = [
        _txn("d", "Groceries", "-250.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c", "Main", "250.00", name="Main Checking", desc="Transfer from Schwab Bank"),
        _txn("x", "Emergency", "-1.00", name="Emergency Savings", is_transfer=False),
    ]
    report = typemap.seed_account_types(conn, txns)
    stored = archive.load_account_types(conn)
    assert stored["Main"]["product_type"] == PRODUCT_CHECKING
    assert stored["Main"]["source"] == "inferred"
    assert stored["Emergency"]["product_type"] == PRODUCT_SAVINGS
    assert stored["Emergency"]["source"] == "heuristic"
    assert report["seeded"] == 2


def test_seed_never_overwrites_confirmed(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    typemap.confirm_account_type(conn, "Main", PRODUCT_SAVINGS)
    txns = [
        _txn("d", "Groceries", "-250.00", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c", "Main", "250.00", desc="Transfer from Schwab Bank"),
    ]
    report = typemap.seed_account_types(conn, txns)
    stored = archive.load_account_types(conn)
    # User-confirmed Savings survives even though the structural match implies Checking.
    assert stored["Main"]["product_type"] == PRODUCT_SAVINGS
    assert stored["Main"]["source"] == "confirmed"
    assert report["preserved"] == 1


def test_seed_does_not_downgrade_inferred_to_heuristic(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    archive.set_account_type(conn, "Main", PRODUCT_CHECKING, source="inferred")
    # A later seed where only the name hint is available must not overwrite the
    # stronger inferred type.
    txns = [_txn("x", "Main", "-1.00", name="Main Savings", is_transfer=False)]
    typemap.seed_account_types(conn, txns)
    stored = archive.load_account_types(conn)
    assert stored["Main"]["product_type"] == PRODUCT_CHECKING
    assert stored["Main"]["source"] == "inferred"


def test_seed_clears_stale_heuristic_when_evidence_turns_conflicting(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    # A prior run guessed a type from the account name.
    archive.set_account_type(conn, "Shared", PRODUCT_CHECKING, source="heuristic")
    # This run's structural matches imply CONFLICTING types for "Shared", so the
    # earlier guess is no longer trustworthy and must be cleared, not left to keep
    # feeding the matcher's guard and tie-breaker.
    txns = [
        _txn("d1", "A", "-10.00", date="2026-06-01", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c1", "Shared", "10.00", date="2026-06-01", name="Shared Checking Pool", desc="Transfer from Schwab Bank"),
        _txn("d2", "B", "-20.00", date="2026-06-02", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c2", "Shared", "20.00", date="2026-06-02", name="Shared Checking Pool", desc="Transfer from Schwab Bank"),
    ]
    report = typemap.seed_account_types(conn, txns)
    stored = archive.load_account_types(conn)
    assert "Shared" not in stored
    assert report["cleared"] == 1


def test_seed_keeps_confirmed_type_even_when_evidence_conflicts(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    # The user authoritatively confirmed the type; conflicting structural evidence
    # must NOT clear it.
    typemap.confirm_account_type(conn, "Shared", PRODUCT_CHECKING)
    txns = [
        _txn("d1", "A", "-10.00", date="2026-06-01", desc="Transfer to Schwab Bank Investor Checking"),
        _txn("c1", "Shared", "10.00", date="2026-06-01", desc="Transfer from Schwab Bank"),
        _txn("d2", "B", "-20.00", date="2026-06-02", desc="Transfer to Schwab Bank Investor Savings"),
        _txn("c2", "Shared", "20.00", date="2026-06-02", desc="Transfer from Schwab Bank"),
    ]
    report = typemap.seed_account_types(conn, txns)
    stored = archive.load_account_types(conn)
    assert stored["Shared"]["product_type"] == PRODUCT_CHECKING
    assert stored["Shared"]["source"] == "confirmed"
    assert report["cleared"] == 0


# --- End-to-end: raw feed -> archive -> categorize -> seed -> reconcile --------


def _schwab_account(account_id, name, transactions):
    return {
        "org": {"domain": "schwab.com", "name": "Charles Schwab"},
        "id": account_id,
        "name": name,
        "currency": "USD",
        "balance": "0.00",
        "available-balance": "0.00",
        "balance-date": 1718000000,
        "transactions": transactions,
    }


def _posted(day):
    # 2026-06-DD at noon UTC, as a unix timestamp the normalizer accepts.
    import calendar
    from datetime import datetime, timezone

    return calendar.timegm(
        datetime(2026, 6, day, 12, 0, 0, tzinfo=timezone.utc).timetuple()
    )


def test_end_to_end_keyword_reconciliation(tmp_path):
    """A realistic Schwab envelope scenario exercised through the whole stack.

    Two clean structural matches on earlier days seed the product types for
    Main (Checking) and Emergency (Savings). A later same-day collision of two
    $100 transfers — structurally ambiguous — is then resolved correctly using
    only that seeded type map. Nothing is mocked: data is normalized, written to
    a real SQLite archive, categorized, and reconciled.
    """
    from finance_mcp import normalize

    raw = {
        "errors": [],
        "accounts": [
            _schwab_account("ACT-groc", "Groceries", [
                {"id": "sd1", "posted": _posted(10), "amount": "-250.00",
                 "description": "Transfer to Schwab Bank Investor Checking", "payee": "Charles Schwab", "memo": "", "pending": False},
                {"id": "cd1", "posted": _posted(19), "amount": "-100.00",
                 "description": "Transfer to Schwab Bank Investor Checking", "payee": "Charles Schwab", "memo": "", "pending": False},
            ]),
            _schwab_account("ACT-house", "Housekeeping", [
                {"id": "sd2", "posted": _posted(11), "amount": "-310.00",
                 "description": "Transfer to Schwab Bank Investor Savings", "payee": "Charles Schwab", "memo": "", "pending": False},
                {"id": "cd2", "posted": _posted(19), "amount": "-100.00",
                 "description": "Transfer to Schwab Bank Investor Savings", "payee": "Charles Schwab", "memo": "", "pending": False},
            ]),
            _schwab_account("ACT-main", "Main Checking", [
                {"id": "sc1", "posted": _posted(10), "amount": "250.00",
                 "description": "Transfer from Schwab Bank", "payee": "Charles Schwab", "memo": "", "pending": False},
                {"id": "cc1", "posted": _posted(19), "amount": "100.00",
                 "description": "Transfer from Schwab Bank", "payee": "Charles Schwab", "memo": "", "pending": False},
            ]),
            _schwab_account("ACT-emerg", "Emergency Savings", [
                {"id": "sc2", "posted": _posted(11), "amount": "310.00",
                 "description": "Transfer from Schwab Bank", "payee": "Charles Schwab", "memo": "", "pending": False},
                {"id": "cc2", "posted": _posted(19), "amount": "100.00",
                 "description": "Transfer from Schwab Bank", "payee": "Charles Schwab", "memo": "", "pending": False},
            ]),
        ],
    }

    conn = archive.connect(tmp_path / "archive.db")
    archive.upsert(conn, normalize.normalize(raw))
    categories.seed_default_rules(conn)

    txns = archive.load_transactions(conn)
    categories.apply_categories(conn, txns)
    # Every transfer leg got flagged by the "transfer to"/"transfer from" rules.
    assert all(t["is_transfer"] for t in txns)

    # Seed the type map from structural matches + name hints, then reconcile.
    typemap.seed_account_types(conn, txns)
    type_map = archive.load_account_types(conn)
    assert type_map["ACT-main"]["product_type"] == PRODUCT_CHECKING
    assert type_map["ACT-emerg"]["product_type"] == PRODUCT_SAVINGS

    proposals = propose_transfer_links(txns, account_types=type_map)
    links = {
        (p.amount_cents, p.debit_account_name, p.credit_account_name): p
        for p in proposals
        if p.debit_txn_id and p.credit_txn_id
    }

    # The two seeding transfers resolved structurally.
    assert links[(25000, "Groceries", "Main Checking")].confidence == CONF_STRUCTURAL
    assert links[(31000, "Housekeeping", "Emergency Savings")].confidence == CONF_STRUCTURAL

    # The ambiguous $100 collision resolved by destination type, to the RIGHT
    # envelopes — Groceries funded Checking, Housekeeping funded Savings.
    groc_link = links[(10000, "Groceries", "Main Checking")]
    house_link = links[(10000, "Housekeeping", "Emergency Savings")]
    assert groc_link.confidence == CONF_KEYWORD
    assert groc_link.keyword == PRODUCT_CHECKING
    assert house_link.confidence == CONF_KEYWORD
    assert house_link.keyword == PRODUCT_SAVINGS

    # No $100 leg was left unconfirmed once the type map was applied.
    hundred = [p for p in proposals if p.amount_cents == 10000]
    assert all(p.confidence == CONF_KEYWORD for p in hundred)


def test_end_to_end_confirmed_type_guards_a_wrong_structural_match(tmp_path):
    """A user-confirmed type that contradicts the only structural pairing forces
    the transfer to be flagged for review instead of silently auto-linked."""
    from finance_mcp import normalize

    raw = {
        "errors": [],
        "accounts": [
            _schwab_account("ACT-groc", "Groceries", [
                {"id": "d", "posted": _posted(19), "amount": "-400.00",
                 "description": "Transfer to Schwab Bank Investor Checking", "payee": "Charles Schwab", "memo": "", "pending": False},
            ]),
            _schwab_account("ACT-main", "Main", [
                {"id": "c", "posted": _posted(19), "amount": "400.00",
                 "description": "Transfer from Schwab Bank", "payee": "Charles Schwab", "memo": "", "pending": False},
            ]),
        ],
    }
    conn = archive.connect(tmp_path / "archive.db")
    archive.upsert(conn, normalize.normalize(raw))
    categories.seed_default_rules(conn)
    txns = archive.load_transactions(conn)
    categories.apply_categories(conn, txns)

    # User declares Main is a Savings account, contradicting the debit's "to
    # Checking" keyword.
    typemap.confirm_account_type(conn, "ACT-main", PRODUCT_SAVINGS)
    type_map = archive.load_account_types(conn)

    proposals = propose_transfer_links(txns, account_types=type_map)
    assert all(p.debit_txn_id is None or p.credit_txn_id is None for p in proposals)
    assert all(p.confidence == CONF_UNCONFIRMED for p in proposals)
    assert any("conflicts with a known account type" in p.explanation for p in proposals)
