"""Red flags: loud alerts for returned or missed debt payments.

A debt account (loan, mortgage, financed purchase) is supposed to receive one
payment each cycle. Two things can quietly go wrong and never show up in normal
spend totals, because a payment that posts and is then reversed nets to zero:

* **Returned / reversed payment** — a payment posted to the loan account and was
  then clawed back (a negative-amount transaction on the loan account). The
  money never actually left; the debt was not paid.
* **Missed month** — a month whose payments net to zero or less, including the
  net-zero case a return produces and the case where nothing posted at all. The
  amount of a payment is never compared: any positive monthly net counts as paid.

These are detected directly from transactions on the debt account itself, so the
signal is unambiguous and source-account-independent: it does not matter which
checking account or envelope the payment was funded from. An account that syncs
only a balance (no transactions) cannot be audited this way; when the lender
exposes no payment ledger, a debt can instead opt into funding-side auditing
(``payment_source``): the same payment-and-return logic runs against matching
outflows on a funding account, with the sign reversed because there a payment is
money leaving and a return is money coming back. A debt with neither its own
postings nor a configured payment source is surfaced as an explicit "can't
audit" note rather than silently skipped.

The detector is a pure function over a :class:`~finance_mcp.budget_config.DebtAccount`
list and a transaction list; the report loader wires it to the durable archive.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .budget_config import BudgetConfig, DebtAccount

# How many days past a debt's nominal due day a month is given before its absent
# payment counts as missed. Applied to every audited month: a month is evaluated
# only once as_of has reached that month's due date plus this grace, so a payment
# that isn't due yet is never flagged early — including a month-end due date whose
# grace window extends into the following month.
DEFAULT_GRACE_DAYS = 5

# Default lookback when the caller gives no start: enough to cover a year of
# monthly cadence so a missed month is visible in context.
DEFAULT_WINDOW_DAYS = 370


def _posted_date(txn: dict) -> date | None:
    """Parse a transaction's posted date, tolerating date or datetime strings."""
    raw = txn.get("posted")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _amount_cents(txn: dict) -> int | None:
    """Exact integer cents from the transaction's decimal-string amount.

    The string form (``"752.41"``) is parsed via :class:`~decimal.Decimal` so
    summed payments never drift the way binary floats do. A value that is not a
    whole number of cents is treated as unparseable rather than rounded.
    """
    raw = txn.get("amount")
    if raw is None:
        return None
    try:
        cents = Decimal(str(raw)) * 100
        if not cents.is_finite():
            # Infinity and NaN parse cleanly and (for Infinity) equal their own
            # integral value, so they would slip past the whole-cents check below;
            # reject them here so a bad amount is dropped rather than reaching int().
            return None
        if cents != cents.to_integral_value():
            return None
        return int(cents)
    except (InvalidOperation, ValueError, ArithmeticError):
        # ArithmeticError covers decimal.Overflow raised by the multiply on a huge
        # finite amount (e.g. "1e999999999") and OverflowError from int(); an amount
        # we cannot represent is dropped, never crashes the audit.
        return None


def _dollars(cents: int) -> float:
    """Integer cents to a JSON-friendly float dollar amount."""
    return cents / 100.0


def _belongs_to_debt(txn: dict, debt: DebtAccount) -> bool:
    """Whether a transaction is one of this debt's payment-or-return rows.

    On-account mode (no ``payment_source``): the row must post to the debt's own
    account. Funding-side mode: the loan's own postings are always ignored; the row
    must sit on the configured funding account (or any *other* account when none is
    given) and its description or payee must contain one of the user's match
    substrings, compared case-insensitively.
    """
    src = debt.payment_source
    if src is None:
        return txn.get("account_id") == debt.account_id
    if txn.get("account_id") == debt.account_id:
        return False
    if src.account_id is not None and txn.get("account_id") != src.account_id:
        return False
    desc = (txn.get("description") or "").lower()
    payee = (txn.get("payee") or "").lower()
    return any(p.lower() in desc or p.lower() in payee for p in src.description_contains)


