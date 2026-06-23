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
                    account_id=args.account,
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
            a = f" @{r['account_id']}" if r.get("account_id") else ""
            print(f"  {r['rule_id']:>4} p{r['priority']:<4} {r['field']:<11} "
                  f"'{r['pattern']}' -> {r['category']}{t}{a}")
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


def _parse_date(value: str):
    from datetime import date

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}") from exc


def _cmd_forecast(args: argparse.Namespace) -> int:
    from datetime import timedelta
    from pathlib import Path

    from . import budget_config, forecast

    try:
        as_of = _parse_date(args.as_of) if args.as_of else None
        through = _parse_date(args.through) if args.through else None
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 1
    if as_of is None:
        from datetime import date as _date

        as_of = _date.today()
    if through is None:
        through = as_of + timedelta(days=forecast.DEFAULT_HORIZON_DAYS)
    if through < as_of:
        print(
            f"Invalid window: --through {through} is before --as-of {as_of}",
            file=sys.stderr,
        )
        return 1

    cfg_path = Path(args.config).expanduser() if args.config else config.budget_config_path()
    try:
        cfg = budget_config.load_config(cfg_path)
    except budget_config.BudgetConfigError as exc:
        print(f"Budget config error: {exc}", file=sys.stderr)
        return 1

    report = forecast.forecast_report(cfg, as_of=as_of, through=through)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    s = report["summary"]
    print(f"Forecast {report['as_of']} → {report['through']}")
    print(f"  {'Envelope':<22} {'Balance':>11} {'In':>10} {'Out':>10} {'Min':>11}  Verdict")
    for e in report["envelopes"]:
        if e["verdict"] == "balance_unknown":
            print(f"  {e['envelope']:<22} {'—':>11} {e['total_in']:>10.2f} "
                  f"{e['total_out']:>10.2f} {'—':>11}  balance unknown")
            continue
        verdict = "ok"
        if e["verdict"] == "at_risk":
            verdict = f"AT RISK {e['at_risk_date']} (short {e['shortfall']:.2f})"
        else:
            caveats = []
            if e["same_day_funding_dependent"]:
                caveats.append("same-day funded")
            if e["relies_on_projected_income"]:
                caveats.append("relies on projected income")
            if caveats:
                verdict = f"ok ({', '.join(caveats)})"
        print(f"  {e['envelope']:<22} {e['current_balance']:>11.2f} {e['total_in']:>10.2f} "
              f"{e['total_out']:>10.2f} {e['projected_min_balance']:>11.2f}  {verdict}")
    print(f"  ({s['at_risk']} at risk, {s['sufficient']} ok, "
          f"{s['balance_unknown']} balance unknown)")
    return 0


