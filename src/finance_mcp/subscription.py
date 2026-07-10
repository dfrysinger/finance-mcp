"""Subscription audit: did each tracked recurring charge post, and what
recurring-looking merchants aren't tracked yet?

Two scripted outputs the assistant reasons over:

* **tracked** (deterministic) — the full roster of configured ``recurring``
  bills, one entry each, carrying the expected amount, nominal day-of-month, the
  date last seen, the next due date, and a status (``active``/``overdue``/
  ``unseen``). This is the complete subscription list, independent of whether any
  given bill posted this window.
* **expected_missing** (deterministic) — a tracked recurring charge (a budget
  ``recurring`` bill, optionally pinned to its merchant via the bill's ``match``
  keyword) whose monthly occurrence in the window has no matching debit. Because
  we know the expected merchant, amount, and day, this alert never depends on a
  model. An occurrence within ``grace_days`` of the window end is *not* flagged
  (it may still post), so the audit never cries wolf on a charge that simply
  isn't due yet.
* **candidate_new** (script surfaces, assistant judges) — merchants with
  repeated, same-amount, regularly-spaced debits that are *not* in the tracked
  set. The script emits a structured, auditable candidate list; deciding whether
  each is really a new subscription is left to the assistant.
* **came_back** (deterministic) — tracked bills the user marked ``canceling`` or
  ``canceled`` (via the bill's ``lifecycle``/``cancel_effective``) that
  nonetheless charged again on or after the cancellation date. A canceled bill's
  *absence* is suppressed from ``expected_missing`` (it is the desired outcome),
  so the only signal a cancellation raises is a charge coming back.

A tracked bill without a ``match`` keyword is matched by its envelope->account
binding instead: a debit on one of the envelope's accounts, for the expected
amount, near the expected day. Pinning the merchant with a ``match`` keyword is
strictly more reliable (it survives the charge landing on a different card), so
it is recommended for anything you want a dependable missing-charge alert on.

Money is carried in integer cents throughout and rendered to dollars only at the
report edge. Candidate charges are grouped by merchant identity and then split
into near-equal-amount clusters (within a small absolute-or-percentage
tolerance), so a stable price that wobbles by a cent or two — sales-tax rounding,
foreign-currency conversion, metered usage — stays one stream instead of
fragmenting below the occurrence threshold. Two genuinely different prices at the
same merchant (e.g. a $21.49 and a $214.90 plan) still separate into distinct
streams.
"""

from __future__ import annotations

import contextlib
import math
import threading
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .budget_config import BudgetConfig, Envelope, RecurringBill, monthly_dates
from .normalize import amount_to_cents

DEFAULT_WINDOW_DAYS = 365
DEFAULT_DAY_TOLERANCE = 7
DEFAULT_MIN_OCCURRENCES = 3
# Minimum status look-back: how far before the audit window start a tracked
# bill's *status* may look back to find its most recent should-have-posted
# occurrence. The effective look-back is max(this, grace + 31) — a monthly bill's
# latest occurrence on or before ``end - grace`` sits at most ~31 days earlier, so
# the window must span grace + 31 days to always generate it; this two-month floor
# covers the common small-grace case. The look-back only *generates* occurrences
# (so a late in-tolerance charge can still match); an unmatched occurrence is
# judged overdue only when the data actually covers its full payment window, so
# the look-back can never invent an "overdue" for a date with no evidence either
# way.
_STATUS_LOOKBACK_DAYS = 62

# Upper bound for the day-count tolerances (``day_tolerance`` and ``grace_days``).
# Both are public, server- and CLI-exposed parameters that widen the status
# look-back (``max(_STATUS_LOOKBACK_DAYS, grace + 31)`` days). Without a cap a
# large value would expand ``monthly_dates`` to tens of thousands of occurrences
# per bill; a monthly bill never needs a tolerance or grace beyond a single
# cycle, so a one-year ceiling is generous. (Pathological boundary *dates* — an
# ``end``/``start`` at date.min/date.max — are handled separately by the
# ``_shift_days`` clamp, not by this cap.)
_MAX_TOLERANCE_DAYS = 366

# Median-spacing bands (in days) used to label a candidate's cadence. A group
# whose median gap falls outside every band is treated as irregular, not a
# subscription, and is dropped from the candidate list. Bands follow the cadence
# taxonomy documented by Plaid's recurring-transactions product (weekly ~7,
# biweekly ~14, monthly ~30, annual ~365); quarterly (~91) is added for bills
# like some insurance/software plans. Monthly is widened to 24-38 days so a
# charge still groups across 28-to-31-day month-length drift plus a few days of
# weekend/holiday posting shift.
_CADENCE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 5, 9),
    ("biweekly", 12, 16),
    ("monthly", 24, 38),
    ("quarterly", 78, 104),
    ("yearly", 350, 380),
)
_CADENCE_ORDER = {"weekly": 0, "biweekly": 1, "monthly": 2, "quarterly": 3, "yearly": 4}

# The cadence bands the detector used before amount-tolerant clustering was
# added. Phase 1 of candidate detection classifies exact-price streams with
# THESE bands (not the broadened ones above) so that locking a stream in phase 1
# reproduces the pre-broadening behavior exactly: the wider monthly window and
# the new biweekly/quarterly bands admit a stream only through phase 2, which is
# guarded by the interval-regularity (CV) check. Widening what phase 1 accepts
# would let an erratic exact-price run (e.g. two charges a day apart then one
# months later, median coincidentally ~monthly) surface with no regularity check.
_LEGACY_CADENCE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 5, 9),
    ("monthly", 24, 35),
    ("yearly", 350, 380),
)

# Amount tolerance for grouping near-identical charges into one recurring stream.
# A stable price is the subscription signal, but real charges wobble by a cent or
# two (sales-tax rounding, foreign-currency conversion, metered usage) — so
# grouping on EXACT cents splits e.g. a $26.85 / $26.86 / $26.85 monthly charge
# into sub-threshold fragments and misses it entirely. Charges within
# ``max(flat, pct * cluster median)`` are treated as one stream. The
# max(absolute, percentage) shape follows the open-source bank2excel-finance
# detector; the percentage is tightened to a subscription-appropriate 5% (that
# detector uses 20%, which is aimed at variable utility bills).
_CANDIDATE_AMOUNT_TOL_CENTS = 50
_CANDIDATE_AMOUNT_TOL_PCT = 0.05

# Maximum coefficient of variation (stddev / mean) of the inter-charge gaps for
# an amount-clustered candidate to still count as a regular cadence. Widening the
# amount tolerance admits more clusters, so this regularity guard keeps a merely
# habitual merchant (one hit ~monthly by coincidence, with erratic spacing) from
# being mislabeled a subscription. A clean monthly stream sits near CV 0.05-0.15
# even with a skipped cycle; 0.40 is a lenient upper bound. (Author heuristic —
# see docs/roadmap.md; no published PFM-specific cutoff exists.)
_MAX_INTERVAL_CV = 0.40

# Occurrences required to call a stream recurring. Plaid matures most cadences at
# 3 occurrences but annual streams at 2 (a year of history rarely shows 3), so a
# yearly-spaced pair is enough to surface for review.
_ANNUAL_MIN_OCCURRENCES = 2

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS_ONLY = re.compile(r"^\d+$")


def _shift_days(d: date, days: int) -> date:
    """Shift ``d`` by ``days`` (which may be negative), clamping to the
    representable date range instead of raising ``OverflowError``.

    The audit's internal windows only ever widen *past* real transaction data
    (a leading-edge look-back, or a short trailing projection for ``next_due``),
    so clamping a pathological boundary ``start``/``end`` (e.g. ``date.min`` or
    ``date.max``, reachable via the CLI/MCP date parsers) to the representable
    edge yields a correct result while keeping the public surface from raising
    an opaque ``OverflowError``.
    """
    try:
        return d + timedelta(days=days)
    except OverflowError:
        return date.max if days > 0 else date.min


def default_start(end: date) -> date:
    """Default detection/report window start: ``DEFAULT_WINDOW_DAYS`` before
    ``end``, clamped to the representable date range.

    The detect and report surfaces widen an omitted ``start`` a year back from
    ``end``. Doing that with raw ``date`` arithmetic underflows and raises an
    opaque ``OverflowError`` when ``end`` is at/near ``date.min`` (reachable via
    the CLI/MCP ISO date parsers). Routing every default-start computation
    through this one clamped helper keeps a boundary ``end`` from crashing the
    public surface.
    """
    return _shift_days(end, -DEFAULT_WINDOW_DAYS)


@dataclass(frozen=True)
class _Charge:
    """One observed debit (a candidate subscription payment)."""

    on: date
    amount_cents: int  # positive magnitude of the outflow
    merchant_raw: str  # primary display string (may be "" when none is present)
    merchant_key: str  # normalized grouping key ("" when ungroupable)
    match_tokens: frozenset[str]  # normalized merchant-identity tokens (desc+payee)
    envelope: str | None
    tid: str


def _env_name(account_index: dict[str, Envelope], account_id: object) -> str | None:
    env = account_index.get(account_id) if account_id is not None else None
    return env.name if env is not None else None


def _txn_date(txn: dict) -> date | None:
    posted = txn.get("posted")
    if isinstance(posted, str) and len(posted) >= 10:
        try:
            return date.fromisoformat(posted[:10])
        except ValueError:
            return None
    return None


