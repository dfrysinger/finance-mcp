"""High-level sync: fetch from SimpleFIN (chunked) and update the cache."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import archive, client, config, normalize, store

# Stay safely under SimpleFIN's 90-day-per-request ceiling.
_CHUNK_SECONDS = 89 * 24 * 60 * 60


def _date_windows(start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """Split [start, end] into <=89-day windows for the 90-day API limit."""
    windows: list[tuple[int, int]] = []
    cursor = start_ts
    while cursor < end_ts:
        window_end = min(cursor + _CHUNK_SECONDS, end_ts)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows or [(start_ts, end_ts)]


def _hashable(value):
    """Coerce a SimpleFIN error field to a stable hashable value.

    Error fields should be strings per the protocol, but a malformed bridge
    response could send a list or dict. Coercing anything non-primitive to its
    repr keeps deduplication keys hashable so one bad entry cannot crash the
    whole sync before data is cached.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _dedupe_errors(items: list) -> list:
    """Drop duplicate SimpleFIN errors while preserving first-seen order.

    A single persistent problem (e.g. a connection that needs re-auth) is
    returned once per fetch window, so accumulating every window's errors would
    report the same problem several times. Structured errors dedupe on their
    stable identity (code + conn/account id + message); legacy string errors
    dedupe on the string itself.
    """
    seen: set = set()
    out: list = []
    for item in items:
        if isinstance(item, dict):
            key = (
                "dict",
                _hashable(item.get("code")),
                _hashable(item.get("conn_id")),
                _hashable(item.get("account_id")),
                _hashable(item.get("msg")),
            )
        else:
            key = ("str", _hashable(item))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def sync(
    *,
    days: int = 120,
    access_url: str | None = None,
    pending: bool = True,
    now: int | None = None,
) -> dict:
    """Pull the last ``days`` of data into the cache and return a summary.

    Raises ``RuntimeError`` if no access URL is configured. Any SimpleFIN-level
    ``errors``/``errlist`` are stored on the cache and returned so the caller can
    surface them (the protocol requires showing these to the user).
    """
    url = access_url or config.load_access_url()
    if not url:
        raise RuntimeError(
            "No SimpleFIN access URL configured. Run `finance-mcp claim` first."
        )

    end_ts = int(now if now is not None else time.time())
    start_ts = end_ts - days * 24 * 60 * 60

    cache = store.load_cache()
    all_accounts: dict[str, dict] = {a.get("account_id"): a for a in cache["accounts"]}
    transactions = list(cache["transactions"])
    errors: list = []
    errlist: list = []

    for window_start, window_end in _date_windows(start_ts, end_ts):
        raw = client.fetch_accounts(
            url, start_date=window_start, end_date=window_end, pending=pending
        )
        norm = normalize.normalize(raw)
        for account in norm["accounts"]:
            all_accounts[account.get("account_id")] = account
        transactions = normalize.merge_transactions(transactions, norm["transactions"])
        errors.extend(norm["errors"])
        errlist.extend(norm["errlist"])

    cache["accounts"] = list(all_accounts.values())
    cache["transactions"] = transactions
    cache["errors"] = _dedupe_errors(errors)
    cache["errlist"] = _dedupe_errors(errlist)
    store.save_cache(cache)

    # Fold this sync into the durable multi-year archive. The JSON cache already
    # holds the full accumulated set (old + new), and the archive upsert never
    # deletes, so the archive retains history even if the cache is ever reset.
    conn = archive.connect()
    try:
        archive_stats = archive.upsert(conn, {
            "transactions": cache["transactions"],
            "accounts": cache["accounts"],
        })
    finally:
        conn.close()

    return {
        "synced_at": datetime.now(tz=timezone.utc).isoformat(),
        "days": days,
        "account_count": len(cache["accounts"]),
        "transaction_count": len(transactions),
        "archived_transactions": archive_stats["transactions_added"],
        "balance_snapshots_added": archive_stats["balance_snapshots_added"],
        "errors": cache["errors"],
        "errlist": cache["errlist"],
    }
