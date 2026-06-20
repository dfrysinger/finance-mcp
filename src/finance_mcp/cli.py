"""Command-line interface: claim, sync, accounts, transactions, summary."""

from __future__ import annotations

import argparse
import json
import sys

from . import archive, categories, client, config, queries, store, sync


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
