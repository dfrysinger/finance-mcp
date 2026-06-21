"""Allocation audit: did each scheduled transfer actually fire?

The forecast (Piece C) projects the scheduled-transfer calendar *forward*; this
piece looks *backward* and asks, per scheduled occurrence in a window, whether
the planned paycheck->envelope allocation actually happened — on its day, for
its amount. It compares the budget config's ``scheduled_transfers`` against real
money movement and reports each occurrence as on-time, early, late,
wrong-amount, or missing.

Evidence for an actual allocation depends on the transfer kind:

* **Internal** (the scheduled transfer names a source envelope — a paycheck hub
  fanning out to a category envelope): evidence is a *reconciled* transfer link
  (confirmed or inferred) whose debit account maps to the source envelope and
  whose credit account maps to the destination envelope. A transfer the
  reconciler could not confidently pair (a needs-confirm link) is deliberately
  NOT counted as fired — the user resolves that ambiguity in the confirm surface
  first — so a genuinely-ambiguous allocation surfaces here as missing rather
  than being silently credited to the wrong envelope.
* **External** (no source envelope — a direct deposit straight into the
  envelope): evidence is a real credit posted to one of the destination
  envelope's accounts that is not itself a transfer leg.

Matching is greedy and deterministic: occurrences are matched earliest-expected
first, each occurrence consumes at most one actual and each actual is consumed at
most once, an exact-amount actual is preferred over a same-envelope near-date
amount mismatch, and any candidate outside ``day_tolerance`` is left unmatched
(reported missing) rather than force-matched to the wrong month. Money is carried
in integer cents throughout and only rendered to dollars at the report edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .budget_config import BudgetConfig, Envelope, monthly_dates
from .normalize import amount_to_cents

DEFAULT_WINDOW_DAYS = 90
DEFAULT_DAY_TOLERANCE = 7

# A transfer is only treated as fired when the reconciler is confident about the
# pairing. Mirrors burndown's reconciled set (kept in lock-step intentionally).
RECONCILED_STATUSES = frozenset({"confirmed", "inferred"})

STATUS_ON_TIME = "on_time"
STATUS_EARLY = "early"
STATUS_LATE = "late"
STATUS_WRONG_AMOUNT = "wrong_amount"
STATUS_MISSING = "missing"


@dataclass(frozen=True)
class _Actual:
    """One observed allocation, normalized for matching."""

    on: date
    amount_cents: int  # positive magnitude
    source_env: str | None
    dest_env: str | None
    kind: str  # "internal" | "external"
    evidence_ids: tuple[str, ...]


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


def _internal_actuals(
    links: list[dict],
    txn_by_id: dict[str, dict],
    account_index: dict[str, Envelope],
) -> list[_Actual]:
    out: list[_Actual] = []
    for link in links:
        if link.get("status") not in RECONCILED_STATUSES:
            continue
        debit_id, credit_id = link.get("debit_txn_id"), link.get("credit_txn_id")
        if debit_id is None or credit_id is None:
            # A reconciled both-leg link always has both ids; skip defensively
            # rather than invent an envelope for a missing leg.
            continue
        debit, credit = txn_by_id.get(debit_id), txn_by_id.get(credit_id)
        if debit is None or credit is None:
            continue
        # Date the allocation strictly by the destination credit leg — when the
        # money actually landed. A reconciled link always has a dated credit leg
        # (the matcher refuses any transfer lacking a posted date), so requiring
        # it here never drops a real allocation; it only refuses to judge drift
        # off the wrong (source) leg.
        on = _txn_date(credit)
        if on is None:
            continue
        amount = link.get("amount_cents")
        if amount is None:
            parsed = amount_to_cents(credit.get("amount"))
            amount = abs(parsed) if parsed is not None else None
        if amount is None or amount <= 0:
            continue
        out.append(
            _Actual(
                on=on,
                amount_cents=int(amount),
                source_env=_env_name(account_index, debit.get("account_id")),
                dest_env=_env_name(account_index, credit.get("account_id")),
                kind="internal",
                evidence_ids=(debit_id, credit_id),
            )
        )
    return out


def _external_actuals(
    transactions: list[dict],
    account_index: dict[str, Envelope],
    linked_ids: set[str],
) -> list[_Actual]:
    out: list[_Actual] = []
    for txn in transactions:
        tid = txn.get("id")
        if tid is None or tid in linked_ids:
            # A leg of any transfer link is internal movement, never an external
            # direct deposit — leave it for the internal path.
            continue
        if txn.get("is_transfer"):
            # An unreconciled transfer leg the categorizer flagged: not a deposit.
            continue
        cents = amount_to_cents(txn.get("amount"))
        if cents is None or cents <= 0:
            continue  # only inflows (credits) can satisfy an allocation
        dest_env = _env_name(account_index, txn.get("account_id"))
        if dest_env is None:
            continue  # a credit into an unbudgeted account is not an allocation
        on = _txn_date(txn)
        if on is None:
            continue
        out.append(
            _Actual(
                on=on,
                amount_cents=cents,
                source_env=None,
                dest_env=dest_env,
                kind="external",
                evidence_ids=(tid,),
            )
        )
    return out


def _occurrence_status(expected: date, actual: _Actual, expected_cents: int) -> tuple[str, int]:
    drift = (actual.on - expected).days
    if actual.amount_cents != expected_cents:
        return STATUS_WRONG_AMOUNT, drift
    if drift == 0:
        return STATUS_ON_TIME, drift
    return (STATUS_EARLY if drift < 0 else STATUS_LATE), drift


def allocation_audit(
    config: BudgetConfig,
    transactions: list[dict],
    links: list[dict],
    *,
    start: date,
    end: date,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
) -> dict:
    """Audit every scheduled-transfer occurrence in ``[start, end]`` against actuals.

    Pure function: all evidence is supplied (``transactions`` carry ``is_transfer``
    from the categorizer; ``links`` are transfer-link rows). Returns a
    JSON-serializable report with one block per scheduled transfer, each listing
    its expected occurrences and the matched actual (if any).
    """
    if end < start:
        raise ValueError(
            f"allocation window is empty: end ({end}) is before start ({start})"
        )
    if day_tolerance < 0:
        raise ValueError(
            f"day_tolerance must be non-negative, got {day_tolerance}"
        )

    txn_by_id = {t["id"]: t for t in transactions if t.get("id") is not None}
    account_index = config.account_index()

    linked_ids: set[str] = set()
    for link in links:
        for col in ("debit_txn_id", "credit_txn_id"):
            value = link.get(col)
            if value is not None:
                linked_ids.add(value)

    internal = _internal_actuals(links, txn_by_id, account_index)
    external = _external_actuals(transactions, account_index, linked_ids)
    consumed_internal: set[int] = set()
    consumed_external: set[int] = set()

    # Flatten to occurrences and match earliest-expected first so an earlier
    # month claims its actual before a later month can contend for it.
    occurrences: list[tuple[date, int]] = []
    for index, sched in enumerate(config.scheduled_transfers):
        for due in monthly_dates(sched.day, start, end):
            occurrences.append((due, index))
    occurrences.sort(key=lambda item: (item[0], item[1]))

    per_transfer: list[list[dict]] = [[] for _ in config.scheduled_transfers]

    for due, index in occurrences:
        sched = config.scheduled_transfers[index]
        is_internal = sched.from_envelope is not None
        pool = internal if is_internal else external
        consumed = consumed_internal if is_internal else consumed_external

        candidates: list[tuple[int, _Actual]] = []
        for i, actual in enumerate(pool):
            if i in consumed:
                continue
            if actual.dest_env != sched.to_envelope:
                continue
            if is_internal and actual.source_env != sched.from_envelope:
                continue
            if abs((actual.on - due).days) > day_tolerance:
                continue
            candidates.append((i, actual))

        # Prefer an exact-amount actual, then the nearest date, then a stable
        # evidence-id tiebreak so the choice is deterministic across runs.
        def sort_key(item: tuple[int, _Actual]) -> tuple:
            _, actual = item
            exact = 0 if actual.amount_cents == sched.amount_cents else 1
            return (exact, abs((actual.on - due).days), actual.evidence_ids)

        candidates.sort(key=sort_key)

        if candidates:
            chosen_i, chosen = candidates[0]
            consumed.add(chosen_i)
            status, drift = _occurrence_status(due, chosen, sched.amount_cents)
            occ = {
                "expected_date": due.isoformat(),
                "status": status,
                "drift_days": drift,
                "expected_amount": _dollars(sched.amount_cents),
                "actual_date": chosen.on.isoformat(),
                "actual_amount": _dollars(chosen.amount_cents),
                "kind": chosen.kind,
                "evidence_ids": list(chosen.evidence_ids),
            }
        else:
            occ = {
                "expected_date": due.isoformat(),
                "status": STATUS_MISSING,
                "drift_days": None,
                "expected_amount": _dollars(sched.amount_cents),
                "actual_date": None,
                "actual_amount": None,
                "kind": "internal" if is_internal else "external",
                "evidence_ids": [],
            }
        per_transfer[index].append(occ)

    transfers_out = []
    summary = {
        STATUS_ON_TIME: 0,
        STATUS_EARLY: 0,
        STATUS_LATE: 0,
        STATUS_WRONG_AMOUNT: 0,
        STATUS_MISSING: 0,
    }
    for index, sched in enumerate(config.scheduled_transfers):
        occ_list = sorted(per_transfer[index], key=lambda o: o["expected_date"])
        for occ in occ_list:
            summary[occ["status"]] += 1
        transfers_out.append(
            {
                "name": sched.name,
                "to_envelope": sched.to_envelope,
                "from_envelope": sched.from_envelope,
                "kind": "internal" if sched.from_envelope is not None else "external",
                "amount": _dollars(sched.amount_cents),
                "occurrences": occ_list,
            }
        )

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "day_tolerance": day_tolerance,
        "transfers": transfers_out,
        "summary": summary,
    }


def _dollars(cents: int) -> str:
    """Render integer cents as a fixed two-decimal dollar string."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def allocation_report(
    config: BudgetConfig,
    *,
    start: date | None = None,
    end: date | None = None,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
) -> dict:
    """Load the categorized archive + transfer links, then run the allocation audit.

    ``end`` defaults to today and ``start`` to a fixed lookback before it. The
    categorized transactions and the transfer links are read from the same
    durable archive (``home_dir()/archive.db``) so the actuals and the link
    statuses describe one consistent snapshot; when no archive exists yet the
    transactions fall back to the JSON cache and the link set is empty.
    """
    from . import archive, config as app_config, store

    end = end or date.today()
    start = start or (end - timedelta(days=DEFAULT_WINDOW_DAYS))

    view = store.load_archive_view()
    db_path = app_config.home_dir() / "archive.db"
    if db_path.exists():
        conn = archive.connect(db_path)
        try:
            links = archive.load_transfer_links(conn)
        finally:
            conn.close()
    else:
        links = []

    return allocation_audit(
        config,
        view["transactions"],
        links,
        start=start,
        end=end,
        day_tolerance=day_tolerance,
    )
