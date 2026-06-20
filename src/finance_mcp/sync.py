"""High-level sync: fetch from SimpleFIN (chunked) and update the cache."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import client, config, normalize, store

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
    cache["errors"] = errors
    cache["errlist"] = errlist
    store.save_cache(cache)

    return {
        "synced_at": datetime.now(tz=timezone.utc).isoformat(),
        "days": days,
        "account_count": len(cache["accounts"]),
        "transaction_count": len(transactions),
        "errors": errors,
        "errlist": errlist,
    }