def _merchant_strings(txn: dict) -> tuple[str, str]:
    """Return ``(display, identity)`` for a transaction.

    ``identity`` is the merchant-bearing text used for both keyword matching and
    candidate grouping: description and payee joined and lowercased, falling back
    to memo only when both are empty. ``memo`` is a catch-all display column
    (transaction type, check number, cardholder, …) — not a reliable merchant
    source — so it is deliberately kept OUT of the matching identity unless it is
    the only text present; otherwise a tracked brand that incidentally appears in
    an unrelated charge's memo could satisfy a bill and hide a genuinely-missing
    charge. ``account_name`` is likewise *not* used — a blank-merchant debit must
    not be grouped under its card's name.

    ``display`` is the human-readable label: the first field that carries a real
    merchant token (so an all-numeric description like a reference number does
    not become the label when the payee names the merchant), falling back to the
    first non-empty field, then "".
    """
    raw_fields: list[str] = []
    display = ""
    for key in ("description", "payee", "memo"):
        value = txn.get(key)
        if isinstance(value, str) and value.strip():
            stripped = value.strip()
            raw_fields.append(stripped)
            if not display and _merchant_key(stripped):
                display = stripped
    if not display and raw_fields:
        display = raw_fields[0]

    identity_parts: list[str] = []
    for key in ("description", "payee"):
        value = txn.get(key)
        if isinstance(value, str) and value.strip():
            identity_parts.append(value.strip())
    if not identity_parts:
        memo = txn.get("memo")
        if isinstance(memo, str) and memo.strip():
            identity_parts.append(memo.strip())
    identity = " ".join(identity_parts).lower()
    return display, identity


def _merchant_key(text: str) -> str:
    """Normalize merchant text into a grouping key.

    Lowercases, splits on any run of non-alphanumerics, drops purely-numeric
    tokens (store ids, dates, auth codes that vary between charges of the same
    merchant), and rejoins with single spaces. Conservative on purpose: it
    groups ``SQ *COFFEE 1234`` with ``SQ *COFFEE 5678`` without collapsing
    genuinely different merchants together.
    """
    tokens = [t for t in _NON_ALNUM.split(text.lower()) if t and not _DIGITS_ONLY.match(t)]
    return " ".join(tokens)


def _charges(
    transactions: list[dict],
    account_index: dict[str, Envelope],
    *,
    start: date,
    end: date,
) -> list[_Charge]:
    """Normalize in-window spendable debits into ``_Charge`` records.

    Transfers (``is_transfer``) and credits are not subscription payments and are
    dropped, as are debits with no parseable date or one posted outside the
    audit window — restricting to the window keeps the report fully described by
    its ``start``/``end`` rather than by whatever multi-year history the archive
    happens to hold. A debit with no merchant text is *kept* (so an envelope-only
    bill can still match it) but carries an empty ``merchant_key`` and is skipped
    by candidate grouping, which has no merchant to surface.
    """
    out: list[_Charge] = []
    for txn in transactions:
        if txn.get("is_transfer"):
            continue
        tid = txn.get("id")
        if tid is None:
            continue
        cents = amount_to_cents(txn.get("amount"))
        if cents is None or cents >= 0:
            continue  # only outflows (debits) can be subscription payments
        on = _txn_date(txn)
        if on is None or on < start or on > end:
            continue
        display, identity = _merchant_strings(txn)
        # Grouping/labeling key uses the stable first-non-empty display string, so
        # a price-stable merchant is never split into sub-threshold groups merely
        # because an auxiliary field (payee) is populated on only some rows.
        # Matching/suppression tokens use the fuller merchant identity (description
        # + payee), so a keyword living in the payee still pins and suppresses.
        out.append(
            _Charge(
                on=on,
                amount_cents=-cents,  # store the positive magnitude
                merchant_raw=display,
                merchant_key=_merchant_key(display),
                match_tokens=frozenset(_merchant_key(identity).split()),
                envelope=_env_name(account_index, txn.get("account_id")),
                tid=str(tid),
            )
        )
    # Deterministic order: by date, then amount, then id. The greedy matcher and
    # the candidate grouping both depend on this being stable.
    out.sort(key=lambda c: (c.on, c.amount_cents, c.tid))
    return out


def _identity_matches_bill(
    charge: _Charge,
    bill: RecurringBill,
    match_tokens: frozenset[str] | None,
) -> bool:
    """Does this charge belong to this bill's merchant/account, ignoring amount?

    The identity half of :func:`_matches_bill` with the amount comparison
    dropped. Used by the cancellation watch: a subscription "coming back" is any
    charge from the same merchant (keyword) or envelope after the cancellation
    date, even if it posts off the old schedule or at a different price — a
    final/partial charge or a re-bill at a new amount must still raise the alert.
    """
    if match_tokens is not None:
        return bool(match_tokens) and match_tokens <= charge.match_tokens
    if bill.envelope is None:
        return False
    return charge.envelope == bill.envelope


def _matches_bill(
    charge: _Charge,
    bill: RecurringBill,
    *,
    amount_tolerance_cents: int,
    amount_tolerance_pct: float,
    match_tokens: frozenset[str] | None,
) -> bool:
    # A variable-amount bill (metered insurance, escrow-adjusted mortgage) is
    # matched by merchant/envelope and due date alone — its amount legitimately
    # swings every cycle, so gating on amount would read a normal price change as
    # a missing bill. Fixed bills still gate on amount, within the larger of the
    # flat tolerance and the global percentage drift allowance (the percentage is
    # amount-proportional so one setting fits a $5 bill and a $3,400 mortgage).
    if not bill.variable:
        tolerance = amount_tolerance_cents
        if amount_tolerance_pct > 0:
            tolerance = max(
                tolerance, round(amount_tolerance_pct * bill.amount_cents)
            )
        if abs(charge.amount_cents - bill.amount_cents) > tolerance:
            return False
    if match_tokens is not None:
        # Keyword match: the bill's normalized tokens must all appear among the
        # charge's merchant-identity tokens (description + payee, the reliable
        # merchant fields — memo is excluded so an incidental brand in a catch-all
        # note can't satisfy the bill). Anchored to tokens rather than a raw
        # substring so a short keyword like "ATT" cannot be satisfied by an
        # unrelated "BATTERY WORLD" charge — either false match would hide a
        # genuinely-missing bill. An empty token set (a keyword that normalized
        # away) matches nothing rather than everything.
        return bool(match_tokens) and match_tokens <= charge.match_tokens
    # No merchant keyword: fall back to the envelope the charge landed in. A
    # bill with no envelope and no keyword cannot match anything (parse rejects
    # that combination), and a charge on no envelope (None) must not match a
    # bill whose envelope is likewise None.
    if bill.envelope is None:
        return False
    return charge.envelope == bill.envelope


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _cadence_label(
    median_days: float,
    bands: tuple[tuple[str, int, int], ...] = _CADENCE_BANDS,
) -> str:
    for name, lo, hi in bands:
        if lo <= median_days <= hi:
            return name
    return "irregular"


