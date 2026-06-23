"""Tests for the red-flags detector: returned and missed debt payments."""

from datetime import date

from finance_mcp import redflags
from finance_mcp.budget_config import DebtAccount

AS_OF = date(2026, 6, 22)
START = date(2026, 1, 1)

LOAN = "ACT-loan"
OTHER = "ACT-other"


def _txn(account, amount, *, on, desc="Payment"):
    return {
        "id": f"{account}-{on}-{amount}",
        "account_id": account,
        "account_name": account,
        "amount": str(amount),
        "amount_float": float(amount),
        "posted": f"{on}T12:00:00+00:00",
        "description": desc,
        "payee": "",
        "is_transfer": False,
    }


def _detect(debts, txns, **kw):
    kw.setdefault("as_of", AS_OF)
    kw.setdefault("start", START)
    return redflags.detect_red_flags(debts, txns, **kw)


def _kinds(rep):
    return sorted(f["kind"] for f in rep["flags"])


# --- returned payments --------------------------------------------------------

def test_negative_posting_is_a_returned_payment():
    debts = (DebtAccount(LOAN, "Loan", 75224, 1),)
    txns = [
        _txn(LOAN, "752.24", on="2026-05-01"),
        _txn(LOAN, "-752.24", on="2026-05-06", desc="Reversal"),
        _txn(LOAN, "752.24", on="2026-05-20"),  # re-paid same month
    ]
    rep = _detect(debts, txns, as_of=date(2026, 6, 3))
    returned = [f for f in rep["flags"] if f["kind"] == "returned_payment"]
    assert len(returned) == 1
    assert returned[0]["severity"] == "red"
    assert returned[0]["date"] == "2026-05-06"
    assert returned[0]["actual"] == -752.24
    # Net for May is +752.24 (paid, returned, re-paid), so it is NOT missed even
    # though a return happened — the returned flag still fires independently.
    # As-of June 3 is before June's due-day-1 + grace, so the empty current month
    # is not yet evaluated.
    assert not any(f["kind"] == "missed_payment" for f in rep["flags"])


# --- missed months ------------------------------------------------------------

def test_payment_then_return_nets_zero_is_a_missed_month():
    debts = (DebtAccount(LOAN, "Loan", 75224, 1),)
    txns = [
        _txn(LOAN, "752.24", on="2026-05-01"),
        _txn(LOAN, "-752.24", on="2026-05-06"),
        _txn(LOAN, "752.24", on="2026-06-02"),
    ]
    rep = _detect(debts, txns)
    missed = [f for f in rep["flags"] if f["kind"] == "missed_payment"]
    assert len(missed) == 1
    assert missed[0]["month"] == "2026-05"
    assert missed[0]["actual"] == 0.0


def test_partial_payment_counts_as_paid():
    # Amount is never judged: a payment that posts for any amount counts as paid,
    # so an underpaid month is not a red flag — only a month with no payment is.
    debts = (DebtAccount(LOAN, "Loan", 75224, 1),)
    txns = [
        _txn(LOAN, "752.24", on="2026-04-01"),
        _txn(LOAN, "400.00", on="2026-05-01"),  # underpaid, but a payment posted
        _txn(LOAN, "752.24", on="2026-06-02"),
    ]
    rep = _detect(debts, txns)
    assert not any(f["severity"] == "red" for f in rep["flags"])


def test_full_payment_each_month_is_clean():
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns)
    assert rep["flags"] == []
    assert rep["summary"]["red"] == 0


def test_varying_payment_amounts_all_count_as_paid():
    # Each month receives a different amount; none is a missed payment because a
    # payment posted in every month.
    debts = (DebtAccount(LOAN, "Loan", 75224, 1),)
    txns = [
        _txn(LOAN, "752.24", on="2026-04-01"),
        _txn(LOAN, "100.00", on="2026-05-01"),
        _txn(LOAN, "900.00", on="2026-06-02"),
    ]
    rep = _detect(debts, txns)
    assert not any(f["severity"] == "red" for f in rep["flags"])


