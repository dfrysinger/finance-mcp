"""MCP server exposing the SimpleFIN-backed transaction cache to Copilot.

Runs over stdio. Read tools (`list_accounts`, `get_transactions`,
`account_balances`, `spending_summary`) serve from the local cache and never hit
the network. Budgeting tools (`budget_burndown`, `budget_forecast`,
`allocation_audit_report`, `subscription_audit_report`) and transfer tools
(`reconcile_transfers`, `list_transfers`, `confirm_transfer`) read the durable
archive. `sync_now` is the only tool that reaches out to SimpleFIN.
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
    exclude_income: bool = True,
) -> dict[str, Any]:
    """Aggregate net spending over a date range, grouped for budgeting.

    ``group_by`` is ``category`` (default), ``account``, ``org``, ``month``, or
    ``envelope``. Categories come from a local rules engine plus manual
    overrides. Internal transfers and income (payroll, benefits, investment
    income) are excluded by default so the totals reflect real spending; set
    ``exclude_transfers=False`` or ``exclude_income=False`` to include them.

    Per group, ``outflow`` is spend, ``inflow`` is refunds/returns that net
    against it, and ``unclassified_inflow`` is positive amounts with no spending
    category (surfaced but not netted). ``net`` is outflow plus refunds.

    ``envelope`` rolls spend up by the configured budget envelope that owns each
    account (so spend on a non-envelope account such as a loan or brokerage falls
    into an ``(unmapped)`` bucket). It needs at least one envelope in the budget
    config; with none configured it returns an error rather than an empty view.
    """
    from . import budget_config

    envelope_index: dict[str, str] | None = None
    if group_by == "envelope":
        try:
            cfg = _load_budget_config()
        except budget_config.BudgetConfigError as exc:
            return {"ok": False, "error": str(exc)}
        if not cfg.envelopes:
            return {
                "ok": False,
                "error": (
                    "group_by='envelope' needs envelopes in the budget config, "
                    "but none are defined"
                ),
            }
        envelope_index = {
            acct: env.name for acct, env in cfg.account_index().items()
        }

    cache = store.load_archive_view()
    return queries.spending_summary(
        cache["transactions"],
        group_by=group_by,
        start_date=start_date,
        end_date=end_date,
        include_pending=include_pending,
        exclude_transfers=exclude_transfers,
        exclude_income=exclude_income,
        envelope_index=envelope_index,
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
    account_id: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    day_min: int | None = None,
    day_max: int | None = None,
    match_mode: str = "substring",
) -> dict[str, Any]:
    """Add a category rule: a merchant match (+ optional predicates) -> category.

    ``field`` is ``description``, ``payee``, or ``any``. ``priority`` is
    lowest-wins. Set ``is_transfer=True`` for internal transfers / card payments
    so they are excluded from spending totals. Set ``account_id`` to scope the
    rule to a single account (so a generic descriptor like "FUNDS TRAN" can be
    reclassified on one account without affecting the same text elsewhere);
    leave it ``None`` to apply to every account.

    The merchant match defaults to a case-insensitive substring; set
    ``match_mode='regex'`` to match ``pattern`` as a case-insensitive regular
    expression instead (useful when a store number splits a merchant name).
    Optional predicates further narrow a rule and must all hold to match:
    ``amount_min``/``amount_max`` bound the amount magnitude (``abs(amount)``,
    so 200-350 matches a $304 charge), and ``day_min``/``day_max`` bound the
    posted day-of-month (1-31) — together they isolate a recurring charge like a
    mid-month insurance premium from other charges at the same merchant.
    """
    conn = archive.connect()
    try:
        rule_id = categories.add_rule(
            conn, pattern, category,
            field=field, is_transfer=is_transfer, priority=priority,
            account_id=account_id,
            amount_min=amount_min, amount_max=amount_max,
            day_min=day_min, day_max=day_max, match_mode=match_mode,
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


def _load_budget_config() -> Any:
    """Load the budget config from its default path, raising BudgetConfigError."""
    from . import budget_config

    return budget_config.load_config(config.budget_config_path())


def _parse_iso(value: str | None):
    from datetime import date

    return date.fromisoformat(value) if value else None


@mcp.tool()
def budget_burndown(month: str) -> dict[str, Any]:
    """Per-envelope planned target vs. actual spend for one ``YYYY-MM`` month.

    Reads the budget config and the categorized archive. Returns each envelope's
    target, actual spend, and remaining (negative = over budget), plus unmapped
    spend on accounts in no envelope so nothing is silently dropped.
    """
    from . import budget_config, burndown

    try:
        year, mon = (int(p) for p in month.split("-"))
    except (ValueError, TypeError):
        return {"ok": False, "error": f"month must be YYYY-MM, got {month!r}"}
    try:
        cfg = _load_budget_config()
    except budget_config.BudgetConfigError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return burndown.burndown_report(cfg, year=year, month=mon)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def budget_forecast(
    as_of: str | None = None, through: str | None = None
) -> dict[str, Any]:
    """Per-envelope sufficiency over a window: will each cover its upcoming bills.

    Dates are YYYY-MM-DD. ``as_of`` defaults to today and ``through`` to 60 days
    later. Each envelope gets a verdict (``ok`` / ``at_risk`` / ``balance_unknown``)
    with the projected minimum balance and, when at risk, the date and shortfall.
    """
    from datetime import date

    from . import budget_config, forecast

    try:
        start = _parse_iso(as_of) or date.today()
        end = _parse_iso(through) or forecast.default_through(start)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid date: {exc}"}
    if end < start:
        return {"ok": False, "error": f"through {end} is before as_of {start}"}
    try:
        cfg = _load_budget_config()
    except budget_config.BudgetConfigError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return forecast.forecast_report(cfg, as_of=start, through=end)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def allocation_audit_report(
    start: str | None = None,
    end: str | None = None,
    day_tolerance: int = 7,
) -> dict[str, Any]:
    """Audit each scheduled transfer: did it fire on time, late, early, or not at all.

    Dates are YYYY-MM-DD; ``end`` defaults to today and ``start`` to a year back.
    ``day_tolerance`` is how far a transfer may drift and still count as fired. A
    genuinely-ambiguous allocation surfaces as ``missing`` until its transfer link
    is confirmed via ``confirm_transfer``.
    """
    from . import allocation, budget_config

    try:
        s, e = _parse_iso(start), _parse_iso(end)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid date: {exc}"}
    try:
        cfg = _load_budget_config()
    except budget_config.BudgetConfigError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return allocation.allocation_report(cfg, start=s, end=e, day_tolerance=day_tolerance)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def red_flags_report(as_of: str | None = None) -> dict[str, Any]:
    """Loud alerts for debt payments that were returned or missed.

    ``as_of`` is YYYY-MM-DD, defaulting to today. Audits each configured debt
    account (loans, mortgages, financed purchases) directly from its own
    transactions, so a payment is caught no matter which account funded it. A
    returned payment (posted then reversed) and a month whose payments net to
    zero or less (none posted, or fully reversed) each surface as a red flag; the
    payment amount is never compared, so any positive net counts as paid. A debt
    that syncs only a balance surfaces as an explicit ``unauditable`` note rather
    than a silent gap. With no ``debt_accounts`` configured the report is simply
    empty.
    """
    from datetime import date

    from . import budget_config, redflags

    try:
        a = _parse_iso(as_of) or date.today()
    except ValueError as exc:
        return {"ok": False, "error": f"invalid date: {exc}"}
    try:
        cfg = _load_budget_config()
    except budget_config.BudgetConfigError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return redflags.red_flags_report(cfg, as_of=a)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def subscription_audit_report(
    start: str | None = None,
    end: str | None = None,
    day_tolerance: int = 7,
    min_occurrences: int = 3,
) -> dict[str, Any]:
    """Flag tracked bills that did not post (billing problem / cancellation) and
    surface untracked recurring merchants as candidates for the assistant to judge.

    Dates are YYYY-MM-DD; ``end`` defaults to today and ``start`` to a year back so
    a monthly charge clears ``min_occurrences``. ``tracked`` is the full roster of
    configured recurring bills with each one's amount, due day, last-seen date, and
    next due date. ``expected_missing`` is the deterministic high-stakes alert;
    ``candidate_new`` is advisory — the assistant decides which candidates are real
    subscriptions.
    """
    from . import budget_config, subscription

    try:
        s, e = _parse_iso(start), _parse_iso(end)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid date: {exc}"}
    # Subscriptions degrade gracefully: with no budget config there are no
    # tracked bills, but the audit still surfaces every untracked recurring
    # merchant it finds, so the view shows all detected subscriptions by default.
    cfg_path = config.budget_config_path()
    if cfg_path.exists():
        try:
            cfg = budget_config.load_config(cfg_path)
        except budget_config.BudgetConfigError as exc:
            return {"ok": False, "error": str(exc)}
    else:
        cfg = budget_config.BudgetConfig(
            version=budget_config.SUPPORTED_VERSION, envelopes=()
        )
    try:
        return subscription.subscription_report(
            cfg, start=s, end=e,
            day_tolerance=day_tolerance, min_occurrences=min_occurrences,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def subscriptions_detect(
    start: str | None = None,
    end: str | None = None,
    min_occurrences: int = 3,
    day_tolerance: int = 7,
) -> dict[str, Any]:
    """Detect recurring charges from history and save them as tracked bills.

    Scans the archive for merchants with repeated, same-amount, monthly-spaced
    debits and writes each as a ``recurring`` bill in the budget config (creating
    the config if absent), so your subscriptions become a saved list rather than
    something re-inferred on every audit. Idempotent: a merchant already tracked
    is skipped. Dates are YYYY-MM-DD; ``end`` defaults to today and ``start`` to
    a year back. ``day_tolerance`` (default 7) is the day-of-month drift allowed
    when deciding whether a charge is already covered by an existing bill.
    Weekly/yearly merchants are reported under ``unsupported_cadence`` (only
    monthly bills are tracked). Monthly merchants that could not be auto-tracked
    — text too generic/variable to pin, or a recurring charge at a different
    price from an already-tracked subscription — are reported under
    ``needs_review`` (each with a ``reason``) rather than written.
    """
    from datetime import date

    from . import budget_config, store, subscription

    try:
        s, e = _parse_iso(start), _parse_iso(end)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid date: {exc}"}
    e = e or date.today()
    s = s or subscription.default_start(e)
    if e < s:
        return {"ok": False, "error": f"end {e} is before start {s}"}
    try:
        view = store.load_archive_view()
        cfg_path = config.budget_config_path()
        existing_cfg = (
            budget_config.load_config(cfg_path) if cfg_path.exists() else None
        )
        detected = subscription.detect_subscriptions(
            view["transactions"], start=s, end=e, min_occurrences=min_occurrences,
            day_tolerance=day_tolerance, config=existing_cfg,
        )
        summary = subscription.merge_subscriptions_into_file(
            cfg_path, detected["bills"]
        )
    except (ValueError, budget_config.BudgetConfigError) as exc:
        return {"ok": False, "error": str(exc)}
    summary["ok"] = True
    summary["unsupported_cadence"] = [
        sk for sk in detected["skipped"] if sk.get("kind") == "unsupported_cadence"
    ]
    summary["needs_review"] = [
        sk for sk in detected["skipped"] if sk.get("kind") == "needs_review"
    ]
    summary["window"] = {"start": s.isoformat(), "end": e.isoformat()}
    return summary


@mcp.tool()
def subscriptions_mark(
    name: str,
    lifecycle: str,
    cancel_effective: str | None = None,
    variable: bool | None = None,
) -> dict[str, Any]:
    """Mark a tracked recurring bill as canceling, canceled, or active again.

    The cancellation watch: after you cancel (or try to cancel) a subscription,
    record it here so the audit stops reporting its expected charges as missing
    and instead warns you if it charges again. ``lifecycle`` is ``canceling`` (a
    cancellation was attempted but not confirmed), ``canceled`` (confirmed), or
    ``active`` (reactivate a bill you'd marked). ``cancel_effective`` is the
    YYYY-MM-DD date the cancellation takes effect and is required for canceling
    and canceled — any matching charge on or after it is surfaced as the bill
    "coming back". Omit it when reactivating. ``variable`` optionally flags the
    bill as one whose amount changes every cycle (a usage-based or escrow bill):
    ``true`` matches it by merchant and date regardless of amount and reports the
    actual charged amount, ``false`` restores exact-amount matching, ``null``
    leaves the setting unchanged. The bill is found by ``name``
    (case-insensitive); the name must match exactly one bill.
    """
    from . import budget_config, subscription

    try:
        result = subscription.set_bill_lifecycle(
            config.budget_config_path(), name, lifecycle,
            cancel_effective=cancel_effective,
            variable=variable,
        )
    except budget_config.BudgetConfigError as exc:
        return {"ok": False, "error": str(exc)}
    result["ok"] = True
    return result


@mcp.tool()
def reconcile_transfers() -> dict[str, Any]:
    """Rebuild internal-transfer links from the archive (idempotent).

    Re-runs the matcher over the categorized archive and persists the links,
    preserving every confirmed link and recomputing the rest. Returns counts of
    inferred / needs-confirm / unmatched links plus promotions and downgrades.
    Run this after a sync so ``list_transfers`` reflects the latest data.
    """
    from . import reconcile

    return reconcile.reconcile()


@mcp.tool()
def list_transfers(status: str | None = None) -> dict[str, Any]:
    """List reconstructed transfer links as ``from_account -> to_account $amount [why]``.

    The raw feed names only the product type a transfer went to, never the named
    account; each link here recovers the hidden counterparty and records why it was
    drawn. ``status`` optionally restricts to one lifecycle state (``confirmed`` /
    ``inferred`` / ``unconfirmed`` / ``unmatched``); ``unconfirmed`` links are the
    ones awaiting the user's review and sort first.
    """
    from . import reconcile

    return reconcile.transfers_view(status=status)


@mcp.tool()
def confirm_transfer(link_id: int) -> dict[str, Any]:
    """Confirm one transfer link by id, locking the pairing as authoritative.

    A confirmed link is excluded from every future reconcile, so the user's
    decision is never silently recomputed. Only a two-leg link can be confirmed;
    confirming an unmatched single leg is rejected. Returns the updated link or an
    error.
    """
    from . import reconcile

    try:
        link = reconcile.confirm(link_id)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "link": link}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