def _dollars(cents: int) -> str:
    """Render integer cents as a fixed two-decimal dollar string."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def _bill_token_sets(
    config: BudgetConfig,
) -> tuple[list[frozenset[str] | None], list[frozenset[str]], list[int]]:
    """Per-bill keyword token sets, plus the keyword sets and amounts for review.

    Returns ``(bill_token_sets, tracked_token_sets, tracked_amounts)``.
    ``bill_token_sets`` is aligned with ``config.recurring`` and holds ``None``
    for an envelope-only bill (no ``match`` keyword). ``tracked_token_sets`` is
    the non-empty keyword sets, reused to keep a tracked subscription from
    resurfacing as a "new" candidate. ``tracked_amounts`` is the parallel list of
    those bills' amounts (in cents), used only to tell apart a charge that is the
    *same* tracked subscription from one that shares the keyword at a *different*
    price (a price change or a separate plan) — the latter is surfaced for review
    rather than silently dropped. Suppression itself stays amount-blind: a
    keyword-tracked merchant whose price changed must not be auto-written as a
    second bill competing with the first. Token-anchored (not raw substring) so a
    short keyword like "ATT" neither satisfies nor suppresses an unrelated
    "BATTERY WORLD" charge.
    """
    bill_token_sets: list[frozenset[str] | None] = []
    tracked_token_sets: list[frozenset[str]] = []
    tracked_amounts: list[int] = []
    for bill in config.recurring:
        tokens = (
            frozenset(_merchant_key(bill.match).split())
            if bill.match is not None
            else None
        )
        bill_token_sets.append(tokens)
        if tokens:
            tracked_token_sets.append(tokens)
            tracked_amounts.append(bill.amount_cents)
    return bill_token_sets, tracked_token_sets, tracked_amounts


def _match_tracked_bills(
    config: BudgetConfig,
    charges: list[_Charge],
    bill_token_sets: list[frozenset[str] | None],
    *,
    start: date,
    window_start: date,
    end: date,
    earliest_txn: date | None,
    day_tolerance: int,
    amount_tolerance_cents: int,
    amount_tolerance_pct: float = 0.0,
    grace: int,
) -> tuple[set[str], list[dict], list[dict]]:
    """Greedy-match each recurring bill's monthly occurrences against charges.

    Returns ``(consumed, expected_missing, tracked)``. A charge is *consumed* by
    the closest unconsumed in-tolerance occurrence (exact amount preferred, then
    least date drift). An occurrence with no match is reported missing only once
    it is genuinely overdue (outside the ``grace`` window). ``consumed`` does not
    depend on ``grace`` — it is the set of charges already covered by a tracked
    bill, which both the audit and detect use to avoid re-surfacing them.

    Occurrences are expanded across ``[start, end]`` (``start`` may reach before
    ``window_start`` so a narrow window can still see this bill's most recent due
    date), but a missing occurrence is only reported in ``expected_missing`` when
    it falls on or after ``window_start`` — the requested audit window. ``tracked``
    status, by contrast, is judged across the full ``[start, end]`` span so it can
    tell active from overdue regardless of how narrow ``window_start`` is.

    ``tracked`` is the full roster of configured recurring bills — one entry per
    bill regardless of whether it posted this window — each carrying its expected
    amount, nominal day-of-month, the date it was last seen, its next due date,
    and a status reflecting whether it is *currently* posting on schedule:
    ``active`` when the most recent due charge posted, ``overdue`` when the most
    recent occurrence that is past the grace window did not post, and ``unseen``
    when no charge has matched and nothing is overdue yet. An occurrence only
    counts toward overdue when ``earliest_txn`` shows we hold data covering its
    full in-tolerance payment window (``occ - day_tolerance``); otherwise an early
    payment could be unobservable, so the bill stays ``unseen`` rather than being
    falsely flagged overdue. Early-window gaps that predate the latest seen charge
    are historical noise and do not, on their own, mark a subscription overdue.
    """
    consumed: set[str] = set()
    expected_missing: list[dict] = []
    tracked: list[dict] = []
    for bill, bill_tokens in zip(config.recurring, bill_token_sets):
        last_seen: date | None = None
        # Posting dates of every charge matched to this bill, in scan order. A
        # missing row derives its "last seen" from this list (the most recent
        # match strictly before that occurrence) rather than from the running
        # ``last_seen`` above: greedy earliest-first matching can assign a charge
        # to an EARLIER occurrence than the one it best fits (e.g. a large
        # ``day_tolerance`` lets a March charge satisfy a January occurrence), so
        # the running ``last_seen`` may already hold a charge that postdates a
        # later missing occurrence. Selecting strictly-before keeps a missing
        # row's last_seen on or before its own due date in every case.
        matched_ons: list[date] = []
        # The amount of the charge that set ``last_seen`` (the most recent matched
        # charge). Surfaced for variable bills, whose stored ``amount_cents`` is
        # only a typical figure — the UI shows what was actually charged.
        last_amount_cents: int | None = None
        # A canceling/canceled bill is no longer expected to charge on or after
        # its cancel_effective date, so occurrences from that day forward must not
        # be reported missing or counted toward overdue — their absence is the
        # desired outcome. The parse layer guarantees cancel_effective is set
        # whenever lifecycle is non-active, but the date is read defensively.
        suppress_after_cancel = (
            bill.lifecycle != "active" and bill.cancel_effective is not None
        )
        # The latest scheduled occurrence that actually posted (matched a charge).
        # Compared against latest_due below: status is judged on whether the most
        # recent should-have-posted *occurrence* was paid, not on the charge's
        # posting date — a charge that clears a few days early (within tolerance)
        # still satisfies its occurrence and must not read as overdue.
        last_matched_due: date | None = None
        # The latest occurrence that is already past the grace window AND whose
        # full payment window is covered by available data — i.e. the most recent
        # charge that definitely should have posted by now and that we would have
        # seen if it had.
        latest_due: date | None = None
        for occ in monthly_dates(bill.day, start, end):
            # A cancellation suppresses overdue/missing reporting from its
            # effective date forward (computed once per occurrence and reused
            # below). Matching still runs for these occurrences so a charge that
            # posts post-cancellation is seen and surfaced as "came back".
            occ_after_cancel = (
                suppress_after_cancel and occ >= bill.cancel_effective
            )
            # An occurrence counts toward overdue only when its full in-tolerance
            # payment window is observed: a charge satisfying `occ` may post as
            # early as `occ - day_tolerance`, so if data begins after that day (or
            # there is no data at all) we cannot rule out an unseen early payment
            # and must not call the bill overdue — it reads "unseen" instead.
            # ``expected_missing`` is independent: it still reports the occurrence
            # as missing, since that is a factual per-occurrence absence.
            #
            # The window-start subtraction is done only when it stays
            # representable. If ``occ - day_tolerance`` would underflow date.min
            # the window reaches before any possible data, so the occurrence is by
            # definition not covered — a clamp UP to date.min would instead read
            # as covered and could invent a false "overdue" at the boundary.
            covered = (
                earliest_txn is not None
                and day_tolerance <= (occ - date.min).days
                and (occ - timedelta(days=day_tolerance)) >= earliest_txn
            )
            if covered and (end - occ).days >= grace and not occ_after_cancel:
                latest_due = occ
            # Greedy earliest-first match: the closest unconsumed charge within
            # tolerance, preferring an exact amount, then least date drift. For
            # variable bills the stored amount is only a typical placeholder, so
            # the exact-amount preference is disabled and ranking is by date
            # drift alone — otherwise a farther charge that happens to equal the
            # placeholder could beat the closer charge for this occurrence.
            best: _Charge | None = None
            best_key: tuple = ()
            for charge in charges:
                if charge.tid in consumed:
                    continue
                drift = abs((charge.on - occ).days)
                if drift > day_tolerance:
                    continue
                if not _matches_bill(
                    charge,
                    bill,
                    amount_tolerance_cents=amount_tolerance_cents,
                    amount_tolerance_pct=amount_tolerance_pct,
                    match_tokens=bill_tokens,
                ):
                    continue
                exact = (not bill.variable) and (
                    charge.amount_cents == bill.amount_cents
                )
                cand_key = (not exact, drift, charge.tid)
                if best is None or cand_key < best_key:
                    best, best_key = charge, cand_key
            if best is not None:
                consumed.add(best.tid)
                matched_ons.append(best.on)
                if last_seen is None or best.on > last_seen:
                    last_seen = best.on
                    last_amount_cents = best.amount_cents
                if last_matched_due is None or occ > last_matched_due:
                    last_matched_due = occ
                continue
            # No charge matched. Only call it missing once it is genuinely
            # overdue; an occurrence still inside the grace window may post late.
            if (end - occ).days < grace:
                continue
            # A canceled/canceling bill's absence on or after its effective date
            # is expected, not a problem — do not report it missing.
            if occ_after_cancel:
                continue
            # A pre-window occurrence (only reachable via the status look-back)
            # informs status above, but the missing alert stays scoped to the
            # requested audit window.
            if occ < window_start:
                continue
            # Only assert a charge is MISSING when our data fully covers the span
            # in which it could have posted. A charge for ``occ`` may clear as
            # early as ``occ - day_tolerance``; we hold complete data from
            # ``coverage_start`` — the earlier of the earliest transaction we hold
            # and the requested window start (before the earliest transaction no
            # charge exists; before the window start the caller queried nothing).
            # In production these coincide, because subscription_report clamps the
            # query start forward to the earliest transaction — but the archive is
            # loaded whole and can begin well before a narrow trailing window, so
            # ``earliest_txn`` (not ``window_start``) is the real coverage floor
            # there. If the earliest-possible payment date falls before
            # ``coverage_start`` part of the payment window lies before our data —
            # an early payment there would be unobserved, making the occurrence's
            # absence unknowable rather than a proven miss. This is the
            # just-started-bill leading edge (the window opens days before the
            # bill's first charge): reporting those occurrences as
            # "missing … last seen never" beside a real ``tracked`` last-seen was
            # the reported discrepancy. A genuine, fully-covered skip — including
            # one before the bill's first observed charge — is still reported.
            # Gated on holding data at all: with an empty archive
            # (``earliest_txn is None``) there is no data boundary to fall before,
            # so every in-window occurrence is still reported — the absence of any
            # charge is itself the signal.
            if earliest_txn is not None:
                coverage_start = min(window_start, earliest_txn)
                if day_tolerance > (occ - date.min).days or (
                    occ - timedelta(days=day_tolerance)
                ) < coverage_start:
                    continue
            # The most recent matched charge strictly before this occurrence.
            # Derived from ``matched_ons`` (not the running ``last_seen``) so a
            # charge greedily assigned to an earlier occurrence can never make a
            # missing row report a "last seen" on or after its own due date.
            # ``None`` -> "never" for a genuine no-prior-charge gap.
            prior_seen = max((d for d in matched_ons if d < occ), default=None)
            expected_missing.append(
                {
                    "name": bill.name,
                    "envelope": bill.envelope,
                    "expected_amount": _dollars(bill.amount_cents),
                    "expected_date": occ.isoformat(),
                    "match": bill.match,
                    "last_seen": prior_seen.isoformat() if prior_seen is not None else None,
                }
            )
        # Current status from the most recent should-have-posted occurrence: behind
        # only if that occurrence has no matching charge (compared by occurrence
        # date, so an early-but-in-tolerance payment counts as on time).
        if latest_due is None:
            status = "active" if last_seen is not None else "unseen"
        elif last_matched_due is None or last_matched_due < latest_due:
            status = "overdue"
        else:
            status = "active"
        # The next scheduled occurrence strictly after the window end, reusing the
        # canonical monthly expansion so the projected due date matches everywhere
        # else this bill is reasoned about. 62 days guarantees at least one
        # monthly occurrence regardless of where `end` falls in the month. The
        # ``d > end`` filter enforces the strictly-after invariant even at the
        # date.max edge, where ``_shift_days(end, 1)`` clamps back to ``end`` and
        # could otherwise surface the window-end date itself as the "next" due.
        upcoming = [
            d
            for d in monthly_dates(bill.day, _shift_days(end, 1), _shift_days(end, 62))
            if d > end
        ]
        # "Came back" = any charge from this bill's merchant/envelope posted on or
        # after the cancellation date — real money leaving the account after the
        # "Came back" = any charge from this bill's merchant/envelope posted on or
        # after the cancellation date — real money leaving the account after the
        # user expected the bill to stop. Detected by an identity scan that is
        # blind to day-of-month drift and amount, so an off-schedule return or a
        # re-bill at a changed price still raises the alert (the scheduled matcher
        # alone would miss both, and the post-cancel absence is suppressed above,
        # so this scan is the only signal a returning charge gets). came_back_on
        # is the most recent such charge, surfaced instead of the scheduled
        # last_seen because an off-schedule return never sets last_seen.
        #
        # The scan is floored at window_start as well as cancel_effective: charges
        # spans a look-back beyond the requested window (to judge status), but a
        # came-back alert must depend only on the audited window, exactly like
        # expected_missing. Without the window_start floor a return that posted
        # before `start` but inside the look-back would warn, and nudging `start`
        # by a day across that charge would flip the result — a window the caller
        # never asked about deciding the alert. A return before the window is out
        # of view here just as a pre-window missed occurrence is; widen `start` to
        # see it.
        came_back_floor = (
            max(bill.cancel_effective, window_start) if suppress_after_cancel else None
        )
        came_back_on: date | None = None
        if came_back_floor is not None:
            for charge in charges:
                if charge.on < came_back_floor:
                    continue
                if not _identity_matches_bill(charge, bill, bill_tokens):
                    continue
                if came_back_on is None or charge.on > came_back_on:
                    came_back_on = charge.on
        came_back = came_back_on is not None
        tracked.append(
            {
                "name": bill.name,
                "envelope": bill.envelope,
                "amount": _dollars(bill.amount_cents),
                "variable": bill.variable,
                "last_amount": (
                    _dollars(last_amount_cents)
                    if last_amount_cents is not None
                    else None
                ),
                "day": bill.day,
                "cadence": bill.cadence,
                "match": bill.match,
                "last_seen": last_seen.isoformat() if last_seen is not None else None,
                "next_due": upcoming[0].isoformat() if upcoming else None,
                "status": status,
                "lifecycle": bill.lifecycle,
                "cancel_effective": (
                    bill.cancel_effective.isoformat()
                    if bill.cancel_effective is not None
                    else None
                ),
                "came_back": came_back,
                "came_back_on": came_back_on.isoformat() if came_back_on is not None else None,
            }
        )
    return consumed, expected_missing, tracked


def subscription_audit(
    config: BudgetConfig,
    transactions: list[dict],
    *,
    start: date,
    end: date,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
    amount_tolerance_cents: int = 0,
    amount_tolerance_pct: float | None = None,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    grace_days: int | None = None,
) -> dict:
    """Audit tracked recurring charges and surface untracked recurring merchants.

    Pure function: all evidence is supplied. ``transactions`` carry the same
    fields the archive view exposes (``id``, ``account_id``, ``amount``,
    ``posted``, ``description``/``payee``/``memo``, ``is_transfer``). Returns a
    fully JSON-serializable report.

    ``amount_tolerance_pct`` is the global drift allowance (a 0..1 fraction of a
    bill's amount a charge may differ and still match it); ``None`` takes the
    value from ``config`` so the configured allowance applies by default while a
    caller can still override it. It is combined with the flat
    ``amount_tolerance_cents`` per bill as ``max(flat, pct*amount)``, and is
    ignored for bills flagged ``variable`` (those match amount-blind).
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    if day_tolerance < 0:
        raise ValueError(f"day_tolerance must be >= 0, got {day_tolerance}")
    if day_tolerance > _MAX_TOLERANCE_DAYS:
        raise ValueError(
            f"day_tolerance must be <= {_MAX_TOLERANCE_DAYS}, got {day_tolerance}"
        )
    if amount_tolerance_cents < 0:
        raise ValueError(f"amount_tolerance_cents must be >= 0, got {amount_tolerance_cents}")
    pct = (
        config.recurring_amount_tolerance_pct
        if amount_tolerance_pct is None
        else amount_tolerance_pct
    )
    # bool is a subclass of int, so reject it explicitly — an out-of-band True
    # would otherwise read as a 100% tolerance — mirroring the config parser.
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        raise ValueError(f"amount_tolerance_pct must be a number, got {pct!r}")
    pct = float(pct)
    if not math.isfinite(pct) or not 0.0 <= pct <= 1.0:
        raise ValueError(
            f"amount_tolerance_pct must be between 0 and 1, got {pct}"
        )
    if min_occurrences < 2:
        raise ValueError(f"min_occurrences must be >= 2, got {min_occurrences}")
    grace = day_tolerance if grace_days is None else grace_days
    if grace < 0:
        raise ValueError(f"grace_days must be >= 0, got {grace}")
    if grace > _MAX_TOLERANCE_DAYS:
        raise ValueError(
            f"grace_days must be <= {_MAX_TOLERANCE_DAYS}, got {grace}"
        )

    account_index = config.account_index()
    # Status look-back: a bill's tracked status is judged from its most recent
    # should-have-posted occurrence, which can sit before a narrow window's start.
    # Generate occurrences back far enough to always include that occurrence — the
    # latest one on or before ``end - grace`` is at most ~31 days before it, so the
    # look-back spans max(_STATUS_LOOKBACK_DAYS, grace + 31) days (never past
    # `start`, since the look-back only widens the leading edge) so even a late
    # in-tolerance charge can still match and a large grace_days can't drop the
    # occurrence. Whether an *unmatched* occurrence counts as overdue is gated
    # separately, inside _match_tracked_bills, by the earliest transaction we hold
    # — so generating an older occurrence here can never invent a false overdue.
    earliest_txn = _earliest_txn_date(transactions)
    lookback_days = max(_STATUS_LOOKBACK_DAYS, grace + 31)
    status_start = min(start, _shift_days(end, -lookback_days))
    # Match against charges from a window widened on the leading edge by
    # day_tolerance: an occurrence at the start can legitimately have been paid by
    # a charge that posted up to day_tolerance days earlier. candidate_new stays
    # strictly within [start, end] (enforced in _candidate_new) so stale archive
    # history can never surface as a "new" subscription.
    match_start = _shift_days(status_start, -day_tolerance)
    charges = _charges(transactions, account_index, start=match_start, end=end)

    consumed: set[str] = set()
    expected_missing: list[dict] = []
    # Per-bill keyword token sets (None for an envelope-only bill). The non-None
    # sets are reused as `tracked_token_sets` to keep a tracked subscription from
    # resurfacing as a "new" candidate.
    bill_token_sets, tracked_token_sets, _ = _bill_token_sets(config)
    consumed, expected_missing, tracked = _match_tracked_bills(
        config,
        charges,
        bill_token_sets,
        start=status_start,
        window_start=start,
        end=end,
        earliest_txn=earliest_txn,
        day_tolerance=day_tolerance,
        amount_tolerance_cents=amount_tolerance_cents,
        amount_tolerance_pct=pct,
        grace=grace,
    )

    candidate_new = _candidate_new(
        charges,
        consumed=consumed,
        tracked_token_sets=tracked_token_sets,
        min_occurrences=min_occurrences,
        window_start=start,
    )

    # A bill the user marked canceling/canceled that nonetheless charged again on
    # or after its effective date — the cancellation watch's headline alert.
    came_back = [t for t in tracked if t.get("came_back")]

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "day_tolerance": day_tolerance,
        "min_occurrences": min_occurrences,
        "tracked": tracked,
        "expected_missing": expected_missing,
        "came_back": came_back,
        "candidate_new": candidate_new,
        "summary": {
            "tracked": len(config.recurring),
            "missing_occurrences": len(expected_missing),
            "came_back": len(came_back),
            "candidates": len(candidate_new),
        },
    }