# --- current-month grace ------------------------------------------------------

def test_current_month_not_flagged_before_due_plus_grace():
    # As-of is the 10th; payment due on the 15th — not yet due, so no missed flag
    # for the current month even though nothing has posted this month.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
    ]
    rep = _detect(debts, txns, as_of=date(2026, 6, 10))
    assert not any(f["month"] == "2026-06" for f in rep["flags"])


def test_current_month_flagged_after_due_plus_grace():
    # As-of is the 25th, well past a due-day-15 + grace; June has no payment.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
    ]
    rep = _detect(debts, txns, as_of=date(2026, 6, 25))
    assert any(f["kind"] == "missed_payment" and f["month"] == "2026-06" for f in rep["flags"])


def test_payment_in_partial_boundary_month_is_not_missed():
    # When the window start falls mid-month, a payment earlier in that same month
    # must still count. The window is month-aligned so the boundary month's net is
    # whole; otherwise the early payment is excluded and the month falsely missed.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-01-05"),  # before a mid-January start
        _txn(LOAN, "500.00", on="2026-02-15"),
        _txn(LOAN, "500.00", on="2026-03-15"),
    ]
    rep = _detect(debts, txns, start=date(2026, 1, 17), as_of=date(2026, 3, 20))
    assert not any(f["month"] == "2026-01" for f in rep["flags"])


def test_month_end_due_date_respects_grace_into_next_month():
    # A month-end due date plus grace lands in the following month. The month must
    # not be flagged missed until that grace deadline passes, even though it is no
    # longer the current calendar month.
    debts = (DebtAccount(LOAN, "Loan", 50000, 31),)  # due the last day of month
    txns = [
        _txn(LOAN, "500.00", on="2026-04-30"),
        # nothing in May
    ]
    # May due 2026-05-31, +5 grace = 2026-06-05.
    before = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 6, 3))
    assert not any(f["month"] == "2026-05" for f in before["flags"])
    after = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 6, 6))
    assert any(
        f["month"] == "2026-05" and f["kind"] == "missed_payment"
        for f in after["flags"]
    )

def test_balance_only_account_is_unauditable_not_silent():
    debts = (DebtAccount(LOAN, "Loan", 50000, 1),)
    rep = _detect(debts, [])  # zero transactions on the loan
    assert len(rep["flags"]) == 1
    assert rep["flags"][0]["kind"] == "unauditable"
    assert rep["flags"][0]["severity"] == "info"
    assert rep["summary"]["unauditable"] == 1
    assert rep["summary"]["red"] == 0


def test_no_expected_amount_still_audits_missed_and_returned():
    # expected_amount is informational only — missed detection runs without it.
    debts = (DebtAccount(LOAN, "Loan", None, None),)
    txns = [
        _txn(LOAN, "752.24", on="2026-04-01"),
        _txn(LOAN, "-752.24", on="2026-05-06"),  # May nets negative
    ]
    rep = _detect(debts, txns)
    kinds = {f["kind"] for f in rep["flags"]}
    assert "returned_payment" in kinds
    assert "missed_payment" in kinds


def test_no_debt_accounts_is_empty_report():
    rep = _detect((), [_txn(OTHER, "10.00", on="2026-05-01")])
    assert rep["flags"] == []
    assert rep["debt_account_count"] == 0
    assert rep["summary"]["red"] == 0


def test_only_this_accounts_txns_are_considered():
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
        _txn(OTHER, "-9999.00", on="2026-05-01"),  # noise on another account
    ]
    rep = _detect(debts, txns)
    assert rep["flags"] == []


def test_months_before_first_activity_are_not_flagged():
    # First payment is April; Jan-Mar are before this loan existed in the data
    # and must not be reported as missed.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns, start=START, as_of=date(2026, 6, 20))
    assert not any(f["month"] in ("2026-01", "2026-02", "2026-03") for f in rep["flags"])


