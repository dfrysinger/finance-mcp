"""Subscription audit: did each tracked recurring charge post, and what
recurring-looking merchants aren't tracked yet?

Two scripted outputs the assistant reasons over:

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

A tracked bill without a ``match`` keyword is matched by its envelope->account
binding instead: a debit on one of the envelope's accounts, for the expected
amount, near the expected day. Pinning the merchant with a ``match`` keyword is
strictly more reliable (it survives the charge landing on a different card), so
it is recommended for anything you want a dependable missing-charge alert on.

Money is carried in integer cents throughout and rendered to dollars only at the
report edge. Candidate amounts are grouped on exact cents because a stable price
is the subscription signal; a mid-window price change therefore splits a merchant
into two groups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .budget_config import BudgetConfig, Envelope, RecurringBill, monthly_dates
from .normalize import amount_to_cents

DEFAULT_WINDOW_DAYS = 365
DEFAULT_DAY_TOLERANCE = 7
DEFAULT_MIN_OCCURRENCES = 3

# Median-spacing bands (in days) used to label a candidate's cadence. A group
# whose median gap falls outside every band is treated as irregular, not a
# subscription, and is dropped from the candidate list.
_CADENCE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 5, 9),
    ("monthly", 24, 35),
    ("yearly", 350, 380),
)
_CADENCE_ORDER = {"weekly": 0, "monthly": 1, "yearly": 2}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS_ONLY = re.compile(r"^\d+$")


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


def _matches_bill(
    charge: _Charge,
    bill: RecurringBill,
    *,
    amount_tolerance_cents: int,
    match_tokens: frozenset[str] | None,
) -> bool:
    if abs(charge.amount_cents - bill.amount_cents) > amount_tolerance_cents:
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
    # No merchant keyword: fall back to the envelope the charge landed in.
    return charge.envelope == bill.envelope


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _cadence_label(median_days: float) -> str:
    for name, lo, hi in _CADENCE_BANDS:
        if lo <= median_days <= hi:
            return name
    return "irregular"


def _dollars(cents: int) -> str:
    """Render integer cents as a fixed two-decimal dollar string."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def subscription_audit(
    config: BudgetConfig,
    transactions: list[dict],
    *,
    start: date,
    end: date,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
    amount_tolerance_cents: int = 0,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    grace_days: int | None = None,
) -> dict:
    """Audit tracked recurring charges and surface untracked recurring merchants.

    Pure function: all evidence is supplied. ``transactions`` carry the same
    fields the archive view exposes (``id``, ``account_id``, ``amount``,
    ``posted``, ``description``/``payee``/``memo``, ``is_transfer``). Returns a
    fully JSON-serializable report.
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    if day_tolerance < 0:
        raise ValueError(f"day_tolerance must be >= 0, got {day_tolerance}")
    if amount_tolerance_cents < 0:
        raise ValueError(f"amount_tolerance_cents must be >= 0, got {amount_tolerance_cents}")
    if min_occurrences < 2:
        raise ValueError(f"min_occurrences must be >= 2, got {min_occurrences}")
    grace = day_tolerance if grace_days is None else grace_days
    if grace < 0:
        raise ValueError(f"grace_days must be >= 0, got {grace}")

    account_index = config.account_index()
    # Match against charges from a window widened on the leading edge by
    # day_tolerance: an occurrence at `start` can legitimately have been paid by
    # a charge that posted up to day_tolerance days earlier. candidate_new stays
    # strictly within [start, end] (enforced in _candidate_new) so stale archive
    # history can never surface as a "new" subscription.
    match_start = start - timedelta(days=day_tolerance)
    charges = _charges(transactions, account_index, start=match_start, end=end)

    consumed: set[str] = set()
    expected_missing: list[dict] = []
    # Per-bill keyword token sets (None for an envelope-only bill). The non-None
    # sets are reused as `tracked_token_sets` to keep a tracked subscription from
    # resurfacing as a "new" candidate. Token-anchored (not raw substring) in
    # BOTH directions so a short keyword like "ATT" neither satisfies nor
    # suppresses an unrelated "BATTERY WORLD" charge.
    bill_token_sets: list[frozenset[str] | None] = []
    tracked_token_sets: list[frozenset[str]] = []
    for bill in config.recurring:
        tokens = frozenset(_merchant_key(bill.match).split()) if bill.match is not None else None
        bill_token_sets.append(tokens)
        if tokens:
            tracked_token_sets.append(tokens)

    for bill, bill_tokens in zip(config.recurring, bill_token_sets):
        last_seen: date | None = None
        for occ in monthly_dates(bill.day, start, end):
            # Greedy earliest-first match: the closest unconsumed charge within
            # tolerance, preferring an exact amount, then least date drift.
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
                    match_tokens=bill_tokens,
                ):
                    continue
                exact = charge.amount_cents == bill.amount_cents
                cand_key = (not exact, drift, charge.tid)
                if best is None or cand_key < best_key:
                    best, best_key = charge, cand_key
            if best is not None:
                consumed.add(best.tid)
                if last_seen is None or best.on > last_seen:
                    last_seen = best.on
                continue
            # No charge matched. Only call it missing once it is genuinely
            # overdue; an occurrence still inside the grace window may post late.
            if (end - occ).days < grace:
                continue
            expected_missing.append(
                {
                    "name": bill.name,
                    "envelope": bill.envelope,
                    "expected_amount": _dollars(bill.amount_cents),
                    "expected_date": occ.isoformat(),
                    "match": bill.match,
                    "last_seen": last_seen.isoformat() if last_seen is not None else None,
                }
            )

    candidate_new = _candidate_new(
        charges,
        consumed=consumed,
        tracked_token_sets=tracked_token_sets,
        min_occurrences=min_occurrences,
        window_start=start,
    )

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "day_tolerance": day_tolerance,
        "min_occurrences": min_occurrences,
        "expected_missing": expected_missing,
        "candidate_new": candidate_new,
        "summary": {
            "tracked": len(config.recurring),
            "missing_occurrences": len(expected_missing),
            "candidates": len(candidate_new),
        },
    }


def _summarize_group(members: list["_Charge"]) -> tuple[list[date], float, str]:
    """Return ``(sorted_dates, median_gap_days, cadence_label)`` for a group."""
    dates = sorted(c.on for c in members)
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    median_gap = _median(gaps) if gaps else 0.0
    return dates, median_gap, _cadence_label(median_gap)


def _recurs(members: list["_Charge"], min_occurrences: int) -> bool:
    """True when a group has enough occurrences AND a non-irregular cadence."""
    if len(members) < min_occurrences:
        return False
    return _summarize_group(members)[2] != "irregular"


def _merge_subset_buckets(
    buckets: dict[tuple[int, frozenset[str]], list["_Charge"]],
) -> dict[tuple[int, frozenset[str]], list["_Charge"]]:
    """Merge a token-set group into its *unique* maximal superset at the same amount.

    The same merchant can yield different identity token sets across charges when
    an auxiliary field (e.g. payee) is populated on only some rows — e.g.
    ``{"netflix"}`` on one charge and ``{"netflix", "com"}`` on another. When a
    subset has exactly one maximal superset, folding it in reunites the merchant.

    A subset that sits under *two or more* incomparable maximal supersets is
    ambiguous generic noise (e.g. a bare ``{"pos","purchase"}`` under both
    ``{"pos","purchase","hulu"}`` and ``{"pos","purchase","disney"}``); it is
    NOT clearly the same merchant as any one of them, so it is left standalone
    rather than folded into one arbitrarily — folding it would inject off-cadence
    dates and could hide that merchant or fake a cadence. Two distinct merchants
    therefore never merge, and neither is contaminated by ambiguous remnants.

    The caller only feeds this the *sub-threshold* buckets (those that do not
    already recur on their own) and only adopts a merge that yields a recurring
    group, so a bucket that already meets the occurrence threshold can never be
    demoted by being folded into an off-cadence superset.
    """
    by_amount: dict[int, list[frozenset[str]]] = {}
    for amount_cents, tokens in buckets:
        by_amount.setdefault(amount_cents, []).append(tokens)

    merged: dict[tuple[int, frozenset[str]], list[_Charge]] = {}
    for amount_cents, token_sets in by_amount.items():
        for tokens in token_sets:
            supersets = [s for s in token_sets if s != tokens and tokens < s]
            # Keep only the maximal supersets (those under no other superset).
            maximal = [s for s in supersets if not any(s < other for other in supersets)]
            root = maximal[0] if len(maximal) == 1 else tokens
            merged.setdefault((amount_cents, root), []).extend(
                buckets[(amount_cents, tokens)]
            )
    return merged


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
    # Bucket surviving charges by (amount, merchant-identity token set). Grouping
    # on the identity tokens (not the display key) means a merchant named only in
    # the payee under a numeric/junk description still groups, and two distinct
    # merchants that merely share a generic description are kept apart.
    buckets: dict[tuple[int, frozenset[str]], list[_Charge]] = {}
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
        # Tested against the charge's merchant-identity tokens (description +
        # payee) so a keyword living in the payee still suppresses it, and
        # token-subset (not raw substring) so only a genuine merchant overlap
        # suppresses it.
        if any(ts <= charge.match_tokens for ts in tracked_token_sets):
            continue
        buckets.setdefault((charge.amount_cents, charge.match_tokens), []).append(charge)

    # Two-phase grouping so that a merge can only ever *help* (defragment a
    # split merchant), never *demote* one. Phase 1: any bucket that already meets
    # the occurrence threshold with a recurring cadence is emitted on its own and
    # is never folded into a superset (folding an off-cadence remnant in could
    # otherwise corrupt its cadence and hide it). Phase 2: only the remaining
    # sub-threshold buckets are subset-merged, and a merged group is kept only if
    # it now recurs — so a one-off remnant that fails to reunite a real merchant
    # simply drops out instead of faking or hiding a candidate.
    groups: dict[tuple[int, frozenset[str]], list[_Charge]] = {}
    pending: dict[tuple[int, frozenset[str]], list[_Charge]] = {}
    for key, members in buckets.items():
        if _recurs(members, min_occurrences):
            groups[key] = members
        else:
            pending[key] = members
    for key, members in _merge_subset_buckets(pending).items():
        if _recurs(members, min_occurrences):
            groups.setdefault(key, []).extend(members)

    candidates: list[dict] = []
    for (amount_cents, tokens), members in groups.items():
        dates, median_gap, cadence = _summarize_group(members)
        merchant_key = " ".join(sorted(tokens))
        candidates.append(
            {
                "merchant": _representative_merchant(members),
                "merchant_key": merchant_key,
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
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    grace_days: int | None = None,
) -> dict:
    """Load the categorized archive, then run the subscription audit.

    ``end`` defaults to today and ``start`` to a year before it so a monthly
    subscription reliably clears ``min_occurrences``. The categorized
    transactions are read from the durable archive (falling back to the JSON
    cache when no archive exists yet).
    """
    from . import store

    end = end or date.today()
    start = start or (end - timedelta(days=DEFAULT_WINDOW_DAYS))

    view = store.load_archive_view()
    return subscription_audit(
        config,
        view["transactions"],
        start=start,
        end=end,
        day_tolerance=day_tolerance,
        amount_tolerance_cents=amount_tolerance_cents,
        min_occurrences=min_occurrences,
        grace_days=grace_days,
    )
