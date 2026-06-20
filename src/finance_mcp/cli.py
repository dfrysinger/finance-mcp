"""Command-line interface: claim, sync, accounts, transactions, summary."""

from __future__ import annotations

import argparse
import json
import sys

from . import client, config, queries, store, sync


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
    cache = store.load_cache()
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
    cache = store.load_cache()
    rows = queries.filter_transactions(
        cache["transactions"],
        start_date=args.start,
        end_date=args.end,
        account_id=args.account,
        search=args.search,
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
        print(f"{(txn.get('posted') or '')[:10]} {flag} {txn.get('amount',''):>12} "
              f"{(txn.get('account_name') or '')[:18]:<18} {txn.get('description','')}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    cache = store.load_cache()
    result = queries.spending_summary(
        cache["transactions"],
        group_by=args.group_by,
        start_date=args.start,
        end_date=args.end,
    )
    print(json.dumps(result, indent=2))
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
    p_txn.add_argument("--limit", type=int, default=50)
    p_txn.add_argument("--json", action="store_true")
    p_txn.set_defaults(func=_cmd_transactions)

    p_sum = sub.add_parser("summary", help="aggregate spending")
    p_sum.add_argument("--group-by", choices=["account", "org", "month"], default="account")
    p_sum.add_argument("--start")
    p_sum.add_argument("--end")
    p_sum.set_defaults(func=_cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
