"""Envelope burn-down: planned monthly target vs. actual spend, per envelope.

This is the first envelope-budgeting surface that sits on top of the archive,
the categorizer, and the transfer reconciler. For one calendar month it answers
the priority-one question: *am I over- or under-spending each envelope?*

Design decisions worth knowing:

- **Spend is outflow, not net of refunds.** Headline ``actual_spend`` is the sum
  of an envelope's non-transfer *outflows* (the locked design's definition).
  Refunds and net-of-refund spend are reported alongside it as separate fields,
  never folded into the headline — netting a stray credit (a reimbursement, a
  miscategorized deposit) into spend would *under*report and quietly hide a
  budget overrun.
- **Integer cents, never floats.** Every amount is parsed from the
  authoritative decimal *string* into integer cents (see
  :func:`finance_mcp.normalize.amount_to_cents`). A float target of ``750.0``
  could compare ``> 750.00`` and flag a false overrun; integer cents cannot.
- **Transfers are excluded two ways, and both are counted.** A transaction is
  not spend if the categorizer flagged it ``is_transfer`` *or* it is a leg of a
  reconciled transfer link. The reconcile pass that populates those links is a
  later piece, so today exclusion comes mostly from the category flag; the
  report surfaces both counts so the user can see which mechanism fired.
- **Nothing is silently dropped.** Outflow on an account that is in *no*
  envelope is surfaced in an ``unmapped`` bucket, so spend never vanishes just
  because the budget config is incomplete.
- **Month by posted date, not UTC clock.** Transactions are bucketed by the
  bank's posted *date* string (``YYYY-MM``), matching the rest of the tool and
  sidestepping any timezone/DST boundary error.
"""

from __future__ import annotations

import sqlite3

from .budget_config import BudgetConfig, Envelope
from .normalize import amount_to_cents

# Positive credits in these categories are income/allocations, not purchase
# refunds, so they must not reduce an envelope's reported refunds total.
INCOME_CATEGORIES = frozenset({"Income", "Investment Income"})

# Only fully reconciled transfer links isolate spend reliably. An ``unmatched``
# single leg is a transfer the matcher could not pair, so excluding it could
# hide real spend; we leave those in spend (and the category flag still applies).
RECONCILED_STATUSES = frozenset({"confirmed", "inferred"})


def _txn_cents(txn: dict) -> int | None:
    """Signed integer cents for a transaction, from its authoritative amount.

    Uses the decimal-string ``amount`` when present: its parse is final, so an
    unparseable or sub-cent string yields ``None`` (the row is then surfaced as
    a data-quality skip, never silently rounded into spend). Only a legacy row
    with no authoritative string at all falls back to ``amount_float``, and that
    float is routed through the same exact parser so a non-finite or sub-cent
    value is likewise rejected rather than rounded.
    """
    amt = txn.get("amount")
    if amt is not None and str(amt).strip() != "":
        return amount_to_cents(amt)
    raw_float = txn.get("amount_float")
    if raw_float is not None:
        return amount_to_cents(str(raw_float))
    return None


def _is_transfer(txn: dict, reconciled_leg_ids: frozenset[str]) -> str | None:
    """Why this txn is a transfer (``"category"`` / ``"link"``), or None."""
    if txn.get("is_transfer"):
        return "category"
    if txn.get("id") in reconciled_leg_ids:
        return "link"
    return None


def _cents(amount: int) -> float:
    return round(amount / 100, 2)


def _blank_bucket() -> dict[str, int]:
    return {
        "outflow_cents": 0,
        "refunds_cents": 0,
        "txn_count": 0,
        "excluded_by_category": 0,
        "excluded_by_link": 0,
    }


def _accrue(bucket: dict[str, int], txn: dict, reconciled_leg_ids: frozenset[str]) -> None:
    """Fold one in-month transaction into a spend bucket.

    ``bucket`` carries integer-cent running totals. Transfers are tallied (so
    the report can show how they were excluded) but never added to spend.
    """
    transfer = _is_transfer(txn, reconciled_leg_ids)
    if transfer == "category":
        bucket["excluded_by_category"] += 1
        return
    if transfer == "link":
        bucket["excluded_by_link"] += 1
        return

    cents = _txn_cents(txn)
    if cents is None or cents == 0:
        return
    if cents < 0:
        bucket["outflow_cents"] += -cents
        bucket["txn_count"] += 1
    else:
        if (txn.get("category") or "") not in INCOME_CATEGORIES:
            bucket["refunds_cents"] += cents


def _envelope_result(env: Envelope, bucket: dict[str, int]) -> dict:
    outflow = bucket["outflow_cents"]
    refunds = bucket["refunds_cents"]
    actual = outflow  # headline spend = outflow (refunds reported separately)
    target = env.monthly_target_cents

    result: dict = {
        "envelope": env.name,
        "accounts": list(env.accounts),
        "role": env.role,
        "monthly_target": _cents(target) if target is not None else None,
        "outflow": _cents(outflow),
        "refunds": _cents(refunds),
        "actual_spend": _cents(actual),
        "net_spend": _cents(actual - refunds),
        "txn_count": bucket["txn_count"],
        "excluded_by_category": bucket["excluded_by_category"],
        "excluded_by_link": bucket["excluded_by_link"],
    }
    if target is None:
        result["remaining"] = None
        result["over_budget"] = None
        result["pct_used"] = None
    else:
        result["remaining"] = _cents(target - actual)
        result["over_budget"] = actual > target
        result["pct_used"] = round(100 * actual / target, 1) if target > 0 else None
    return result