def _cmd_allocation(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import allocation, budget_config

    try:
        start = _parse_date(args.start) if args.start else None
        end = _parse_date(args.end) if args.end else None
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 1
    if start is not None and end is not None and end < start:
        print(f"Invalid window: --end {end} is before --start {start}",
              file=sys.stderr)
        return 1

    cfg_path = Path(args.config).expanduser() if args.config else config.budget_config_path()
    try:
        cfg = budget_config.load_config(cfg_path)
    except budget_config.BudgetConfigError as exc:
        print(f"Budget config error: {exc}", file=sys.stderr)
        return 1

    try:
        report = allocation.allocation_report(
            cfg, start=start, end=end, day_tolerance=args.day_tolerance
        )
    except ValueError as exc:
        print(f"Allocation audit error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    w = report["window"]
    s = report["summary"]
    print(f"Allocation audit {w['start']} -> {w['end']} "
          f"(tolerance {report['day_tolerance']}d)")
    if not report["transfers"]:
        print("  (no scheduled transfers configured)")
    for tr in report["transfers"]:
        src = tr["from_envelope"] or "(external)"
        print(f"  {tr['name']}: {src} -> {tr['to_envelope']} "
              f"${tr['amount']} [{tr['kind']}]")
        for occ in tr["occurrences"]:
            if occ["status"] == "missing":
                print(f"    {occ['expected_date']}  MISSING "
                      f"(expected ${occ['expected_amount']})")
            else:
                drift = occ["drift_days"]
                drift_s = f", {drift:+d}d" if drift else ""
                print(f"    {occ['expected_date']}  {occ['status']}{drift_s}  "
                      f"actual ${occ['actual_amount']} on {occ['actual_date']}")
    parts = [f"{v} {k}" for k, v in s.items() if v]
    print(f"  ({', '.join(parts) if parts else 'no occurrences'})")
    return 0


def _cmd_subscriptions(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import budget_config, subscription, store

    try:
        start = _parse_date(args.start) if args.start else None
        end = _parse_date(args.end) if args.end else None
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 1
    if start is not None and end is not None and end < start:
        print(f"Invalid window: --end {end} is before --start {start}",
              file=sys.stderr)
        return 1

    cfg_path = Path(args.config).expanduser() if args.config else config.budget_config_path()

    if args.action == "detect":
        from datetime import date, timedelta

        e = end or date.today()
        s = start or (e - timedelta(days=subscription.DEFAULT_WINDOW_DAYS))
        try:
            view = store.load_archive_view()
            existing_cfg = (
                budget_config.load_config(cfg_path) if cfg_path.exists() else None
            )
            detected = subscription.detect_subscriptions(
                view["transactions"], start=s, end=e,
                min_occurrences=args.min_occurrences,
                day_tolerance=args.day_tolerance,
                config=existing_cfg,
            )
            summary = subscription.merge_subscriptions_into_file(cfg_path, detected["bills"])
        except (ValueError, budget_config.BudgetConfigError) as exc:
            print(f"Subscription detect error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            summary["unsupported_cadence"] = [
                sk for sk in detected["skipped"] if sk.get("kind") == "unsupported_cadence"
            ]
            summary["needs_review"] = [
                sk for sk in detected["skipped"] if sk.get("kind") == "needs_review"
            ]
            print(json.dumps(summary, indent=2))
            return 0
        print(f"Detected subscriptions {s} -> {e} (saved to {summary['path']})")
        print(f"  Added: {summary['added']}   Already tracked: {summary['already_tracked']}"
              f"   Tracked total: {summary['tracked_total']}")
        for b in summary["added_bills"]:
            print(f"    + {b['name']}: ${b['amount']} on day {b['day']} (match {b['match']!r})")
        unsupported = [
            sk for sk in detected["skipped"] if sk.get("kind") == "unsupported_cadence"
        ]
        review = [
            sk for sk in detected["skipped"] if sk.get("kind") == "needs_review"
        ]
        if unsupported:
            print("  Not saved (only monthly bills can be tracked):")
            for sk in unsupported:
                print(f"    - {sk['merchant']} ({sk['cadence']})")
        if review:
            print("  Needs review (not auto-saved):")
            for sk in review:
                print(f"    - {sk['merchant']}: {sk['reason']}")
        return 0

    if args.action == "mark":
        if not args.name or not args.lifecycle:
            print("subscriptions mark requires --name and --lifecycle",
                  file=sys.stderr)
            return 1
        try:
            result = subscription.set_bill_lifecycle(
                cfg_path, args.name, args.lifecycle,
                cancel_effective=args.effective,
                variable=args.variable,
            )
        except budget_config.BudgetConfigError as exc:
            print(f"Subscription mark error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        eff = result.get("cancel_effective")
        eff_s = f" effective {eff}" if eff else ""
        var_s = ", variable amount" if result.get("variable") else ""
        print(f"Marked {result['name']!r} as {result['lifecycle']}{eff_s}{var_s} "
              f"(saved to {result['path']})")
        return 0

    # action == "audit": with no config there are simply no tracked bills, and
    # the audit still surfaces every untracked recurring merchant it finds.
    if cfg_path.exists():
        try:
            cfg = budget_config.load_config(cfg_path)
        except budget_config.BudgetConfigError as exc:
            print(f"Budget config error: {exc}", file=sys.stderr)
            return 1
    else:
        cfg = budget_config.BudgetConfig(
            version=budget_config.SUPPORTED_VERSION, envelopes=()
        )

    try:
        report = subscription.subscription_report(
            cfg, start=start, end=end,
            day_tolerance=args.day_tolerance,
            min_occurrences=args.min_occurrences,
        )
    except ValueError as exc:
        print(f"Subscription audit error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    w = report["window"]
    sm = report["summary"]
    print(f"Subscription audit {w['start']} -> {w['end']}")
    print(f"  Tracked bills: {sm['tracked']}")
    if report.get("came_back"):
        print("  ⚠ CANCELED BILLS THAT CAME BACK (charged after cancellation):")
        for c in report["came_back"]:
            seen = c.get("came_back_on") or c.get("last_seen")
            print(f"    {c['name']}: ${c['amount']} — marked {c['lifecycle']} "
                  f"effective {c.get('cancel_effective')}, charged again {seen}")
    if report.get("tracked"):
        print("  Tracked subscriptions:")
        for t in report["tracked"]:
            env = f" [{t['envelope']}]" if t.get("envelope") else ""
            last = t.get("last_seen") or "never"
            nxt = t.get("next_due") or "?"
            lc = t.get("lifecycle", "active")
            if lc != "active":
                flag = "came back!" if t.get("came_back") else lc
                state = f"{flag}, effective {t.get('cancel_effective')}"
            else:
                state = t["status"]
            print(f"    {t['name']}{env}: ${t['amount']} on day {t['day']} "
                  f"({state}) — next due {nxt}, last seen {last}")
    if report["expected_missing"]:
        print("  MISSING expected charges (possible billing problem / cancellation):")
        for m in report["expected_missing"]:
            last = m.get("last_seen") or "never"
            print(f"    {m['name']}: expected ~${m['expected_amount']} "
                  f"on {m['expected_date']}, last seen {last}")
    else:
        print("  No missing expected charges.")
    if report["candidate_new"]:
        print("  Candidate untracked recurring charges (review with assistant):")
        for c in report["candidate_new"]:
            print(f"    {c['merchant']}: ${c['amount']} x{c['occurrences']} "
                  f"({c['cadence']})")
    else:
        print("  No new recurring candidates found.")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from . import reconcile

    report = reconcile.reconcile()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print("Transfer reconciliation")
    print(f"  links inferred:   {report['links']}")
    print(f"  needs confirm:    {report['needs_confirm']}")
    print(f"  unmatched legs:   {report['unmatched']}")
    print(f"  confirmed (kept): {report['confirmed_preserved']}")
    print(f"  promoted:         {report['promoted']}")
    print(f"  downgraded:       {report['downgraded']}")
    return 0


def _cmd_transfers(args: argparse.Namespace) -> int:
    from . import reconcile

    view = reconcile.transfers_view(status=args.status)
    if args.json:
        print(json.dumps(view, indent=2))
        return 0

    label = f" ({args.status})" if args.status else ""
    print(f"Transfers{label}: {view['total']}")
    for tr in view["transfers"]:
        src = tr["from_account"] or "?"
        dst = tr["to_account"] or "?"
        amount = tr["amount"] if tr["amount"] is not None else "?"
        why = f"  [{tr['why']}]" if tr["why"] else ""
        print(f"  #{tr['link_id']} {tr['status']:<11} "
              f"{src} -> {dst} ${amount}{why}")
    if not view["transfers"]:
        print("  (none)")
    return 0


def _cmd_confirm(args: argparse.Namespace) -> int:
    from . import reconcile

    try:
        link = reconcile.confirm(args.link_id)
    except LookupError as exc:
        print(f"No such link: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Cannot confirm: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(link, indent=2))
        return 0
    print(f"Confirmed link #{link['link_id']}: "
          f"{link['debit_txn_id']} -> {link['credit_txn_id']} "
          f"(status {link['status']})")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    from . import webui

    return webui.serve(host=args.host, port=args.port,
                       allow_hosts=tuple(args.allow_host))


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
    p_rules.add_argument("--account",
                         help="scope the rule to one account_id (omit = any account)")
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

    p_fc = sub.add_parser(
        "forecast",
        help="per-envelope sufficiency: will it cover upcoming bills, and when is it at risk",
    )
    p_fc.add_argument("--as-of", help="start of the window, as YYYY-MM-DD (default: today)")
    p_fc.add_argument(
        "--through",
        help="end of the window, as YYYY-MM-DD (default: as-of + 60 days)",
    )
    p_fc.add_argument("--config", help="path to budget config (default: ~/.finance-mcp/budget.json)")
    p_fc.add_argument("--json", action="store_true")
    p_fc.set_defaults(func=_cmd_forecast)

    p_al = sub.add_parser(
        "allocation",
        help="did each scheduled transfer fire on time, late, or not at all",
    )
    p_al.add_argument("--start", help="window start, as YYYY-MM-DD (default: a year back)")
    p_al.add_argument("--end", help="window end, as YYYY-MM-DD (default: today)")
    p_al.add_argument("--day-tolerance", type=int, default=7,
                      help="days a transfer may drift and still count as fired")
    p_al.add_argument("--config", help="path to budget config (default: ~/.finance-mcp/budget.json)")
    p_al.add_argument("--json", action="store_true")
    p_al.set_defaults(func=_cmd_allocation)

    p_sub = sub.add_parser(
        "subscriptions",
        help="missing expected charges + candidate untracked recurring merchants",
    )
    p_sub.add_argument(
        "action", nargs="?", choices=["audit", "detect", "mark"], default="audit",
        help="'audit' (default) checks tracked bills and surfaces candidates; "
             "'detect' saves detected recurring charges into the budget config; "
             "'mark' sets a bill's lifecycle (canceling/canceled/active)",
    )
    p_sub.add_argument("--start", help="window start, as YYYY-MM-DD (default: a year back)")
    p_sub.add_argument("--end", help="window end, as YYYY-MM-DD (default: today)")
    p_sub.add_argument("--day-tolerance", type=int, default=7,
                       help="days a charge may drift and still count as on schedule")
    p_sub.add_argument("--min-occurrences", type=int, default=3,
                       help="times a merchant must recur to surface as a candidate")
    p_sub.add_argument("--name", help="bill name to mark (with action 'mark')")
    p_sub.add_argument(
        "--lifecycle", choices=["active", "canceling", "canceled"],
        help="lifecycle to set the named bill to (with action 'mark')",
    )
    p_sub.add_argument(
        "--effective",
        help="cancellation effective date, as YYYY-MM-DD (required when marking "
             "canceling/canceled; omit when reactivating)",
    )
    p_sub.add_argument(
        "--variable", dest="variable", action="store_const", const=True,
        default=None,
        help="mark the bill as variable-amount (match by merchant/date, ignore "
             "amount) (with action 'mark')",
    )
    p_sub.add_argument(
        "--no-variable", dest="variable", action="store_const", const=False,
        help="clear the variable-amount flag, restoring exact-amount matching "
             "(with action 'mark')",
    )
    p_sub.add_argument("--config", help="path to budget config (default: ~/.finance-mcp/budget.json)")
    p_sub.add_argument("--json", action="store_true")
    p_sub.set_defaults(func=_cmd_subscriptions)

    p_rec = sub.add_parser(
        "reconcile",
        help="rebuild internal-transfer links from the archive (idempotent)",
    )
    p_rec.add_argument("--json", action="store_true")
    p_rec.set_defaults(func=_cmd_reconcile)

    p_tr = sub.add_parser(
        "transfers",
        help="list reconciled transfer links (From -> To $X [why])",
    )
    p_tr.add_argument(
        "--status",
        choices=["confirmed", "inferred", "unconfirmed", "unmatched"],
        help="show only links in this lifecycle state",
    )
    p_tr.add_argument("--json", action="store_true")
    p_tr.set_defaults(func=_cmd_transfers)

    p_cf = sub.add_parser(
        "confirm",
        help="confirm one transfer link by id (locks it as authoritative)",
    )
    p_cf.add_argument("link_id", type=int, help="the link id to confirm")
    p_cf.add_argument("--json", action="store_true")
    p_cf.set_defaults(func=_cmd_confirm)

    p_web = sub.add_parser(
        "web",
        help="serve a local read-only web UI for reviewing the archive",
    )
    p_web.add_argument("--host", default="127.0.0.1",
                       help="bind address (default 127.0.0.1; serves private data)")
    p_web.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    p_web.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                       help="extra Host-header name allowed to reach the API "
                            "(repeatable; needed to reach a non-loopback bind)")
    p_web.set_defaults(func=_cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