def _summarize_group(
    members: list["_Charge"],
    bands: tuple[tuple[str, int, int], ...] = _CADENCE_BANDS,
) -> tuple[list[date], float, str]:
    """Return ``(sorted_dates, median_gap_days, cadence_label)`` for a group."""
    dates = sorted(c.on for c in members)
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    median_gap = _median(gaps) if gaps else 0.0
    return dates, median_gap, _cadence_label(median_gap, bands)


def _interval_cv(dates: list[date]) -> float:
    """Coefficient of variation (stddev / mean) of the gaps between sorted dates.

    A dimensionless measure of how regular the spacing is: 0 is perfectly even,
    higher is more erratic. Fewer than two gaps (i.e. <=2 charges) has no spread
    to measure, so it is treated as trivially regular (``0.0``) rather than
    rejected — this keeps two-occurrence annual streams and small clusters from
    being discarded by the regularity guard. A non-positive mean gap (identical
    dates) is maximally irregular.
    """
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if len(gaps) < 2:
        return 0.0
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return float("inf")
    variance = sum((g - mean) ** 2 for g in gaps) / len(gaps)
    return (variance ** 0.5) / mean


def _recurs(
    members: list["_Charge"],
    min_occurrences: int,
    *,
    max_cv: float | None = None,
    bands: tuple[tuple[str, int, int], ...] = _CADENCE_BANDS,
    annual_matures_at_two: bool = True,
) -> bool:
    """True when a group recurs on a regular cadence.

    A group qualifies when it has enough occurrences for its cadence and its
    median gap lands in a known cadence ``band``. When ``annual_matures_at_two``
    is set, annual streams mature at :data:`_ANNUAL_MIN_OCCURRENCES`; every other
    cadence (and every cadence when the flag is off) matures at
    ``min_occurrences``. When ``max_cv`` is given, the group is additionally
    rejected if its inter-charge spacing is too erratic (coefficient of variation
    above ``max_cv``) — used to keep the broadened amount-tolerant clustering from
    admitting a merely habitual merchant that only lands in a cadence band by
    median coincidence.

    Phase 1 of :func:`_candidate_new` calls this with the *legacy* cadence bands,
    no CV guard, and ``annual_matures_at_two=False`` so locking an exact-price
    stream reproduces the pre-broadening detector exactly and can never surface a
    group the old code did not. The broadened bands, CV guard, and 2-occurrence
    annual maturity apply only to phase 2's recovery of near-miss streams.
    """
    if len(members) < 2:
        return False
    dates, _, cadence = _summarize_group(members, bands)
    if cadence == "irregular":
        return False
    if cadence == "yearly" and annual_matures_at_two:
        needed = _ANNUAL_MIN_OCCURRENCES
    else:
        needed = min_occurrences
    if len(members) < needed:
        return False
    if max_cv is not None and _interval_cv(dates) > max_cv:
        return False
    return True