def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def _due_date(year: int, month: int, due_day: int) -> date:
    """The debt's due date in a given month, clamped to the month's length."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(due_day, last))


def detect_red_flags(
    debt_accounts: tuple[DebtAccount, ...] | list[DebtAccount],
    transactions: list[dict],
    *,
    as_of: date,
    start: date | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> dict:
    """Audit each configured debt account for returned and missed payments.

    Pure function: all evidence is supplied. ``transactions`` are categorized
    archive rows (each with ``account_id``, ``posted``, and a decimal-string
    ``amount``). ``as_of`` bounds the window's end; each audited month is only
    evaluated once ``as_of`` reaches that month's due date plus ``grace_days``.
    ``start`` bounds the earliest month considered (defaulting to a year before
    ``as_of``). The payment amount is never compared — any positive monthly net
    counts as paid, and a month whose postings net to zero or less (none posted,
    or a payment fully reversed by a return) is missed. Returns a
    JSON-serializable report.
    """
    start = start or (as_of - timedelta(days=DEFAULT_WINDOW_DAYS))
    # Audit whole months: align the window's lower bound to the first of its month
    # so the in-window row filter and the month-by-month walk agree. Without this,
    # a payment earlier in a partial boundary month would be excluded from that
    # month's net and the month falsely flagged missed.
    start = start.replace(day=1)
    flags: list[dict] = []

    for debt in debt_accounts:
        src = debt.payment_source
        rows = []
        all_rows: list[tuple[date, int]] = []
        dropped = 0
        for txn in transactions:
            if not _belongs_to_debt(txn, debt):
                continue
            if txn.get("pending"):
                # A pending posting has not actually cleared yet, so it cannot
                # satisfy (or fail) a cleared-payment audit. Skip it for
                # everything — counting it as paid would hide a payment that never
                # clears, the exact failure this report exists to surface.
                continue
            d = _posted_date(txn)
            cents = _amount_cents(txn)
            if d is None or cents is None:
                # A matched row we cannot parse is not silently ignored: it is
                # counted and surfaced as a data-quality flag below, since a dropped
                # row could be a real returned or missed payment. Only count rows
                # that could fall inside the audit window — a malformed row with a
                # date before the window would never have been audited anyway, and
                # an unparseable date is treated as possibly-in-window.
                if d is None or start <= d <= as_of:
                    dropped += 1
                continue
            if src is not None:
                # Funding-side: a payment leaves the funding account (a negative
                # outflow) and a return comes back in (a positive inflow). Flip the
                # sign so the rest of the detector reuses its on-account convention
                # unchanged, where a payment is positive and a return is negative.
                cents = -cents
            if d > as_of:
                continue
            # Keep every parseable posting up to as_of (ignoring the window's lower
            # bound) so the payment-schedule origin can be found even when it
            # predates the window; rows is the in-window subset used for auditing.
            all_rows.append((d, cents))
            if d < start:
                continue
            rows.append((d, cents, txn))

        if dropped:
            flags.append(
                {
                    "kind": "data_quality",
                    "kind_label": "Data issue",
                    "severity": "info",
                    "account_id": debt.account_id,
                    "account_label": debt.label,
                    "date": None,
                    "month": None,
                    "expected": None,
                    "actual": None,
                    "shortfall": None,
                    "detail": (
                        f"{dropped} transaction(s) on this debt had an unreadable "
                        "date or amount and could not be audited; a returned or "
                        "missed payment among them would not be caught."
                    ),
                }
            )

        expected = debt.expected_amount_cents

        # The schedule's first observed month: the earliest month with any incoming
        # payment, looking across all available history so a payment predating the
        # window still anchors an established debt. Any positive posting counts as a
        # payment — amount is never compared, so an origination/setup fee anchors
        # the schedule exactly as a full payment would.
        payment_months = sorted({_month_key(d) for d, c in all_rows if c > 0})
        first_payment_month: tuple[int, int] | None = (
            payment_months[0] if payment_months else None
        )

        # Unauditable only when the feed carried no in-window activity at all AND no
        # payment was ever seen to anchor a schedule — a balance-only debt. Rows we
        # could not parse still count as activity (already surfaced as data_quality
        # above), so an account with only unreadable in-window rows is not mislabeled
        # balance-only. If a real payment was observed (even before the window), fall
        # through so the silent in-window months are flagged missed, not hidden.
        if not rows and not dropped and first_payment_month is None:
            flags.append(
                {
                    "kind": "unauditable",
                    "kind_label": "Can't audit",
                    "severity": "info",
                    "account_id": debt.account_id,
                    "account_label": debt.label,
                    "date": None,
                    "month": None,
                    "expected": (
                        None
                        if expected is None
                        else _dollars(expected)
                    ),
                    "actual": None,
                    "shortfall": None,
                    "detail": (
                        "No matching payments were found on the funding "
                        "account, so this debt's payments can't be verified "
                        "here — check its payment_source match text."
                        if src is not None
                        else (
                            "This debt syncs a balance but no transactions, so "
                            "its payments can't be verified here."
                        )
                    ),
                }
            )
            continue

        # Returned / reversed payments: any negative posting to the loan account.
        for d, cents, txn in rows:
            if cents < 0:
                desc = (txn.get("description") or txn.get("payee") or "").strip()
                flags.append(
                    {
                        "kind": "returned_payment",
                        "kind_label": "Returned",
                        "severity": "red",
                        "account_id": debt.account_id,
                        "account_label": debt.label,
                        "date": d.isoformat(),
                        "month": f"{d.year:04d}-{d.month:02d}",
                        "expected": None,
                        "actual": _dollars(cents),
                        "shortfall": None,
                        "detail": (
                            f"A payment of {_dollars(-cents):.2f} was returned or "
                            f"reversed on {d.isoformat()}"
                            + (f" ({desc})." if desc else ".")
                        ),
                    }
                )

        # A missed month needs an observed payment to anchor the schedule's start.
        # Without one there is nothing to miss against (a balance-only debt is
        # surfaced as unauditable above).
        if first_payment_month is None:
            continue

        net_by_month: dict[tuple[int, int], int] = {}
        for d, cents, _ in rows:
            net_by_month[_month_key(d)] = net_by_month.get(_month_key(d), 0) + cents

        # Audit from the later of the window start and the month the debt's first
        # payment was observed. Clamping to the window start (when the first payment
        # predates the window) means an established debt that fell silent at the
        # start of the window still has those silent months flagged; clamping to the
        # first payment (when it falls in-window) means a debt whose schedule begins
        # mid-window is not flagged for the months before its first payment.
        start_month = (start.year, start.month)
        first_month = max(start_month, first_payment_month)

        # Walk every month from the audit origin through the as-of month.
        y, m = first_month
        while (y, m) <= (as_of.year, as_of.month):
            due_day = debt.due_day or calendar.monthrange(y, m)[1]
            if as_of < _due_date(y, m, due_day) + timedelta(days=grace_days):
                # This month's payment isn't overdue yet — its due date plus grace
                # has not passed. Applies to any month, not just the current one,
                # because a month-end due date plus grace can land in the next
                # month, so the prior month must not be flagged the instant it ends.
                m, y = (1, y + 1) if m == 12 else (m + 1, y)
                continue
            net = net_by_month.get((y, m), 0)
            # A payment of any amount counts as paid; only a non-positive net is a
            # missed month — covering both no payment at all and a payment fully
            # reversed by a return.
            if net <= 0:
                flags.append(
                    {
                        "kind": "missed_payment",
                        "kind_label": "Missed",
                        "severity": "red",
                        "account_id": debt.account_id,
                        "account_label": debt.label,
                        "date": None,
                        "month": f"{y:04d}-{m:02d}",
                        "expected": None if expected is None else _dollars(expected),
                        "actual": _dollars(net),
                        "shortfall": None,
                        "detail": (
                            f"{y:04d}-{m:02d}: no payment left the funding "
                            "account (none sent, or it was returned)."
                            if src is not None
                            else (
                                f"{y:04d}-{m:02d}: no payment reached the loan "
                                "(none posted, or it was returned)."
                            )
                        ),
                    }
                )
            m, y = (1, y + 1) if m == 12 else (m + 1, y)

    # Most recent first: sort by the flag's effective date (returned uses the
    # exact date; month flags use the month's end), red before info.
    def _sort_key(f: dict):
        when = f.get("date") or (f.get("month") or "")
        return (0 if f["severity"] == "red" else 1, when)

    flags.sort(key=_sort_key, reverse=True)
    # reverse=True flips the severity tie-break too; restore red-before-info.
    flags.sort(key=lambda f: 0 if f["severity"] == "red" else 1)

    summary = {
        "returned": sum(1 for f in flags if f["kind"] == "returned_payment"),
        "missed": sum(1 for f in flags if f["kind"] == "missed_payment"),
        "unauditable": sum(1 for f in flags if f["kind"] == "unauditable"),
        "data_quality": sum(1 for f in flags if f["kind"] == "data_quality"),
        "red": sum(1 for f in flags if f["severity"] == "red"),
    }
    return {
        "ok": True,
        "as_of": as_of.isoformat(),
        "start": start.isoformat(),
        "grace_days": grace_days,
        "debt_account_count": len(debt_accounts),
        "summary": summary,
        "flags": flags,
    }


def red_flags_report(
    config: BudgetConfig,
    *,
    as_of: date | None = None,
    start: date | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> dict:
    """Load the categorized archive, then audit debt accounts for trouble.

    ``as_of`` defaults to today and ``start`` to a year before it. The payment
    amount is never compared, so only returned and missed payments are flagged.
    """
    from . import store

    as_of = as_of or date.today()
    view = store.load_archive_view()
    return detect_red_flags(
        config.debt_accounts,
        view["transactions"],
        as_of=as_of,
        start=start,
        grace_days=grace_days,
    )
