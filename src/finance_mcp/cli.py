"""Command-line interface: claim, sync, accounts, transactions, summary."""

from __future__ import annotations

import argparse
import json
import sys

from . import archive, categories, client, config, importer, queries, store, sync


def _cmd_claim(args: argparse.Namespace) -> int:
    token = args.token or input("Paste your SimpleFIN setup token: ").strip()
    if not token:
        print("No setup token provided.", file=sys.stderr)
        return 1
    try:
        access_url = client.claim_setup_token(token)
    except client.SimpleFINError as exc:
        print(f"Claim failed: {exc}", file=sys.stderr)
        return 1
    path = config.save_access_url(access_url)
    print(f"Access URL saved to {path} (mode 0600).")
    print("Next: run `finance-mcp sync` to pull your transactions.")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    try:
        summary = sync.sync(days=args.days, pending=not args.no_pending)
    except (RuntimeError, client.SimpleFINError) as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Synced {summary['transaction_count']} transactions across "
        f"{summary['account_count']} accounts (last {summary['days']} days)."
    )
    _print_errors(summary["errors"], summary["errlist"])
    return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
    cache = store.load_archive_view()
    if args.json:
        print(json.dumps(cache["accounts"], indent=2))
        return 0
    if not cache["accounts"]:
        print("No accounts cached yet. Run `finance-mcp sync`.")
        return 0
    for acct in cache["accounts"]:
        bal = acct.get("balance")
        cur = acct.get("currency") or ""
        print(f"{acct.get('org','?'):<24} {acct.get('account_name','?'):<28} "
              f"{bal:>12} {cur}  (as of {acct.get('balance_date')})")
    _print_errors(cache["errors"], cache["errlist"])
    return 0