def burndown(
    transactions: list[dict],
    config: BudgetConfig,
    *,
    year: int,
    month: int,
    reconciled_leg_ids: frozenset[str] = frozenset(),
) -> dict:
    """Compute per-envelope target-vs-spend for one month. Pure function.

    ``transactions`` are categorized archive rows (carrying ``category`` and
    ``is_transfer``). ``reconciled_leg_ids`` is the set of transaction ids that
    are legs of reconciled transfer links. Returns a JSON-serializable report.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    month_key = f"{year:04d}-{month:02d}"

    index = config.account_index()
    env_buckets: dict[str, dict[str, int]] = {env.name: _blank_bucket() for env in config.envelopes}
    unmapped: dict[str, dict] = {}

    amount_missing = 0

    for txn in transactions:
        posted = txn.get("posted")
        # An undated row cannot be placed in any month, so — exactly like a row
        # that belongs to a different month — it falls outside this single-month
        # report and is skipped without a per-month counter. Counting it would
        # inflate the reported month's diagnostics with archive-wide rows whose
        # only fault is that they carry no date.
        if not isinstance(posted, str) or len(posted) < 7:
            continue
        if posted[:7] != month_key:
            continue

        # A still-counted spend row needs a usable amount; a transfer is
        # classified before the amount is read, so a transfer with a missing
        # amount is excluded (not miscounted as a data error).
        if _is_transfer(txn, reconciled_leg_ids) is None and _txn_cents(txn) is None:
            amount_missing += 1
            continue

        acct = txn.get("account_id")
        env = index.get(acct) if acct is not None else None
        if env is not None:
            _accrue(env_buckets[env.name], txn, reconciled_leg_ids)
        else:
            key = acct if acct is not None else "(no account id)"
            bucket = unmapped.setdefault(
                key,
                {"account_id": acct, "account_name": txn.get("account_name"), **_blank_bucket()},
            )
            _accrue(bucket, txn, reconciled_leg_ids)

    envelopes_out = [
        _envelope_result(env, env_buckets[env.name]) for env in config.envelopes
    ]

    unmapped_out = []
    total_unmapped = 0
    for bucket in unmapped.values():
        if bucket["outflow_cents"] == 0 and bucket["txn_count"] == 0:
            continue
        total_unmapped += bucket["outflow_cents"]
        unmapped_out.append(
            {
                "account_id": bucket["account_id"],
                "account_name": bucket["account_name"],
                "outflow": _cents(bucket["outflow_cents"]),
                "refunds": _cents(bucket["refunds_cents"]),
                "actual_spend": _cents(bucket["outflow_cents"]),
                "txn_count": bucket["txn_count"],
            }
        )
    unmapped_out.sort(key=lambda b: b["actual_spend"], reverse=True)

    total_target = 0
    total_targeted_spend = 0
    total_untargeted_spend = 0
    for env in config.envelopes:
        outflow = env_buckets[env.name]["outflow_cents"]
        if env.monthly_target_cents is None:
            # A tracked-but-unbudgeted envelope (hub/savings) has no target to
            # burn against, so its spend must not deflate the headline remaining
            # budget; it is surfaced separately instead.
            total_untargeted_spend += outflow
        else:
            total_target += env.monthly_target_cents
            total_targeted_spend += outflow
    n_over = sum(1 for e in envelopes_out if e["over_budget"])

    return {
        "year": year,
        "month": month,
        "period": month_key,
        "envelopes": envelopes_out,
        "unmapped": unmapped_out,
        "totals": {
            "total_target": _cents(total_target),
            "total_actual_spend": _cents(total_targeted_spend),
            "total_untargeted_spend": _cents(total_untargeted_spend),
            "total_remaining": _cents(total_target - total_targeted_spend),
            "total_unmapped_spend": _cents(total_unmapped),
            "envelopes_over_budget": n_over,
        },
        "diagnostics": {
            "amount_missing": amount_missing,
        },
    }


def reconciled_leg_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Transaction ids that are legs of reconciled (confirmed/inferred) links."""
    from . import archive

    ids: set[str] = set()
    for link in archive.load_transfer_links(conn):
        if link.get("status") not in RECONCILED_STATUSES:
            continue
        for col in ("debit_txn_id", "credit_txn_id"):
            tid = link.get(col)
            if tid is not None:
                ids.add(tid)
    return frozenset(ids)


def burndown_report(
    config: BudgetConfig,
    *,
    year: int,
    month: int,
) -> dict:
    """Load the categorized archive view + reconciled legs, then run burn-down.

    The categorized transactions and the reconciled transfer legs are both read
    from the same durable archive (``home_dir()/archive.db``), so the spend rows
    and the transfer-exclusion set always describe one consistent snapshot. When
    no archive exists yet, the view falls back to the JSON cache (which carries
    no transfer links) and the leg set is correspondingly empty.
    """
    from . import archive, config as app_config, store

    view = store.load_archive_view()
    db_path = app_config.home_dir() / "archive.db"
    if db_path.exists():
        conn = archive.connect(db_path)
        try:
            legs = reconciled_leg_ids(conn)
        finally:
            conn.close()
    else:
        legs = frozenset()
    return burndown(
        view["transactions"], config, year=year, month=month, reconciled_leg_ids=legs
    )
