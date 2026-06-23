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


def _debt_account_ids() -> frozenset[str]:
    """The set of configured debt/loan account ids, empty if none are configured.

    Read so categorization can pin a debt account's own postings to a transfer
    category instead of letting a lender's "Principal Interest"-style descriptor
    match an income rule. This reads the canonical default budget config, matching
    the rest of the categorization layer: ``load_archive_view`` always categorizes
    against the durable default archive and its rule table, so a report invoked
    with an alternate ``--config`` still categorizes against the canonical archive
    (its config drives only its own projection math, never categorization). Keeping
    one categorization source avoids a half-scoped state where debt pins follow one
    config while transfer/card-payment rules follow another.

    A *missing* budget config legitimately means "no debt accounts" and is silent.
    A *present-but-invalid* config is different: silently dropping the debt accounts
    would reinstate the income inflation the user added them to fix, so we warn (the
    archive read path must still work without a valid budget, so we degrade to no
    debt accounts rather than raising).
    """
    import sys

    from . import budget_config

    path = config.budget_config_path()
    if not path.exists():
        return frozenset()
    try:
        cfg = budget_config.load_config(path)
    except budget_config.BudgetConfigError as exc:
        print(
            f"Warning: budget config at {path} could not be loaded ({exc}); "
            "debt-account postings will not be reclassified and may inflate "
            "income until the config is fixed.",
            file=sys.stderr,
        )
        return frozenset()
    return frozenset(d.account_id for d in cfg.debt_accounts)


def load_archive_view() -> dict:
    """Return the full archive as a cache-shaped dict for the query helpers.

    Prefers the durable SQLite archive (multi-year history); falls back to the
    JSON cache if the archive does not exist yet (e.g. before the first sync on
    this version).
    """
    from . import archive, categories  # local import avoids a circular import

    debt_ids = _debt_account_ids()
    db_path = config.home_dir() / "archive.db"
    if not db_path.exists():
        # No archive yet (e.g. a legacy cache.json from before this version).
        # Serve the cache, but still categorize via a throwaway seeded DB so a
        # transfer-excluding summary stays honest instead of silently counting
        # transfers as spending.
        base = load_cache()
        if base["transactions"]:
            mem = archive.connect(Path(":memory:"))
            try:
                categories.seed_default_rules(mem)
                categories.apply_categories(
                    mem, base["transactions"], debt_account_ids=debt_ids
                )
            finally:
                mem.close()
        return base

    conn = archive.connect(db_path)
    try:
        # Seed the default rule set once (tracked by a meta sentinel) so
        # is_transfer is populated on every read path (CLI and MCP). Without this,
        # spending_summary would claim exclude_transfers while excluding nothing.
        categories.seed_default_rules(conn)
        base = load_cache()
        if archive.is_empty(conn):
            txns = base["transactions"]
            accounts = base["accounts"]
        else:
            txns = archive.load_transactions(conn)
            accounts = archive.load_accounts(conn)
        categories.apply_categories(conn, txns, debt_account_ids=debt_ids)
        return {
            "synced_at": base.get("synced_at"),
            "accounts": accounts,
            "transactions": txns,
            "errors": base.get("errors", []),
            "errlist": base.get("errlist", []),
        }
    finally:
        conn.close()
