"""Read-side helpers: filter transactions and aggregate spending from the cache.

These operate purely on the normalized cache so they never hit the network.
SimpleFIN does not provide transaction categories, so summaries group by fields
that actually exist (account, institution, month) — no invented categories.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .categories import UNCATEGORIZED


def _parse_date(value: str | None, *, end_of_day: bool = False) -> int | None:
    """Parse a YYYY-MM-DD (or ISO) date string into a UTC Unix timestamp.

    When ``end_of_day`` is set and the value is a bare calendar date (no time
    component), the returned timestamp is the last second of that day, so a
    date-only upper bound is inclusive of every transaction posted on that day
    regardless of its intra-day time (rows carry a second-level ``posted_ts``,
    e.g. midday for the SimpleFIN archive).
    """
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            if end_of_day and fmt == "%Y-%m-%d":
                ts += 86_400 - 1  # extend to 23:59:59 of that day (inclusive)
            return ts
        except ValueError:
            continue
    return None


def filter_transactions(
    transactions: list[dict],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: str | None = None,
    search: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    include_pending: bool = True,
    category: str | None = None,
    include_transfers: bool = True,
    include_income: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """Return transactions matching all supplied filters (AND semantics)."""
    start_ts = _parse_date(start_date)
    end_ts = _parse_date(end_date, end_of_day=True)
    needle = search.lower().strip() if search else None
    want_cat = category.strip().lower() if category else None

    out: list[dict] = []
    for txn in transactions:
        if not include_pending and txn.get("pending"):
            continue
        if not include_transfers and txn.get("is_transfer"):
            continue
        if not include_income and txn.get("is_income"):
            continue
        if want_cat is not None and (txn.get("category") or "").lower() != want_cat:
            continue
        ts = txn.get("posted_ts")
        if start_ts is not None and (ts is None or ts < start_ts):
            continue
        if end_ts is not None and (ts is None or ts > end_ts):
            continue
        if account_id is not None and txn.get("account_id") != account_id:
            continue
        amt = txn.get("amount_float")
        if min_amount is not None and (amt is None or amt < min_amount):
            continue
        if max_amount is not None and (amt is None or amt > max_amount):
            continue
        if needle is not None:
            haystack = " ".join(
                str(txn.get(field) or "")
                for field in ("description", "payee", "memo", "account_name", "org")
            ).lower()
            if needle not in haystack:
                continue
        out.append(txn)

    if limit is not None:
        out = out[:limit]
    return out


def _month_key(txn: dict) -> str:
    iso = txn.get("posted")
    return iso[:7] if isinstance(iso, str) and len(iso) >= 7 else "unknown"


_GROUPERS = {
    "account": lambda t: t.get("account_name") or t.get("account_id") or "unknown",
    "org": lambda t: t.get("org") or "unknown",
    "month": _month_key,
    "category": lambda t: t.get("category") or "Uncategorized",
}

# Grouping keys that are computed from a real transaction field alone, plus the
# envelope grouping, which additionally needs an account_id -> envelope-name map.
_VALID_GROUP_BY = frozenset(_GROUPERS) | {"envelope"}

# Bucket name for spend on an account that belongs to no configured envelope, so
# envelope-grouped totals never silently drop money (mirrors the burn-down's
# unmapped bucket).
UNMAPPED_ENVELOPE = "(unmapped)"


def spending_summary(
    transactions: list[dict],
    *,
    group_by: str = "account",
    start_date: str | None = None,
    end_date: str | None = None,
    include_pending: bool = True,
    exclude_transfers: bool = True,
    exclude_income: bool = True,
    envelope_index: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate net spending over a date range, grouped by a real field.

    ``group_by`` is one of ``account``, ``org``, ``month``, ``category``, or
    ``envelope``. This view answers "how much did I actually spend per group",
    so it sorts money into three kinds and reports them in separate columns:

    - ``outflow`` — money spent (sum of negative amounts).
    - ``inflow`` — *refunds/returns*: positive amounts that carry a real
      spending category. A return keeps the category of what it refunds, so it
      nets against that group's spend (``net = outflow + inflow``).
    - ``unclassified_inflow`` — positive amounts with no spending category
      (``Uncategorized``). These are surfaced separately and deliberately do
      **not** net against spend: an unclassified deposit is just as likely to be
      unrecognized income as a refund, and letting mystery money reduce spend
      would understate it. Categorize such a row (e.g. tag a return to its
      envelope, or a deposit as income) to move it into ``inflow`` or out of the
      view entirely.

    Internal transfers (``exclude_transfers``) and income
    (``exclude_income`` — payroll, benefits, investment income) are dropped
    before aggregation so neither masks real spending. ``net`` is therefore true
    net spend (outflow minus returns), never inflated by income.

    ``envelope`` grouping requires ``envelope_index`` (a mapping of account id to
    envelope name); a transaction on an account in no envelope is bucketed under
    :data:`UNMAPPED_ENVELOPE` so spend is never silently dropped.
    """
    if group_by not in _VALID_GROUP_BY:
        raise ValueError(f"group_by must be one of {sorted(_VALID_GROUP_BY)}")
    if group_by == "envelope":
        if envelope_index is None:
            raise ValueError("group_by='envelope' requires envelope_index")

        def grouper(t: dict) -> str:
            acct = t.get("account_id")
            name = envelope_index.get(acct) if acct is not None else None
            return name if name is not None else UNMAPPED_ENVELOPE
    else:
        grouper = _GROUPERS[group_by]

    rows = filter_transactions(
        transactions,
        start_date=start_date,
        end_date=end_date,
        include_pending=include_pending,
        include_transfers=not exclude_transfers,
        include_income=not exclude_income,
    )

    groups: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "inflow": 0.0,
            "outflow": 0.0,
            "unclassified_inflow": 0.0,
            "net": 0.0,
            "count": 0,
        }
    )
    total_in = total_out = total_unclassified = 0.0
    skipped = 0
    for txn in rows:
        amt = txn.get("amount_float")
        if amt is None:
            skipped += 1
            continue
        bucket = groups[grouper(txn)]
        bucket["count"] += 1
        if amt < 0:
            bucket["outflow"] += amt
            total_out += amt
            bucket["net"] += amt
        elif (txn.get("category") or UNCATEGORIZED) == UNCATEGORIZED:
            # Positive amount with no spending category: surface it, but do not
            # let it net against spend (it may be unrecognized income).
            bucket["unclassified_inflow"] += amt
            total_unclassified += amt
        else:
            # Positive amount with a real spending category: a refund/return
            # that offsets spend in this group.
            bucket["inflow"] += amt
            total_in += amt
            bucket["net"] += amt

    groups_out = [
        {"group": key, **{k: round(v, 2) for k, v in vals.items()}}
        for key, vals in sorted(groups.items(), key=lambda kv: kv[1]["net"])
    ]
    return {
        "group_by": group_by,
        "start_date": start_date,
        "end_date": end_date,
        "exclude_transfers": exclude_transfers,
        "exclude_income": exclude_income,
        "transaction_count": len(rows),
        "amount_missing_count": skipped,
        "total_inflow": round(total_in, 2),
        "total_outflow": round(total_out, 2),
        "total_unclassified_inflow": round(total_unclassified, 2),
        "net": round(total_in + total_out, 2),
        "groups": groups_out,
    }