def _merge_identity(
    by_tokens: dict[frozenset[str], list["_Charge"]],
) -> dict[frozenset[str], list["_Charge"]]:
    """Fold each identity token set into its *unique* maximal superset.

    The same merchant can yield different identity token sets across charges when
    an auxiliary field (e.g. payee) is populated on only some rows — e.g.
    ``{"netflix"}`` on one charge and ``{"netflix", "com"}`` on another. When a
    subset has exactly one maximal superset, folding it in reunites the merchant
    so its charges are grouped (and then amount-clustered) as one stream.

    A subset that sits under *two or more* incomparable maximal supersets is
    ambiguous generic noise (e.g. a bare ``{"pos","purchase"}`` under both
    ``{"pos","purchase","hulu"}`` and ``{"pos","purchase","disney"}``); it is
    NOT clearly the same merchant as any one of them, so it is left standalone
    rather than folded into one arbitrarily — folding it would inject off-cadence
    dates and could hide that merchant or fake a cadence. Two distinct merchants
    therefore never merge, and neither is contaminated by ambiguous remnants.

    Unlike the earlier exact-amount merge this predecessor replaced, folding here
    is amount-blind: reuniting a merchant's descriptor variants is desirable
    regardless of price, because the subsequent amount clustering (not the token
    set) is what separates two differently-priced plans at the same merchant.
    """
    token_sets = list(by_tokens.keys())
    merged: dict[frozenset[str], list[_Charge]] = {}
    for tokens in token_sets:
        supersets = [s for s in token_sets if s != tokens and tokens < s]
        # Keep only the maximal supersets (those under no other superset).
        maximal = [s for s in supersets if not any(s < other for other in supersets)]
        root = maximal[0] if len(maximal) == 1 else tokens
        merged.setdefault(root, []).extend(by_tokens[tokens])
    return merged


def _amount_clusters(members: list["_Charge"]) -> list[list["_Charge"]]:
    """Partition charges that already share a merchant identity into clusters of
    near-equal amount, so a stable price that wobbles by a cent or two stays one
    stream instead of splitting below the occurrence threshold.

    Greedy single pass over amount-sorted charges. A charge joins the current
    cluster only if doing so keeps the cluster's *total* span (max minus min)
    within ``max(flat, pct * cluster median)`` — complete-linkage, not distance
    to a running median. Complete-linkage matters because a running-median test
    lets a monotonic run chain arbitrarily far (each step within tolerance of a
    median that keeps re-centering), collapsing two genuinely distinct prices
    into one cluster whose median then matches neither charge. Capping the span
    guarantees every member is within the tolerance of every other, so the
    cluster's median is a faithful representative amount. Because the input is a
    single merchant, distinct plans/tiers (e.g. $21.49 vs $214.90) still fall
    into separate clusters while penny jitter around one price stays together.
    """
    if not members:
        return []
    ordered = sorted(members, key=lambda c: c.amount_cents)
    clusters: list[list[_Charge]] = [[ordered[0]]]
    for charge in ordered[1:]:
        current = clusters[-1]
        cluster_min = current[0].amount_cents  # input is amount-sorted ascending
        median_cents = _median([c.amount_cents for c in current])
        tolerance = max(
            _CANDIDATE_AMOUNT_TOL_CENTS,
            round(_CANDIDATE_AMOUNT_TOL_PCT * median_cents),
        )
        if charge.amount_cents - cluster_min <= tolerance:
            current.append(charge)
        else:
            clusters.append([charge])
    return clusters


def _recurring_subgroups(
    cluster: list["_Charge"], min_occurrences: int
) -> list[list["_Charge"]]:
    """Phase-2 keep test for one amount cluster: return the member groups to emit
    as candidates (empty list = nothing recurring here).

    The pre-broadening detector grouped on exact cents, so any *distinct exact
    amount* that recurs on its own (legacy bands, no CV guard, no annual-at-2) is
    a stream the old code surfaced. Those exact streams are computed FIRST and, if
    any exist, are ALWAYS emitted: a legacy exact stream must never be masked by a
    nearby in-tolerance charge that the amount grouping happened to pull into the
    same cluster. The CV guard is deliberately not applied to them — the old code
    emitted them regardless of spacing regularity.

    The whole-cluster amount-tolerant recovery grouping (broader/extra cadence
    bands, 2-occurrence annual maturity, WITH the CV guard) is the genuinely new
    capability. It runs:

    * on the whole cluster when NO exact amount recurs on its own — this groups a
      single price that wobbles a cent or two across postings into one stream; or
    * only on the members NOT claimed by any legacy stream otherwise — so the new
      matching can still surface a jittery leftover stream without ever dropping,
      splitting, or re-cadencing a legacy exact stream.

    Because legacy streams partition the cluster by exact amount and recovery runs
    only on the unclaimed remainder, no charge is ever emitted in two groups.
    """
    by_amount: dict[int, list[_Charge]] = {}
    for member in cluster:
        by_amount.setdefault(member.amount_cents, []).append(member)
    legacy_streams: list[list[_Charge]] = []
    claimed_tids: set = set()
    for amount_cents in sorted(by_amount):
        subset = by_amount[amount_cents]
        if _recurs(
            subset,
            min_occurrences,
            bands=_LEGACY_CADENCE_BANDS,
            annual_matures_at_two=False,
        ):
            legacy_streams.append(subset)
            claimed_tids.update(m.tid for m in subset)
    if not legacy_streams:
        if _recurs(cluster, min_occurrences, max_cv=_MAX_INTERVAL_CV):
            return [cluster]
        return []
    subgroups = list(legacy_streams)
    leftover = [m for m in cluster if m.tid not in claimed_tids]
    if leftover and _recurs(leftover, min_occurrences, max_cv=_MAX_INTERVAL_CV):
        subgroups.append(leftover)
    return subgroups


# Structural payment/banking tokens that are never a merchant identity on their
# own — bank-printed boilerplate like "POS PURCHASE", "DEBIT CARD", "ACH". A
# detected keyword built ONLY from these would false-match unrelated charges at
# the same price, so a candidate whose entire shared key is generic is surfaced
# for manual review instead of auto-pinned. Only an all-generic key is rejected:
# a key that still carries a distinctive token (e.g. {"pos","purchase","hulu"})
# pins reliably and is kept.
_GENERIC_MERCHANT_TOKENS = frozenset(
    {
        "pos", "purchase", "debit", "credit", "card", "checkcard", "ckcd",
        "payment", "pmt", "bill", "billpay", "autopay", "auth", "preauth",
        "authorized", "transaction", "trans", "txn", "ach", "eft", "dda",
        "withdrawal", "deposit", "recurring", "www", "com",
        # Card-network names are boilerplate too: a key like "visa purchase" is
        # no more pinnable than "pos purchase".
        "visa", "mastercard", "mc", "amex", "discover",
    }
)


def _all_generic(tokens: frozenset[str] | set[str]) -> bool:
    """True when every token is structural banking boilerplate (no merchant identity)."""
    return bool(tokens) and tokens <= _GENERIC_MERCHANT_TOKENS