def _cmd_transactions(args: argparse.Namespace) -> int:
    cache = store.load_archive_view()
    rows = queries.filter_transactions(
        cache["transactions"],
        start_date=args.start,
        end_date=args.end,
        account_id=args.account,
        search=args.search,
        category=args.category,
        include_transfers=not args.no_transfers,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No matching transactions.")
        return 0
    for txn in rows:
        flag = "P" if txn.get("pending") else " "
        cat = (txn.get("category") or "")[:16]
        print(f"{(txn.get('posted') or '')[:10]} {flag} {txn.get('amount',''):>12} "
              f"{cat:<16} {(txn.get('account_name') or '')[:16]:<16} {txn.get('description','')}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    cache = store.load_archive_view()
    result = queries.spending_summary(
        cache["transactions"],
        group_by=args.group_by,
        start_date=args.start,
        end_date=args.end,
        exclude_transfers=not args.include_transfers,
    )
    print(json.dumps(result, indent=2))
    return 0


def _cmd_networth(args: argparse.Namespace) -> int:
    conn = archive.connect()
    try:
        history = archive.net_worth_history(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(history, indent=2))
        return 0
    if not history:
        print("No balance snapshots yet. Run `finance-mcp sync`.")
        return 0
    for row in history:
        print(f"{row['date']}  {row['total']:>16,.2f}  ({row['account_count']} accounts)")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    conn = archive.connect()
    try:
        print(json.dumps(archive.stats(conn), indent=2))
    finally:
        conn.close()
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    paths = [Path(p).expanduser() for p in args.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"Path not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    summary = importer.import_paths(paths, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    verb = "Would import" if summary["dry_run"] else "Imported"
    print(
        f"{verb} {summary['rows_parsed']} rows from "
        f"{summary['files_imported']} file(s); "
        f"{summary['transactions_added']} new transaction(s) added; "
        f"{summary['rows_skipped']} row(s) skipped."
    )
    for r in summary["results"]:
        skip = f" (-{r['rows_skipped']})" if r.get("rows_skipped") else ""
        print(f"  {r['source'] or '?':9} {r['rows']:>6}{skip}  {r['file']}")
    for w in summary.get("warnings", []):
        print(f"  WARNING: {w['file']} -> {w['reason']}", file=sys.stderr)
    for s in summary["skipped"]:
        print(f"  skipped: {s['file']} ({s['reason']})", file=sys.stderr)
    return 0


def _cmd_categorize(args: argparse.Namespace) -> int:
    conn = archive.connect()
    try:
        seeded = categories.seed_default_rules(conn, force=args.reseed)
    finally:
        conn.close()
    # Read back through the normal path so coverage reflects exactly what queries
    # serve (including the cache fallback when the archive is empty).
    view = store.load_archive_view()
    report = categories.coverage_report(view["transactions"])
    if seeded:
        print(f"Seeded {seeded} default rules.")
    print(f"Coverage: {report['categorized']}/{report['total']} "
          f"({report['coverage_pct']}%) categorized, "
          f"{report['uncategorized']} uncategorized.")
    for cat, n in report["categories"].items():
        print(f"  {cat:<22} {n:>5}")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    conn = archive.connect()
    try:
        if args.action == "add":
            if not args.pattern or not args.category:
                print("add requires --pattern and --category", file=sys.stderr)
                return 1
            try:
                rid = categories.add_rule(
                    conn, args.pattern, args.category,
                    field=args.field, is_transfer=args.transfer, priority=args.priority,
                )
            except ValueError as exc:
                print(f"Could not add rule: {exc}", file=sys.stderr)
                return 1
            print(f"Added rule {rid}: '{args.pattern}' -> {args.category}")
            return 0
        if args.action == "rm":
            if args.rule_id is None:
                print("rm requires --rule-id", file=sys.stderr)
                return 1
            ok = categories.remove_rule(conn, args.rule_id)
            print(f"Removed rule {args.rule_id}." if ok else f"No rule {args.rule_id}.")
            return 0
        rules = categories.list_rules(conn)
        if args.json:
            print(json.dumps(rules, indent=2))
            return 0
        if not rules:
            print("No rules yet. Run `finance-mcp categorize` to seed defaults.")
            return 0
        for r in rules:
            t = " [transfer]" if r["is_transfer"] else ""
            print(f"  {r['rule_id']:>4} p{r['priority']:<4} {r['field']:<11} "
                  f"'{r['pattern']}' -> {r['category']}{t}")
        return 0
    finally:
        conn.close()


def _cmd_set_category(args: argparse.Namespace) -> int:
    conn = archive.connect()
    try:
        categories.set_manual_category(
            conn, args.txn_id, args.category, is_transfer=args.transfer
        )
    except (LookupError, ValueError) as exc:
        print(f"Could not set category: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Set {args.txn_id} -> {args.category}.")
    return 0


def _cmd_uncategorized(args: argparse.Namespace) -> int:
    cache = store.load_archive_view()
    counts: dict[str, dict] = {}
    for txn in cache["transactions"]:
        if txn.get("category") != categories.UNCATEGORIZED:
            continue
        key = txn.get("description") or txn.get("payee") or "(blank)"
        entry = counts.setdefault(key, {"count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += txn.get("amount_float") or 0.0
    ranked = sorted(counts.items(), key=lambda kv: -kv[1]["count"])[: args.limit]
    if args.json:
        print(json.dumps([{"merchant": k, **v} for k, v in ranked], indent=2))
        return 0
    if not ranked:
        print("Nothing uncategorized. 🎉")
        return 0
    print("Top uncategorized merchants (add a rule to cover these):")
    for merchant, v in ranked:
        print(f"  {v['count']:>4}x {v['total']:>12,.2f}  {merchant}")
    return 0


def _parse_month(value: str) -> tuple[int, int]:
    """Parse a ``YYYY-MM`` month string into ``(year, month)``."""
    parts = (value or "").strip().split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"month must be YYYY-MM, got {value!r}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    return year, month


def _cmd_burndown(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import budget_config, burndown

    try:
        year, month = _parse_month(args.month)
    except ValueError as exc:
        print(f"Invalid --month: {exc}", file=sys.stderr)
        return 1
    cfg_path = Path(args.config).expanduser() if args.config else config.budget_config_path()
    try:
        cfg = budget_config.load_config(cfg_path)
    except budget_config.BudgetConfigError as exc:
        print(f"Budget config error: {exc}", file=sys.stderr)
        return 1

    report = burndown.burndown_report(cfg, year=year, month=month)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    t = report["totals"]
    print(f"Burn-down for {report['period']}")
    print(f"  {'Envelope':<22} {'Target':>10} {'Spent':>10} {'Remaining':>11}  Status")
    for e in report["envelopes"]:
        target = f"{e['monthly_target']:.2f}" if e["monthly_target"] is not None else "—"
        if e["over_budget"] is None:
            status = "(no target)"
        elif e["over_budget"]:
            status = f"OVER by {-e['remaining']:.2f}"
        else:
            status = "ok"
        remaining = f"{e['remaining']:.2f}" if e["remaining"] is not None else "—"
        print(f"  {e['envelope']:<22} {target:>10} {e['actual_spend']:>10.2f} "
              f"{remaining:>11}  {status}")
    print(f"  {'TOTAL':<22} {t['total_target']:>10.2f} "
          f"{t['total_actual_spend']:>10.2f} {t['total_remaining']:>11.2f}  "
          f"({t['envelopes_over_budget']} over)")
    if t.get("total_untargeted_spend"):
        print(f"  (untargeted envelope spend, not in the total above: "
              f"{t['total_untargeted_spend']:.2f})")
    if report["unmapped"]:
        print("  Unmapped spend (accounts not in any envelope):")
        for u in report["unmapped"]:
            label = u["account_name"] or u["account_id"] or "(no account)"
            print(f"    {label:<28} {u['actual_spend']:>10.2f} ({u['txn_count']} txns)")
        print(f"    {'total unmapped':<28} {t['total_unmapped_spend']:>10.2f}")
    d = report["diagnostics"]
    if d["amount_missing"]:
        print(f"  (skipped {d['amount_missing']} in-month transactions with an "
              f"unparseable amount)")
    return 0


def _print_errors(errors: list, errlist: list) -> None:
    for err in [*(errors or []), *(errlist or [])]:
        print(f"  ! SimpleFIN: {err}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finance-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_claim = sub.add_parser("claim", help="exchange a SimpleFIN setup token")
    p_claim.add_argument("token", nargs="?", help="setup token (else prompt)")
    p_claim.set_defaults(func=_cmd_claim)

    p_sync = sub.add_parser("sync", help="pull transactions into the cache")
    p_sync.add_argument("--days", type=int, default=120)
    p_sync.add_argument("--no-pending", action="store_true")
    p_sync.set_defaults(func=_cmd_sync)

    p_acct = sub.add_parser("accounts", help="list cached accounts + balances")
    p_acct.add_argument("--json", action="store_true")
    p_acct.set_defaults(func=_cmd_accounts)

    p_txn = sub.add_parser("transactions", help="list cached transactions")
    p_txn.add_argument("--start")
    p_txn.add_argument("--end")
    p_txn.add_argument("--account")
    p_txn.add_argument("--search")
    p_txn.add_argument("--category", help="filter to one category")
    p_txn.add_argument("--no-transfers", action="store_true",
                       help="exclude internal transfers / card payments")
    p_txn.add_argument("--limit", type=int, default=50)
    p_txn.add_argument("--json", action="store_true")
    p_txn.set_defaults(func=_cmd_transactions)

    p_sum = sub.add_parser("summary", help="aggregate spending")
    p_sum.add_argument("--group-by", choices=["account", "org", "month", "category"],
                       default="category")
    p_sum.add_argument("--start")
    p_sum.add_argument("--end")
    p_sum.add_argument("--include-transfers", action="store_true",
                       help="count transfers/payments as spending (off by default)")
    p_sum.set_defaults(func=_cmd_summary)

    p_cat = sub.add_parser("categorize", help="seed default rules + show coverage")
    p_cat.add_argument("--reseed", action="store_true",
                       help="re-insert the default rule set even if rules exist")
    p_cat.set_defaults(func=_cmd_categorize)

    p_rules = sub.add_parser("rules", help="list / add / remove category rules")
    p_rules.add_argument("action", nargs="?", choices=["list", "add", "rm"],
                         default="list")
    p_rules.add_argument("--pattern")
    p_rules.add_argument("--category")
    p_rules.add_argument("--field", choices=["description", "payee", "any"],
                         default="any")
    p_rules.add_argument("--priority", type=int, default=100)
    p_rules.add_argument("--transfer", action="store_true",
                         help="mark matches as transfers (excluded from spend)")
    p_rules.add_argument("--rule-id", type=int)
    p_rules.add_argument("--json", action="store_true")
    p_rules.set_defaults(func=_cmd_rules)

    p_set = sub.add_parser("set-category",
                           help="pin a category to one transaction (survives sync)")
    p_set.add_argument("txn_id")
    p_set.add_argument("category")
    p_set.add_argument("--transfer", action="store_true")
    p_set.set_defaults(func=_cmd_set_category)

    p_unc = sub.add_parser("uncategorized", help="top uncategorized merchants")
    p_unc.add_argument("--limit", type=int, default=20)
    p_unc.add_argument("--json", action="store_true")
    p_unc.set_defaults(func=_cmd_uncategorized)

    p_nw = sub.add_parser("networth", help="net-worth total per snapshot date")
    p_nw.add_argument("--json", action="store_true")
    p_nw.set_defaults(func=_cmd_networth)

    p_stats = sub.add_parser("stats", help="archive size and date coverage")
    p_stats.set_defaults(func=_cmd_stats)

    p_imp = sub.add_parser(
        "import",
        help="import exported statement CSVs (file or directory) into the archive",
    )
    p_imp.add_argument("paths", nargs="+", help="CSV file(s) or director(ies)")
    p_imp.add_argument("--dry-run", action="store_true",
                       help="parse and count without writing to the archive")
    p_imp.add_argument("--json", action="store_true")
    p_imp.set_defaults(func=_cmd_import)

    p_bd = sub.add_parser(
        "burndown",
        help="per-envelope planned target vs. actual spend for one month",
    )
    p_bd.add_argument("--month", required=True, help="month to report, as YYYY-MM")
    p_bd.add_argument("--config", help="path to budget config (default: ~/.finance-mcp/budget.json)")
    p_bd.add_argument("--json", action="store_true")
    p_bd.set_defaults(func=_cmd_burndown)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