def test_red_flags_sort_red_before_info():
    debts = (
        DebtAccount("ACT-a", "A", 50000, 1),  # unauditable (no txns)
        DebtAccount("ACT-b", "B", 50000, 1),  # missed
    )
    txns = [
        _txn("ACT-b", "0.00", on="2026-05-01", desc="none"),
    ]
    # Give B a returned payment so there is at least one red flag.
    txns = [
        _txn("ACT-b", "500.00", on="2026-04-01"),
        _txn("ACT-b", "-500.00", on="2026-05-06"),
    ]
    rep = _detect(debts, txns)
    severities = [f["severity"] for f in rep["flags"]]
    assert severities == sorted(severities, key=lambda s: 0 if s == "red" else 1)
    assert severities[0] == "red"
    assert severities[-1] == "info"


# --- data-quality / robustness ------------------------------------------------

def test_non_finite_amount_is_dropped_not_crashed():
    # A non-finite amount ("inf") parses as a Decimal but must not reach int();
    # it is dropped and surfaced as a data-quality flag rather than raising.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "inf", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns)  # must not raise OverflowError
    dq = [f for f in rep["flags"] if f["kind"] == "data_quality"]
    assert len(dq) == 1
    assert dq[0]["severity"] == "info"
    assert rep["summary"]["data_quality"] == 1


def test_unparseable_row_is_surfaced_not_silently_ignored():
    # A debt row with an unreadable date could hide a returned/missed payment, so
    # it is counted and reported, never silently skipped.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        {**_txn(LOAN, "500.00", on="2026-05-15"), "posted": "not-a-date"},
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns)
    dq = [f for f in rep["flags"] if f["kind"] == "data_quality"]
    assert len(dq) == 1
    assert "1 transaction" in dq[0]["detail"]


def test_only_unparseable_in_window_rows_are_not_mislabeled_balance_only():
    # An account whose only in-window activity is unreadable rows did sync
    # transactions — it must surface as a data-quality issue, not as a
    # "balance-only, no transactions" unauditable note.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        {**_txn(LOAN, "500.00", on="2026-05-15"), "amount": "oops"},
    ]
    rep = _detect(debts, txns)
    kinds = {f["kind"] for f in rep["flags"]}
    assert "data_quality" in kinds
    assert "unauditable" not in kinds
    assert rep["summary"]["unauditable"] == 0
    # A month in which nothing posts to the loan is a missed payment.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
        # nothing in June
    ]
    rep = _detect(debts, txns, as_of=date(2026, 6, 25))
    missed = [f for f in rep["flags"] if f["kind"] == "missed_payment"]
    assert any(f["month"] == "2026-06" for f in missed)


def test_established_debt_silent_at_window_start_is_flagged():
    # The debt was active before the audit window opened, then went silent for the
    # first in-window months before resuming. Those silent in-window months must
    # be flagged: clamping to first activity (not first in-window payment) would
    # wrongly hide them.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2025-06-15"),  # before the window
        _txn(LOAN, "500.00", on="2026-04-15"),  # silent Jan-Mar, resumes April
        _txn(LOAN, "500.00", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 6, 20))
    missed_months = {f["month"] for f in rep["flags"] if f["kind"] == "missed_payment"}
    assert {"2026-01", "2026-02", "2026-03"} <= missed_months

# --- payment-schedule anchoring -----------------------------------------------

def test_fee_month_counts_as_a_payment():
    # A small non-payment fee still counts as a payment for its month — amount is
    # never compared — so neither the fee month nor any later month is flagged.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "25.00", on="2026-03-10", desc="Origination fee"),
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 6, 22))
    assert not any(f["severity"] == "red" for f in rep["flags"])


def test_lone_fee_before_first_due_date_is_not_flagged():
    # A brand-new loan whose only activity is a small fee, with the first payment
    # not yet due, must not be flagged: the fee counts as that month's payment and
    # the current month has not yet passed its due date plus grace.
    debts = (DebtAccount(LOAN, "Loan", 75000, 15),)  # expected $750, due the 15th
    txns = [
        _txn(LOAN, "50.00", on="2026-05-01", desc="Origination fee"),
    ]
    rep = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 6, 1))
    assert not any(f["severity"] == "red" for f in rep["flags"])


