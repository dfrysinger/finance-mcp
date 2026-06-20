"""Read/write the normalized transaction cache on disk."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config

EMPTY_CACHE: dict = {
    "synced_at": None,
    "accounts": [],
    "transactions": [],
    "errors": [],
    "errlist": [],
}


def load_cache(path: Path | None = None) -> dict:
    """Load the cache, returning an empty structure if it does not exist."""
    path = path or config.cache_path()
    if not path.exists():
        return dict(EMPTY_CACHE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(EMPTY_CACHE)
    if not isinstance(data, dict):
        return dict(EMPTY_CACHE)
    for key, default in EMPTY_CACHE.items():
        data.setdefault(key, default)
    return data


def save_cache(cache: dict, path: Path | None = None) -> Path:
    """Persist the cache with owner-only permissions (it holds transaction data)."""
    path = path or config.cache_path()
    cache = dict(cache)
    cache["synced_at"] = datetime.now(tz=timezone.utc).isoformat()
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def load_archive_view() -> dict:
    """Return the full archive as a cache-shaped dict for the query helpers.

    Prefers the durable SQLite archive (multi-year history); falls back to the
    JSON cache if the archive does not exist yet (e.g. before the first sync on
    this version).
    """
    from . import archive  # local import avoids a circular dependency at import time

    db_path = config.home_dir() / "archive.db"
    if not db_path.exists():
        return load_cache()
    conn = archive.connect(db_path)
    try:
        if archive.is_empty(conn):
            return load_cache()
        base = load_cache()
        return {
            "synced_at": base.get("synced_at"),
            "accounts": archive.load_accounts(conn),
            "transactions": archive.load_transactions(conn),
            "errors": base.get("errors", []),
            "errlist": base.get("errlist", []),
        }
    finally:
        conn.close()
