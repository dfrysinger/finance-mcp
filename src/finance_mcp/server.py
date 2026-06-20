"""MCP server exposing the SimpleFIN-backed transaction cache to Copilot.

Runs over stdio. Read tools (`list_accounts`, `get_transactions`,
`account_balances`, `spending_summary`) serve from the local cache and never hit
the network. `sync_now` is the only tool that reaches out to SimpleFIN.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import archive, categories, client, config, queries, store, sync

mcp = FastMCP("finance-mcp")


@mcp.tool()
def list_accounts() -> dict[str, Any]:
    """List cached accounts with balances and the institution each belongs to."""
    cache = store.load_archive_view()
    return {
        "synced_at": cache.get("synced_at"),
        "account_count": len(cache["accounts"]),
        "accounts": cache["accounts"],
        "errors": cache.get("errors", []),
        "errlist": cache.get("errlist", []),
    }


@mcp.tool()
def account_balances() -> dict[str, Any]:
    """Return just the balance and as-of date for each cached account."""
    cache = store.load_archive_view()
    balances = [
        {
            "account_id": a.get("account_id"),
            "account_name": a.get("account_name"),
            "org": a.get("org"),
            "balance": a.get("balance"),
            "currency": a.get("currency"),
            "as_of": a.get("balance_date"),
        }
        for a in cache["accounts"]
    ]
    return {"synced_at": cache.get("synced_at"), "balances": balances}


@mcp.tool()
def get_transactions(
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: str | None = None,
    search: str | None = None,
    category: str | None = None,
    include_transfers: bool = True,
    min_amount: float | None = None,
    max_amount: float | None = None,
    include_pending: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """Query cached transactions.

    Dates are YYYY-MM-DD. Amounts are signed (negative = money out), so use
    ``max_amount=0`` for spending only or ``min_amount=0`` for income only.
    Each transaction carries a derived ``category`` and ``is_transfer`` flag; pass
    ``category`` to filter to one, or ``include_transfers=False`` to drop internal
    transfers and card payments. Returns the matching transactions plus the count.
    """
    cache = store.load_archive_view()
    rows = queries.filter_transactions(
        cache["transactions"],
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
        search=search,
        category=category,
        include_transfers=include_transfers,
        min_amount=min_amount,
        max_amount=max_amount,
        include_pending=include_pending,
        limit=None,
    )
    total = len(rows)
    return {
        "synced_at": cache.get("synced_at"),
        "total_matches": total,
        "returned": min(total, limit),
        "transactions": rows[:limit],
    }


@mcp.tool()
def spending_summary(
    group_by: str = "category",
    start_date: str | None = None,
    end_date: str | None = None,
    include_pending: bool = True,
    exclude_transfers: bool = True,
) -> dict[str, Any]:
    """Aggregate inflow/outflow over a date range, grouped for budgeting.

    ``group_by`` is ``category`` (default), ``account``, ``org``, or ``month``.
    Categories come from a local rules engine plus manual overrides. Internal
    transfers and credit-card payments are excluded by default so the totals
    reflect real spending; set ``exclude_transfers=False`` to include them.
    """
    cache = store.load_archive_view()
    return queries.spending_summary(
        cache["transactions"],
        group_by=group_by,
        start_date=start_date,
        end_date=end_date,
        include_pending=include_pending,
        exclude_transfers=exclude_transfers,
    )


@mcp.tool()
def net_worth_history() -> dict[str, Any]:
    """Total balance across all accounts per as-of date, from the archive.

    Each sync records a balance snapshot, so this builds a net-worth trend over
    time (oldest first). Loan/credit balances are negative, so the total is true
    net worth.
    """
    conn = archive.connect()
    try:
        return {"history": archive.net_worth_history(conn)}
    finally:
        conn.close()


@mcp.tool()
def archive_stats() -> dict[str, Any]:
    """Report archive size and date coverage (transaction count, earliest/latest)."""
    conn = archive.connect()
    try:
        return archive.stats(conn)
    finally:
        conn.close()


@mcp.tool()
def categorization_status() -> dict[str, Any]:
    """Report category coverage and the breakdown of transactions per category."""
    # Go through the read path so the cache fallback (legacy cache.json with no
    # archive.db yet) is reflected instead of reporting 0/0.
    view = store.load_archive_view()
    return categories.coverage_report(view["transactions"])


@mcp.tool()
def list_category_rules() -> dict[str, Any]:
    """List the category rules (pattern -> category) currently in effect."""
    conn = archive.connect()
    try:
        return {"rules": categories.list_rules(conn)}
    finally:
        conn.close()


@mcp.tool()
def add_category_rule(
    pattern: str,
    category: str,
    field: str = "any",
    is_transfer: bool = False,
    priority: int = 100,
) -> dict[str, Any]:
    """Add a category rule: a case-insensitive substring match -> category.

    ``field`` is ``description``, ``payee``, or ``any``. ``priority`` is
    lowest-wins. Set ``is_transfer=True`` for internal transfers / card payments
    so they are excluded from spending totals.
    """
    conn = archive.connect()
    try:
        rule_id = categories.add_rule(
            conn, pattern, category,
            field=field, is_transfer=is_transfer, priority=priority,
        )
        return {"ok": True, "rule_id": rule_id}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


@mcp.tool()
def remove_category_rule(rule_id: int) -> dict[str, Any]:
    """Delete a category rule by its id."""
    conn = archive.connect()
    try:
        return {"ok": categories.remove_rule(conn, rule_id)}
    finally:
        conn.close()


@mcp.tool()
def set_transaction_category(
    txn_id: str,
    category: str,
    is_transfer: bool = False,
) -> dict[str, Any]:
    """Pin a category to a single transaction.

    Manual overrides win over every rule and survive re-syncs, so use this to fix
    a one-off that the rules get wrong.
    """
    conn = archive.connect()
    try:
        categories.set_manual_category(
            conn, txn_id, category, is_transfer=is_transfer
        )
        return {"ok": True, "txn_id": txn_id, "category": category}
    except (LookupError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


@mcp.tool()
def sync_now(days: int = 120) -> dict[str, Any]:
    """Pull fresh data from SimpleFIN into the cache (network call).

    Respect SimpleFIN's ~24 requests/day budget: a sync of >89 days makes one
    request per ~89-day window. Returns a summary including any SimpleFIN errors.
    """
    if config.load_access_url() is None:
        return {
            "ok": False,
            "error": "No SimpleFIN access URL configured. Run `finance-mcp claim` "
            "in a terminal first.",
        }
    try:
        summary = sync.sync(days=days)
    except (RuntimeError, client.SimpleFINError) as exc:
        return {"ok": False, "error": str(exc)}
    summary["ok"] = True
    return summary


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