def test_established_debt_fully_silent_in_window_is_flagged_not_unauditable():
    # The debt paid before the window opened and then paid nothing during it. That
    # is a string of missed payments, not an unauditable balance-only account.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2025-06-15"),  # only payment, before the window
    ]
    rep = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 5, 20))
    kinds = {f["kind"] for f in rep["flags"]}
    assert "unauditable" not in kinds
    missed = {f["month"] for f in rep["flags"] if f["kind"] == "missed_payment"}
    assert {"2026-01", "2026-02", "2026-03", "2026-04", "2026-05"} <= missed


def test_malformed_row_before_window_is_not_counted_as_data_issue():
    # A malformed transaction dated before the audit window would never have been
    # audited even if well-formed, so it must not raise a data-quality flag.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        {**_txn(LOAN, "500.00", on="2024-02-15"), "amount": "oops"},  # pre-window
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "500.00", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns, start=date(2026, 1, 1), as_of=date(2026, 6, 22))
    assert not any(f["kind"] == "data_quality" for f in rep["flags"])


def test_huge_finite_amount_is_dropped_not_crashed():
    # A finite amount large enough to overflow the decimal context must be dropped
    # and surfaced, not raise decimal.Overflow out of the detector.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        _txn(LOAN, "500.00", on="2026-04-15"),
        _txn(LOAN, "1e999999999", on="2026-05-15"),
        _txn(LOAN, "500.00", on="2026-06-15"),
    ]
    rep = _detect(debts, txns)  # must not raise
    assert any(f["kind"] == "data_quality" for f in rep["flags"])


def test_pending_payment_does_not_satisfy_a_due_month():
    # A pending (uncleared) payment must not count as a cleared payment: the month
    # is still flagged missed because the money has not reached the loan.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    cleared = _txn(LOAN, "500.00", on="2026-04-15")
    pending = {**_txn(LOAN, "500.00", on="2026-05-15"), "pending": True}
    txns = [cleared, pending]
    rep = _detect(debts, txns, as_of=date(2026, 5, 25))
    missed = {f["month"] for f in rep["flags"] if f["kind"] == "missed_payment"}
    assert "2026-05" in missed


def test_cleared_payment_still_counts_when_pending_absent():
    # Sanity: rows without a pending key (or pending False) audit normally.
    debts = (DebtAccount(LOAN, "Loan", 50000, 15),)
    txns = [
        {**_txn(LOAN, "500.00", on="2026-04-15"), "pending": False},
        {**_txn(LOAN, "500.00", on="2026-05-15"), "pending": False},
        {**_txn(LOAN, "500.00", on="2026-06-15"), "pending": False},
    ]
    rep = _detect(debts, txns, as_of=date(2026, 6, 22))
    assert rep["flags"] == []


# --- funding-side auditing (payment_source) -----------------------------------

from finance_mcp.budget_config import PaymentSource  # noqa: E402

FUND = "ACT-checking"


def _out(amount, *, on, desc="MTG PMT LOAN PAYMT", account=FUND, payee=""):
    # A funding-account row: a payment is an outflow (negative amount).
    t = _txn(account, amount, on=on, desc=desc)
    t["payee"] = payee
    return t


def _mtg(account_id=None):
    return PaymentSource(description_contains=("MTG PMT",), account_id=account_id)


def test_by_source_outflows_count_as_paid_loan_account_ignored():
    # The loan account has no transactions; payments are outflows on checking.
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-04-01"),
        _out("-1000.00", on="2026-05-01"),
        _out("-1000.00", on="2026-06-01"),
        _txn(LOAN, "999.00", on="2026-05-15"),  # noise on the loan account, ignored
    ]
    rep = _detect((debt,), txns)
    assert rep["flags"] == []
    assert rep["summary"]["unauditable"] == 0