def _candidate_new(
    charges: list[_Charge],
    *,
    consumed: set[str],
    tracked_token_sets: list[frozenset[str]],
    min_occurrences: int,
    window_start: date,
) -> list[dict]:
    """Group untracked debits into recurring-looking subscription candidates.

    ``charges`` may include leading-edge debits before ``window_start`` (kept for
    expected-missing matching); those are excluded here so candidate detection is
    scoped strictly to the requested window.
    """
    # Keep only the charges eligible to become a NEW candidate: inside the
    # window, carrying a merchant identity, not already consumed by a tracked
    # bill, and not suppressed by a tracked keyword. (Identity/suppression rules
    # unchanged from the exact-bucket predecessor.)
    kept: list[_Charge] = []
    for charge in charges:
        if charge.tid in consumed:
            continue
        if charge.on < window_start:
            continue  # leading-edge match-only charge, not part of this window
        if not charge.match_tokens:
            # No merchant identity to surface or group on — never invent a
            # candidate (e.g. don't group blank-description debits under a card).
            continue
        # A charge already covered by a tracked bill keyword is not "new" — even
        # if its amount drifted out of match tolerance and it wasn't consumed.
        # Suppression is amount-blind on purpose: a keyword-tracked merchant
        # whose price changed must not resurface as a new candidate (which detect
        # would write as a second bill competing with the first). Tested against
        # the charge's merchant-identity tokens (description + payee) so a keyword
        # living in the payee still suppresses it, and token-subset (not raw
        # substring) so only a genuine merchant overlap suppresses it.
        if any(ts <= charge.match_tokens for ts in tracked_token_sets):
            continue
        kept.append(charge)

    # Two-phase grouping designed so the broadening can only ever ADD streams,
    # never drop or change one the pre-broadening detector already surfaced.
    #
    # The old detector surfaced a stream when charges sharing ONE exact amount
    # (across descriptor variants) had a median gap in the legacy cadence bands
    # and enough occurrences — with no regularity (CV) guard. We must keep every
    # one of those. The broadening ADDS: wider/extra cadence bands, 2-occurrence
    # annual maturity, and amount-tolerant clustering (a price that wobbles by a
    # cent or two stays one stream) — but those additions are gated by the CV
    # guard so the looser matching cannot admit a merely habitual merchant.
    #
    # Phase 1 — lock exact streams that qualify under the LEGACY test verbatim
    # (legacy bands, no CV, no annual-at-2). Locked charges are removed from
    # further grouping so nothing they contain can be regrouped or double-counted.
    #
    # Phase 2 — regroup the leftovers by merchant identity (`_merge_identity`
    # reunites descriptor variants of one merchant), split each identity into
    # near-equal-amount clusters, and keep a cluster when it qualifies under
    # EITHER path: the legacy test (only for a cluster at a single exact amount —
    # this recovers an exact stream the old code surfaced but that phase 1 missed
    # because descriptor fragmentation kept each exact bucket below the
    # threshold; the CV guard must NOT apply here or an irregular-but-real stream
    # the old code emitted would be lost), OR the broadened test with the CV
    # guard (the genuinely new capability — wider cadences and amount tolerance,
    # regularity-checked).
    groups: list[tuple[frozenset[str], list[_Charge]]] = []
    claimed: set[str] = set()
    exact_buckets: dict[tuple[int, frozenset[str]], list[_Charge]] = {}
    for charge in kept:
        exact_buckets.setdefault(
            (charge.amount_cents, charge.match_tokens), []
        ).append(charge)
    for (_amount_cents, tokens), members in exact_buckets.items():
        if _recurs(
            members,
            min_occurrences,
            bands=_LEGACY_CADENCE_BANDS,
            annual_matures_at_two=False,
        ):
            groups.append((tokens, members))
            claimed.update(m.tid for m in members)

    by_tokens: dict[frozenset[str], list[_Charge]] = {}
    for charge in kept:
        if charge.tid in claimed:
            continue
        by_tokens.setdefault(charge.match_tokens, []).append(charge)
    for tokens, members in _merge_identity(by_tokens).items():
        for cluster in _amount_clusters(members):
            for subgroup in _recurring_subgroups(cluster, min_occurrences):
                groups.append((tokens, subgroup))

    candidates: list[dict] = []
    for tokens, members in groups:
        dates, median_gap, cadence = _summarize_group(members)
        amount_cents = round(_median([m.amount_cents for m in members]))
        merchant_key = " ".join(sorted(tokens))
        # The stable match key is the identity tokens shared by *every* charge in
        # the group: it is a subset of each member, so it is guaranteed to match
        # every grouped charge (the audit matches a keyword by token-subset) on
        # the group's own billing day, and it drops per-charge volatile tokens
        # (auth codes, store ids) that appear on only some rows. When the group
        # has no token common to all members (a genuinely disjoint cluster), this
        # is empty and the merchant cannot be pinned by any single subset keyword
        # — the caller skips auto-tracking it rather than fabricate a keyword that
        # would miss the merchant's own charges and cry "missing".
        shared = frozenset.intersection(*(m.match_tokens for m in members))
        match_key = " ".join(sorted(shared))
        candidates.append(
            {
                "merchant": _representative_merchant(members),
                "merchant_key": merchant_key,
                "match_key": match_key,
                "amount": _dollars(amount_cents),
                "occurrences": len(members),
                "first_seen": dates[0].isoformat(),
                "last_seen": dates[-1].isoformat(),
                "median_interval_days": round(median_gap, 1),
                "cadence": cadence,
                "sample_descriptions": _samples(members),
            }
        )

    candidates.sort(
        key=lambda c: (
            _CADENCE_ORDER.get(c["cadence"], 9),
            -c["occurrences"],
            c["merchant_key"],
            c["amount"],
        )
    )
    return candidates


def _tracked_amount_mismatch(
    charges: list[_Charge],
    *,
    consumed: set[str],
    tracked_token_sets: list[frozenset[str]],
    tracked_amounts: list[int],
    min_occurrences: int,
    window_start: date,
) -> list[dict]:
    """Recurring charges that share a tracked keyword but post at a *different* price.

    A charge whose merchant tokens match an existing keyword bill is suppressed
    from the "new merchant" candidates (so detect never auto-writes a second bill
    competing with the tracked one). But a *recurring* run of such charges at a
    price that matches NONE of the same-keyword tracked bills is a real signal —
    either the tracked subscription's price changed, or the user has a distinct
    second plan under the same merchant. Rather than silently drop it, surface it
    for the user to resolve. Returns one entry per recurring monthly group; never
    written automatically. A charge at the *same* price as a tracked bill (just
    off-cadence) is not a mismatch and is left out.
    """
    if not tracked_token_sets:
        return []
    buckets: dict[tuple[int, frozenset[str]], list[_Charge]] = {}
    for charge in charges:
        if charge.tid in consumed:
            continue
        if charge.on < window_start:
            continue
        if not charge.match_tokens:
            continue
        matched = [
            (tokens, amount)
            for tokens, amount in zip(tracked_token_sets, tracked_amounts)
            if tokens <= charge.match_tokens
        ]
        if not matched:
            continue  # not a tracked merchant — handled by normal candidate path
        if any(amount == charge.amount_cents for _tokens, amount in matched):
            continue  # same price as a tracked bill — the tracked sub, not a mismatch
        # Group by the matched tracked keyword(s), not the full charge token set:
        # the descriptor varies between postings of one merchant ("APPLE COM BILL"
        # vs "APPLE ICLOUD"), and keying on the raw tokens would fragment a single
        # recurring run into sub-threshold buckets and silently drop the signal.
        # The tracked keyword is the merchant identity we already matched on.
        matched_key = frozenset().union(*(tokens for tokens, _amount in matched))
        buckets.setdefault((charge.amount_cents, matched_key), []).append(charge)

    out: list[dict] = []
    for (amount_cents, _tokens), members in buckets.items():
        if not _recurs(members, min_occurrences):
            continue
        _, _, cadence = _summarize_group(members)
        if cadence != "monthly":
            continue
        out.append(
            {
                "merchant": _representative_merchant(members),
                "amount": _dollars(amount_cents),
                "cadence": cadence,
                "occurrences": len(members),
            }
        )
    out.sort(key=lambda m: (m["merchant"], m["amount"]))
    return out


def _representative_merchant(members: list[_Charge]) -> str:
    """The most common raw merchant string in a group (lexical tiebreak)."""
    counts: dict[str, int] = {}
    for c in members:
        counts[c.merchant_raw] = counts.get(c.merchant_raw, 0) + 1
    return min(counts, key=lambda raw: (-counts[raw], raw))


def _samples(members: list[_Charge]) -> list[str]:
    """Up to three distinct raw descriptions, for the assistant to eyeball."""
    seen: list[str] = []
    for c in members:
        if c.merchant_raw not in seen:
            seen.append(c.merchant_raw)
        if len(seen) == 3:
            break
    return seen


def subscription_report(
    config: BudgetConfig,
    *,
    start: date | None = None,
    end: date | None = None,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
    amount_tolerance_cents: int = 0,
    amount_tolerance_pct: float | None = None,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    grace_days: int | None = None,
) -> dict:
    """Load the categorized archive, then run the subscription audit.

    ``end`` defaults to today and ``start`` to a year before it so a monthly
    subscription reliably clears ``min_occurrences``. The categorized
    transactions are read from the durable archive (falling back to the JSON
    cache when no archive exists yet).

    When the archive holds transactions, the audit start is clamped forward to
    the earliest transaction date: occurrences before any data exists could only
    ever be reported "missing" because there is nothing to match them, which is
    noise, not a billing signal. (With an empty archive there is no earliest
    date, so every in-window occurrence is still listed in ``expected_missing`` —
    the absence of any charge is the signal there. A bill's tracked ``status``,
    however, requires evidence: with no data covering an occurrence's payment
    window it reads ``unseen`` rather than ``overdue``, since an early payment we
    never synced cannot be ruled out.)
    """
    from . import store

    end = end or date.today()
    start = start or default_start(end)

    view = store.load_archive_view()
    transactions = view["transactions"]
    earliest = _earliest_txn_date(transactions)
    if earliest is not None and earliest > start:
        start = min(earliest, end)
    return subscription_audit(
        config,
        transactions,
        start=start,
        end=end,
        day_tolerance=day_tolerance,
        amount_tolerance_cents=amount_tolerance_cents,
        amount_tolerance_pct=amount_tolerance_pct,
        min_occurrences=min_occurrences,
        grace_days=grace_days,
    )


def _earliest_txn_date(transactions: list[dict]) -> date | None:
    """The earliest parseable posted date across ``transactions`` (or None)."""
    earliest: date | None = None
    for txn in transactions:
        on = _txn_date(txn)
        if on is not None and (earliest is None or on < earliest):
            earliest = on
    return earliest


