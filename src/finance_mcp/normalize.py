"""Normalize raw SimpleFIN responses into stable, flat records.

SimpleFIN amounts are signed decimal *strings* (negative = money out). We keep an
exact string form for display and a float for arithmetic, and expose ISO-8601
timestamps alongside the raw Unix seconds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _iso(ts: Any) -> str | None:
    """Convert a Unix-seconds timestamp to a UTC ISO-8601 string."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def _amount_float(amount: Any) -> float | None:
    """Parse a SimpleFIN decimal-string amount into a float."""
    if amount is None:
        return None
    try:
        return float(Decimal(str(amount)))
    except (InvalidOperation, ValueError):
        return None


def normalize_account(account: dict) -> dict:
    """Flatten one SimpleFIN account object (without its transactions)."""
    org = account.get("org") or {}
    return {
        "account_id": account.get("id"),
        "account_name": account.get("name"),
        "org": org.get("name") or org.get("domain"),
        "org_domain": org.get("domain"),
        "currency": account.get("currency"),
        "balance": account.get("balance"),
        "balance_float": _amount_float(account.get("balance")),
        "available_balance": account.get("available-balance"),
        "balance_date": _iso(account.get("balance-date")),
        "balance_date_ts": account.get("balance-date"),
    }


def normalize_transaction(account: dict, txn: dict) -> dict:
    """Flatten one transaction, carrying account/institution context onto it."""
    org = account.get("org") or {}
    posted = txn.get("posted")
    return {
        "id": txn.get("id"),
        "account_id": account.get("id"),
        "account_name": account.get("name"),
        "org": org.get("name") or org.get("domain"),
        "posted": _iso(posted),
        "posted_ts": posted,
        "transacted_at": _iso(txn.get("transacted_at")),
        "amount": txn.get("amount"),
        "amount_float": _amount_float(txn.get("amount")),
        "description": txn.get("description"),
        "payee": txn.get("payee"),
        "memo": txn.get("memo"),
        "pending": bool(txn.get("pending", False)),
        "currency": account.get("currency"),
    }


def normalize(data: dict) -> dict:
    """Turn a raw ``/accounts`` response into normalized accounts + transactions.

    Returns a dict with ``accounts``, ``transactions``, and any ``errors`` /
    ``errlist`` surfaced verbatim so callers can show them to the user.
    """
    accounts_raw = data.get("accounts") or []
    accounts: list[dict] = []
    transactions: list[dict] = []
    for account in accounts_raw:
        accounts.append(normalize_account(account))
        for txn in account.get("transactions") or []:
            transactions.append(normalize_transaction(account, txn))

    transactions.sort(key=lambda t: (t.get("posted_ts") or 0), reverse=True)
    return {
        "accounts": accounts,
        "transactions": transactions,
        "errors": data.get("errors") or [],
        "errlist": data.get("errlist") or [],
    }


def merge_transactions(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merge two transaction lists, de-duplicating by id then re-sorting.

    Later (incoming) records win on id collision so a re-sync can promote a
    pending transaction to posted.
    """
    by_id: dict[str, dict] = {}
    order: list[dict] = []
    for txn in [*existing, *incoming]:
        key = txn.get("id")
        if key is None:
            order.append(txn)
            continue
        by_id[key] = txn
    merged = [*by_id.values(), *order]
    merged.sort(key=lambda t: (t.get("posted_ts") or 0), reverse=True)
    return merged