def test_by_source_missing_month_is_flagged():
    # No matching outflow in May -> May is a missed month.
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-04-01"),
        _out("-1000.00", on="2026-06-01"),
    ]
    rep = _detect((debt,), txns)
    missed = {f["month"] for f in rep["flags"] if f["kind"] == "missed_payment"}
    assert "2026-05" in missed
    detail = next(f for f in rep["flags"] if f["kind"] == "missed_payment")["detail"]
    assert "funding account" in detail


def test_by_source_inflow_is_a_returned_payment():
    # A matching inflow on the funding account is money coming back: a return.
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-04-01"),
        _out("1000.00", on="2026-04-09"),  # reversed/credited back
        _out("-1000.00", on="2026-05-01"),
        _out("-1000.00", on="2026-06-01"),
    ]
    rep = _detect((debt,), txns)
    returned = [f for f in rep["flags"] if f["kind"] == "returned_payment"]
    assert len(returned) == 1
    assert returned[0]["date"] == "2026-04-09"


def test_by_source_matches_payee_not_only_description():
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-04-01", desc="ACH DEBIT", payee="MTG PMT"),
        _out("-1000.00", on="2026-05-01", desc="ACH DEBIT", payee="MTG PMT"),
        _out("-1000.00", on="2026-06-01", desc="ACH DEBIT", payee="MTG PMT"),
    ]
    rep = _detect((debt,), txns)
    assert rep["flags"] == []


def test_by_source_account_id_scopes_the_search():
    # An identically-described outflow on a different account must not count.
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-04-01", account="ACT-elsewhere"),
        _out("-1000.00", on="2026-05-01", account="ACT-elsewhere"),
        _out("-1000.00", on="2026-06-01", account="ACT-elsewhere"),
    ]
    rep = _detect((debt,), txns)
    # No matching rows on the configured funding account -> unauditable, not silent.
    kinds = {f["kind"] for f in rep["flags"]}
    assert kinds == {"unauditable"}


def test_by_source_any_account_when_no_account_id():
    # With no account_id, a matching outflow on any account counts.
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(None))
    txns = [
        _out("-1000.00", on="2026-04-01", account="ACT-whatever"),
        _out("-1000.00", on="2026-05-01", account="ACT-another"),
        _out("-1000.00", on="2026-06-01", account="ACT-whatever"),
    ]
    rep = _detect((debt,), txns)
    assert rep["flags"] == []


def test_by_source_no_matches_is_unauditable_with_guidance():
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-05-01", desc="UNRELATED DEBIT"),
    ]
    rep = _detect((debt,), txns)
    assert _kinds(rep) == ["unauditable"]
    assert "payment_source" in rep["flags"][0]["detail"]


def test_by_source_any_account_still_ignores_the_loans_own_postings():
    # With no funding account_id, matching is across every *other* account, but the
    # loan's own postings are still ignored. A matching positive posting on the loan
    # account must NOT be sign-flipped into a phantom return / zeroed-out missed month.
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(None))
    txns = [
        _out("-1000.00", on="2026-04-01"),
        _out("-1000.00", on="2026-05-01"),
        _out("-1000.00", on="2026-06-01"),
        _txn(LOAN, "1000.00", on="2026-05-15", desc="MTG PMT POSTED"),  # on the loan
    ]
    rep = _detect((debt,), txns)
    assert rep["flags"] == []


def test_by_source_pending_outflow_does_not_count_as_paid():
    debt = DebtAccount(LOAN, "Loan", None, 1, payment_source=_mtg(FUND))
    txns = [
        _out("-1000.00", on="2026-04-01"),
        {**_out("-1000.00", on="2026-05-01"), "pending": True},  # not cleared
        _out("-1000.00", on="2026-06-01"),
    ]
    rep = _detect((debt,), txns)
    missed = {f["month"] for f in rep["flags"] if f["kind"] == "missed_payment"}
    assert "2026-05" in missed
