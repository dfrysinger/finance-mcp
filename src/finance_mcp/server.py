"""MCP server exposing the SimpleFIN-backed transaction cache to Copilot.

Runs over stdio. Read tools (`list_accounts`, `get_transactions`,
`account_balances`, `spending_summary`) serve from the local cache and never hit
the network. `sync_now` is the only tool that reaches out to SimpleFIN.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import client, config, queries, store, sync

mcp = FastMCP("finance-mcp")


@mcp.tool()
def list_accounts() -> dict[str, Any]:
    """List cached accounts with balances and the institution each belongs to."""
    cache = store.load_cache()
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
    cache = store.load_cache()
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
    min_amount: float | None = None,
    max_amount: float | None = None,
    include_pending: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """Query cached transactions.

    Dates are YYYY-MM-DD. Amounts are signed (negative = money out), so use
    ``max_amount=0`` for spending only or ``min_amount=0`` for income only.
    Returns the matching transactions plus the total match count.
    """
    cache = store.load_cache()
    rows = queries.filter_transactions(
        cache["transactions"],
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
        search=search,
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
    group_by: str = "account",
    start_date: str | None = None,
    end_date: str | None = None,
    include_pending: bool = True,
) -> dict[str, Any]:
    """Aggregate inflow/outflow over a date range.

    ``group_by`` is ``account``, ``org``, or ``month``. SimpleFIN provides no
    spending categories, so grouping is by real fields only.
    """
    cache = store.load_cache()
    return queries.spending_summary(
        cache["transactions"],
        group_by=group_by,
        start_date=start_date,
        end_date=end_date,
        include_pending=include_pending,
    )


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