def detect_subscriptions(
    transactions: list[dict],
    *,
    start: date,
    end: date,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
    config: BudgetConfig | None = None,
) -> dict:
    """Propose tracked recurring bills from observed history.

    Runs the same untracked-candidate detection the audit uses, then shapes each
    monthly-cadence candidate into a ``recurring`` bill dict ready to drop into a
    budget config: ``name`` (the representative merchant), ``match`` (the tokens
    shared by every observed charge, so the bill is pinned by merchant and needs
    no envelope), ``amount`` (the group's stable price), ``cadence`` (always
    ``"monthly"`` here), and ``day`` (the nominal day-of-month, taken from the
    most recent occurrence so it reflects the current billing date).

    When ``config`` is supplied, a merchant already covered by an existing bill
    is NOT re-proposed: charges consumed by the existing recurring bills (whether
    pinned by a ``match`` keyword *or* by an envelope→account binding) are
    excluded exactly as the audit excludes them, and existing keyword token sets
    suppress their merchant from the candidate list. This keeps detect from
    adding a second, differently-named bill for a merchant the user already
    tracks — which would leave two bills competing for one charge and cry a false
    "missing" alert every cycle.

    Only monthly merchants become bills because ``monthly`` is the one cadence
    the budget's recurring schema and projector support today; weekly/yearly
    merchants are returned under ``"skipped"`` (each tagged ``kind:
    "unsupported_cadence"``) so the caller can report them rather than write an
    unsupported cadence that parsing would reject. A monthly merchant whose
    charges share no common identity token, or whose only shared key is
    structural banking boilerplate (e.g. "pos purchase") that would false-match
    unrelated charges, is also skipped (``kind: "needs_review"``) rather than
    auto-tracked under a keyword that cannot reliably pin it. Finally, a recurring
    run of charges that shares a tracked bill's keyword but posts at a *different*
    price (a price change, or a distinct second plan under the same merchant) is
    surfaced under ``kind: "needs_review"`` instead of being silently dropped —
    detect never auto-writes it (that would create a second competing bill), but
    the user is told so they can update the amount or add a separate bill. Pure:
    all evidence is supplied; nothing is read from disk or written.
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    if day_tolerance < 0:
        raise ValueError(f"day_tolerance must be >= 0, got {day_tolerance}")
    if day_tolerance > _MAX_TOLERANCE_DAYS:
        raise ValueError(
            f"day_tolerance must be <= {_MAX_TOLERANCE_DAYS}, got {day_tolerance}"
        )
    cfg = config if config is not None else BudgetConfig(
        version=0, envelopes=(), recurring=(), scheduled_transfers=()
    )
    # Build charges over the audit's leading-edge-widened window so a charge near
    # the window start still groups with its merchant. Account identity comes from
    # the existing config so an envelope-only bill can consume a charge that posted
    # to one of its accounts.
    charges = _charges(
        transactions,
        cfg.account_index(),
        start=_shift_days(start, -day_tolerance),
        end=end,
    )
    # Exclude charges already covered by an existing tracked bill (keyword- OR
    # envelope-pinned), and suppress those merchants from the candidate list, so
    # an already-tracked subscription is never proposed a second time. consumed is
    # grace-independent, so grace_days is immaterial here.
    bill_token_sets, tracked_token_sets, tracked_amounts = _bill_token_sets(cfg)
    consumed, _, _ = _match_tracked_bills(
        cfg,
        charges,
        bill_token_sets,
        start=start,
        window_start=start,
        end=end,
        earliest_txn=_earliest_txn_date(transactions),
        day_tolerance=day_tolerance,
        amount_tolerance_cents=0,
        amount_tolerance_pct=cfg.recurring_amount_tolerance_pct,
        grace=day_tolerance,
    )
    candidates = _candidate_new(
        charges,
        consumed=consumed,
        tracked_token_sets=tracked_token_sets,
        min_occurrences=min_occurrences,
        window_start=start,
    )
    bills: list[dict] = []
    skipped: list[dict] = []
    billable: list[dict] = []
    for cand in candidates:
        if cand["cadence"] != "monthly":
            skipped.append(
                {
                    "merchant": cand["merchant"],
                    "cadence": cand["cadence"],
                    "kind": "unsupported_cadence",
                    "reason": "only monthly bills can be tracked in the budget config",
                }
            )
            continue
        if not cand["match_key"]:
            # No token is common to all of this merchant's observed charges, so no
            # single keyword can pin it without missing some of its own charges.
            # Surface it for the user to handle rather than auto-track unreliably.
            skipped.append(
                {
                    "merchant": cand["merchant"],
                    "cadence": cand["cadence"],
                    "kind": "needs_review",
                    "reason": "merchant text varies too much to pin with one keyword",
                }
            )
            continue
        if _all_generic(frozenset(cand["match_key"].split())):
            # The only key common to every charge is structural banking
            # boilerplate (e.g. "pos purchase") — pinning it would false-match
            # unrelated charges at the same price. Surface for manual review
            # rather than auto-track an over-broad keyword.
            skipped.append(
                {
                    "merchant": cand["merchant"],
                    "cadence": cand["cadence"],
                    "kind": "needs_review",
                    "reason": "merchant text is too generic to pin with one keyword",
                }
            )
            continue
        billable.append(cand)

    # Two monthly candidates that resolve to the SAME match keyword cannot both
    # become bills: one keyword cannot route a future charge to two different
    # bills, so a second same-keyword bill would double-count the merchant's
    # recurring spend while one bill perpetually reports a missing occurrence.
    # This happens when a single subscription's price steps within tolerance
    # mid-window (leaving two exact streams at one merchant) or when a merchant
    # genuinely runs two look-alike plans. Keep the most-recent price as the bill
    # and surface the rest for the user to resolve as a price change or a separate
    # subscription — the same treatment an already-tracked keyword's price
    # mismatch receives below. Dict insertion order (following the sorted
    # candidate list) keeps bill output deterministic.
    by_match: dict[str, list[dict]] = {}
    for cand in billable:
        by_match.setdefault(cand["match_key"], []).append(cand)
    for group in by_match.values():
        primary = max(group, key=lambda c: (c["last_seen"], c["amount"]))
        bills.append(
            {
                "name": primary["merchant"],
                "match": primary["match_key"],
                "amount": primary["amount"],
                "cadence": "monthly",
                "day": date.fromisoformat(primary["last_seen"]).day,
            }
        )
        for other in group:
            if other is primary:
                continue
            skipped.append(
                {
                    "merchant": other["merchant"],
                    "cadence": other["cadence"],
                    "kind": "needs_review",
                    "reason": (
                        f"a recurring charge of ${other['amount']} matches the "
                        f"same keyword as a proposed ${primary['amount']} monthly "
                        f"bill for {primary['merchant']} — review whether this is a "
                        "price change or a separate subscription"
                    ),
                }
            )
    # A recurring charge that shares a tracked keyword but posts at a different
    # price is not a "new merchant" (it's suppressed above) yet must not vanish:
    # surface it for review so the user can update the bill amount or add a second
    # bill. Never auto-written here.
    for mismatch in _tracked_amount_mismatch(
        charges,
        consumed=consumed,
        tracked_token_sets=tracked_token_sets,
        tracked_amounts=tracked_amounts,
        min_occurrences=min_occurrences,
        window_start=start,
    ):
        skipped.append(
            {
                "merchant": mismatch["merchant"],
                "cadence": mismatch["cadence"],
                "kind": "needs_review",
                "reason": (
                    f"a recurring charge of ${mismatch['amount']} matches an "
                    "already-tracked subscription's keyword but at a different "
                    "price — review whether this is a price change or a separate "
                    "subscription"
                ),
            }
        )
    return {"bills": bills, "skipped": skipped}


def _recurring_match_key(match_text: object) -> frozenset[str]:
    """Normalized token set for a recurring bill's ``match`` keyword.

    Reuses the same merchant normalization the audit matches with, so two
    spellings of one merchant collapse to one key and re-running detect is
    idempotent. A non-string or empty keyword yields an empty set (never tracked
    as a duplicate of a real keyword)."""
    if not isinstance(match_text, str):
        return frozenset()
    return frozenset(_merchant_key(match_text).split())


def _amount_to_cents_or_none(value: object) -> int | None:
    """Best-effort parse of a recurring-bill amount to integer cents.

    Used only to decide whether two bills target the same price (and so would
    compete for one charge). Returns ``None`` for anything that is not a finite
    whole number of cents; a ``None`` amount never counts as equal to another, so
    an unparseable amount is treated as a distinct price rather than silently
    deduped. The final :func:`budget_config.parse_config` validation, not this
    helper, is what rejects a genuinely bad amount before the file is written.
    """
    from decimal import Decimal, InvalidOperation

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        cents = Decimal(str(value)) * 100
    except InvalidOperation:
        return None
    if not cents.is_finite() or cents != cents.to_integral_value():
        return None
    return int(cents)


# Serializes read-modify-write of the budget config so two markers cannot each
# read the same starting file and have the later write silently drop the
# earlier one's change (a lost update). The process-local lock covers the
# threading web server's concurrent workers; an OS file lock, where available,
# additionally serializes against a separate process (a CLI run or the MCP
# server) writing the same file. The guard must wrap the whole read-through-
# replace, not just the write, because the hazard is acting on a stale read.
_CONFIG_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def _config_write_guard(path):
    """Hold the config write lock across a full read-modify-write of ``path``.

    The process-local lock serializes this process's own threads (the threaded
    web server is the only concurrency this code itself creates). On POSIX an
    exclusive ``flock`` on a sibling ``<path>.lock`` file additionally serializes
    against a separate process — a CLI run or the MCP server writing the same
    file. If that file lock cannot be taken on a platform that supports it, the
    write fails loudly rather than silently dropping the cross-process guarantee.
    A platform without ``fcntl`` falls back to process-local locking only (there
    is no portable flock there); that still covers the in-process race.
    """
    import os

    from . import budget_config

    with _CONFIG_WRITE_LOCK:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX has no flock
            fcntl = None
        lock_fd = None
        if fcntl is not None:
            try:
                lock_fd = os.open(
                    os.fspath(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600
                )
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                if lock_fd is not None:
                    os.close(lock_fd)
                # Fail closed: this platform supports the cross-process lock but
                # acquiring it errored, so we cannot promise serialization
                # against another process. Refuse rather than write blind.
                raise budget_config.BudgetConfigError(
                    f"cannot acquire write lock for {path}: {exc}"
                ) from exc
        try:
            yield
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)


def _persist_config(path, raw: dict) -> None:
    """Validate ``raw`` as a budget config and atomically publish it to ``path``.

    Re-parses with :func:`budget_config.parse_config` first, so a config that
    would not load never overwrites a working one. The write is atomic (sibling
    temp file + ``os.replace``), so an interrupted or partial write can never
    truncate the budget config — the single source of truth — and the file is
    always either the old or the new whole. Validation and filesystem errors
    (e.g. an unwritable or missing parent directory) are surfaced as
    :class:`budget_config.BudgetConfigError` so callers report a structured
    error rather than a raw traceback.
    """
    import json
    import os
    import tempfile

    from . import budget_config

    budget_config.parse_config(raw)
    payload = json.dumps(raw, indent=2) + "\n"
    directory = path.parent if str(path.parent) else "."
    try:
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=path.name, suffix=".tmp")
    except OSError as exc:
        raise budget_config.BudgetConfigError(
            f"cannot write budget config to {path}: {exc}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise budget_config.BudgetConfigError(
            f"cannot write budget config to {path}: {exc}"
        ) from exc
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def merge_subscriptions_into_file(path, bills: list[dict]) -> dict:
    """Append proposed recurring bills to the budget file at ``path``.

    Creates the file (with an empty ``envelopes`` list) when absent, so tracking
    subscriptions needs no prior budget setup. A proposed bill is skipped only
    when an existing recurring entry would compete with it for the *same* charge —
    judged the way the audit's matcher judges it: the proposed ``match`` token set
    overlaps an existing entry's by subset *or* superset (so ``"netflix"`` and
    ``"netflix com"`` are recognized as one merchant, not two), or the name
    matches case-insensitively, *and* the two amounts are equal. The amount guard
    matters because the audit matches a charge to a bill by keyword **and** amount
    (exact by default), so two same-merchant subscriptions at different prices
    (e.g. two Apple plans) are genuinely distinct bills and must both be kept;
    deduping them on keyword alone would silently drop the second and then hide it
    from future detection. Re-running detect on unchanged data still finds the
    same amounts, so it remains idempotent. The merged config is re-validated with
    :func:`budget_config.parse_config` before anything is written, and the write is
    atomic (temp file + ``os.replace``), so a malformed or interrupted merge never
    overwrites a working config. Filesystem errors are raised as
    :class:`budget_config.BudgetConfigError`. Returns a summary of what changed.
    """
    import json

    from . import budget_config

    with _config_write_guard(path):
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise budget_config.BudgetConfigError(
                    f"budget config {path} is not valid JSON: {exc}"
                ) from exc
            except OSError as exc:
                raise budget_config.BudgetConfigError(
                    f"cannot read budget config {path}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise budget_config.BudgetConfigError("budget config must be a JSON object")
        else:
            raw = {}
        raw.setdefault("version", budget_config.SUPPORTED_VERSION)
        raw.setdefault("envelopes", [])
        existing = raw.setdefault("recurring", [])
        if not isinstance(existing, list):
            raise budget_config.BudgetConfigError(
                "budget config 'recurring' must be a list"
            )

        # Each tracked bill recorded as (keyword tokens, lowercased name, amount in
        # cents) so a proposal is only treated as a duplicate of one at the same price.
        seen: list[tuple[frozenset[str], str, int | None]] = []
        for entry in existing:
            if isinstance(entry, dict):
                key = _recurring_match_key(entry.get("match"))
                name = entry.get("name")
                name_l = name.strip().lower() if isinstance(name, str) else ""
                seen.append((key, name_l, _amount_to_cents_or_none(entry.get("amount"))))

        def _already_tracked(key: frozenset[str], name_l: str, amount: int | None) -> bool:
            # A proposal duplicates an existing bill only when both would match the
            # same charge: same price AND keyword overlap (subset/superset, mirroring
            # the audit's ``match_tokens <= charge tokens`` rule). A keyword match at a
            # *different* amount is a distinct subscription (e.g. a second Apple plan)
            # and must be kept; an unparseable amount (None) never compares equal, so
            # it is never deduped away here — the final parse_config validation is what
            # rejects a truly bad amount. The display name is only a *fallback* tie
            # break, used when at least one side has no usable keyword (e.g. an
            # existing envelope-only bill): two keyword-backed bills with disjoint
            # keywords are distinct merchants even when their display names collide,
            # which is common when the merchant lives in the payee under a generic
            # description like "POS PURCHASE" — deduping those on name would silently
            # drop a real second subscription.
            for skey, sname, samount in seen:
                if amount is None or samount is None or amount != samount:
                    continue
                if key and skey:
                    if key <= skey or skey <= key:
                        return True
                    continue  # two distinct keywords -> distinct merchants, never dedup
                if name_l and sname and name_l == sname:
                    return True
            return False

        added: list[dict] = []
        skipped: list[dict] = []
        for bill in bills:
            key = _recurring_match_key(bill.get("match"))
            name_l = str(bill.get("name", "")).strip().lower()
            amount = _amount_to_cents_or_none(bill.get("amount"))
            if _already_tracked(key, name_l, amount):
                skipped.append(bill)
                continue
            existing.append(bill)
            seen.append((key, name_l, amount))
            added.append(bill)

        _persist_config(path, raw)
    return {
        "path": str(path),
        "added": len(added),
        "already_tracked": len(skipped),
        "tracked_total": len(existing),
        "added_bills": added,
    }


def set_bill_lifecycle(
    path,
    name: str,
    lifecycle: str,
    cancel_effective: str | None = None,
    variable: bool | None = None,
) -> dict:
    """Set the lifecycle of one recurring bill in the budget file at ``path``.

    The cancellation watch's write side: marks a bill ``canceling`` (a
    cancellation was attempted) or ``canceled`` (confirmed), or back to
    ``active``. ``cancel_effective`` is the ISO date the cancellation takes
    effect and is required for the two non-active states (a charge on or after
    it is what the audit flags as the bill "coming back"); for ``active`` it must
    be omitted and any existing value is cleared.

    ``variable`` optionally sets whether the bill's amount varies every cycle
    (``True`` matches the bill amount-blind and reports the actual charged
    amount; ``False`` restores exact-amount matching). ``None`` (the default)
    leaves the existing setting untouched, so a caller marking only the lifecycle
    does not disturb a bill's variable flag.

    The bill is located by ``name``, matched case-insensitively after trimming.
    A name that matches no bill, or more than one, is an error rather than a
    silent no-op or an ambiguous guess, so the caller always knows exactly which
    bill changed. The edited config is re-validated and atomically written by
    :func:`_persist_config`, so an invalid lifecycle/date combination is rejected
    before it can overwrite a working config. Returns a summary of the change.
    """
    import json

    from . import budget_config

    valid = sorted(budget_config.LIFECYCLE_STATES)
    if not isinstance(lifecycle, str) or lifecycle.strip().lower() not in valid:
        raise budget_config.BudgetConfigError(
            f"lifecycle must be one of {valid}, got {lifecycle!r}"
        )
    lifecycle = lifecycle.strip().lower()
    if variable is not None and not isinstance(variable, bool):
        raise budget_config.BudgetConfigError(
            f"variable must be true or false, got {variable!r}"
        )

    with _config_write_guard(path):
        if not path.exists():
            raise budget_config.BudgetConfigError(
                f"budget config {path} does not exist; nothing to mark"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise budget_config.BudgetConfigError(
                f"budget config {path} is not valid JSON: {exc}"
            ) from exc
        except OSError as exc:
            raise budget_config.BudgetConfigError(
                f"cannot read budget config {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise budget_config.BudgetConfigError("budget config must be a JSON object")

        recurring = raw.get("recurring", [])
        if not isinstance(recurring, list):
            raise budget_config.BudgetConfigError(
                "budget config 'recurring' must be a list"
            )

        target = name.strip().lower()
        matches = [
            entry
            for entry in recurring
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"].strip().lower() == target
        ]
        if not matches:
            raise budget_config.BudgetConfigError(
                f"no recurring bill named {name!r} in {path}"
            )
        if len(matches) > 1:
            raise budget_config.BudgetConfigError(
                f"{len(matches)} recurring bills named {name!r} in {path}; "
                "names must be unique to mark one"
            )
        entry = matches[0]

        if lifecycle == "active":
            # cancel_effective is meaningless for an active bill, so reactivating
            # clears it; requiring the caller to pass none avoids a silent override.
            if cancel_effective is not None:
                raise budget_config.BudgetConfigError(
                    "cancel_effective must be omitted when reactivating a bill"
                )
            entry.pop("lifecycle", None)
            entry.pop("cancel_effective", None)
        else:
            if cancel_effective is None:
                raise budget_config.BudgetConfigError(
                    f"cancel_effective (an ISO date) is required to mark a bill "
                    f"{lifecycle!r}"
                )
            entry["lifecycle"] = lifecycle
            entry["cancel_effective"] = cancel_effective

        # Apply the variable flag independently of lifecycle. Stored only when
        # True (the default is False, so an omitted key reads as fixed) to keep
        # the config minimal: clearing it removes the key rather than writing a
        # redundant ``false``.
        if variable is not None:
            if variable:
                entry["variable"] = True
            else:
                entry.pop("variable", None)

        # _persist_config re-validates, so a malformed cancel_effective is rejected
        # here (as a structured error) before it can overwrite a working config.
        _persist_config(path, raw)
    return {
        "path": str(path),
        "name": entry["name"],
        "lifecycle": lifecycle,
        "cancel_effective": entry.get("cancel_effective"),
        "variable": bool(entry.get("variable", False)),
    }
